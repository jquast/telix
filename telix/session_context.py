"""Per-connection session state for MUD client sessions."""

import typing
import asyncio
from collections.abc import Callable, Awaitable

import telnetlib3.stream_writer
import telnetlib3._session_context  # pylint: disable=no-name-in-module

from . import macros, autoreply, gmcp_snapshot


class CommandQueue:
    """Mutable state for a running command queue, enabling display and cancellation."""

    __slots__ = ("cancel_event", "cancelled", "commands", "current_idx", "render")

    def __init__(self, commands: list[str], render: Callable[[], None]) -> None:
        self.commands = commands
        self.current_idx = 0
        self.cancelled = False
        self.cancel_event = asyncio.Event()
        self.render = render


class SessionContext(telnetlib3._session_context.TelnetSessionContext):
    """
    Per-connection runtime state for a MUD client session.

    Extends :class:`~telnetlib3._session_context.TelnetSessionContext` with
    MUD-specific state (rooms, macros, autoreplies, highlights, chat, etc.).
    Created in ``session_shell`` and attached as ``writer.ctx``.

    :param session_key: Session identifier (``"host:port"``).
    """

    def __init__(self, session_key: str = "") -> None:
        """Initialize session context with default state."""
        super().__init__()

        # back-reference to the writer (set by session_shell)
        self.writer: telnetlib3.stream_writer.TelnetWriter | telnetlib3.stream_writer.TelnetWriterUnicode | None = None

        # identity
        self.session_key: str = session_key

        # room / navigation
        self.room_graph: typing.Any = None
        self.rooms_file: str = ""
        self.current_room_file: str = ""
        self.current_room_num: str = ""
        self.previous_room_num: str = ""
        self.macro_start_room: str = ""
        self.room_changed: asyncio.Event = asyncio.Event()
        self.room_arrival_timeout: float = 5.0

        # walk automation
        self.discover_active: bool = False
        self.discover_current: int = 0
        self.discover_total: int = 0
        self.discover_task: asyncio.Task[None] | None = None
        self.randomwalk_active: bool = False
        self.randomwalk_current: int = 0
        self.randomwalk_total: int = 0
        self.randomwalk_task: asyncio.Task[None] | None = None
        self.randomwalk_auto_search: bool = False
        self.randomwalk_auto_evaluate: bool = False
        self.randomwalk_auto_survey: bool = False
        self.travel_task: asyncio.Task[None] | None = None
        self.active_command: str | None = None
        self.active_command_time: float = 0.0
        self.blocked_exits: set[tuple[str, str]] = set()  # (room_num, direction)

        # walk resume state
        self.last_walk_mode: str = ""
        self.last_walk_room: str = ""
        self.last_walk_strategy: str = "bfs"
        self.last_walk_noreply: bool = False
        self.last_walk_visited: set[str] = set()
        self.last_walk_tried: set[tuple[str, str]] = set()

        # command queue
        self.command_queue: CommandQueue | None = None

        # macros & autoreplies (autoreply_engine inherited from base)
        self.macro_defs: list[typing.Any] = []
        self.macros_file: str = ""
        self.autoreply_rules: list[typing.Any] = []
        self.autoreplies_file: str = ""

        # highlighters
        self.highlight_rules: list[typing.Any] = []
        self.highlights_file: str = ""
        self.highlight_engine: typing.Any | None = None

        # prompt / GA pacing
        self.wait_for_prompt: Callable[..., Awaitable[None]] | None = None
        self.echo_command: Callable[[str], None] | None = None
        self.prompt_ready: asyncio.Event | None = None

        # GMCP
        self.gmcp_data: dict[str, typing.Any] = {}
        self.on_gmcp_ready: Callable[[], None] | None = None
        self.gmcp_snapshot_file: str = ""
        self.gmcp_dirty: bool = False

        # progress bars
        self.progressbar_configs: list[typing.Any] = []
        self.progressbars_file: str = ""

        # highlight captures
        self.captures: dict[str, int] = {}
        self.capture_log: dict[str, list[dict[str, typing.Any]]] = {}

        # chat (GMCP Comm.Channel)
        self.chat_messages: list[dict[str, typing.Any]] = []
        self.chat_unread: int = 0
        self.chat_channels: list[dict[str, typing.Any]] = []
        self.chat_file: str = ""
        self.on_room_info: Callable[[dict[str, typing.Any]], None] | None = None
        self.on_chat_text: Callable[[dict[str, typing.Any]], None] | None = None
        self.on_chat_channels: Callable[[list[dict[str, typing.Any]]], None] | None = None
        self.on_autoreply_activity: Callable[[], None] | None = None

        # rendering / input config
        # (raw_mode, ascii_eol, input_filter, color_filter, typescript_file
        #  inherited from TelnetSessionContext)
        self.repl_enabled: bool = False
        self.history_file: str | None = None

        # modem activity dots (set by REPL, used by send_chained et al.)
        self.rx_dot: typing.Any | None = None
        self.tx_dot: typing.Any | None = None
        self.cx_dot: typing.Any | None = None

        # REPL internals
        self.key_dispatch: typing.Any | None = None
        self.cursor_style: str = ""
        self.send_line: Callable[[str], None] | None = None
        # autoreply_wait_fn inherited from TelnetSessionContext
        self.send_naws: Callable[[], None] | None = None

        # debounced timestamp persistence
        self.macros_dirty: bool = False
        self.autoreplies_dirty: bool = False
        self.save_timer: asyncio.TimerHandle | None = None

    def mark_macros_dirty(self) -> None:
        """Mark macros as needing a save and schedule a debounced flush."""
        self.macros_dirty = True
        self.schedule_flush()

    def mark_autoreplies_dirty(self) -> None:
        """Mark autoreplies as needing a save and schedule a debounced flush."""
        self.autoreplies_dirty = True
        self.schedule_flush()

    def mark_gmcp_dirty(self) -> None:
        """Mark GMCP snapshot as needing a save and schedule a debounced flush."""
        self.gmcp_dirty = True
        self.schedule_flush()

    def schedule_flush(self) -> None:
        """Schedule :meth:`flush_timestamps` after 30 seconds if not already pending."""
        if self.save_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.save_timer = loop.call_later(30, self.flush_timestamps_sync)

    def flush_timestamps_sync(self) -> None:
        """Synchronous wrapper called by the event loop timer."""
        self.save_timer = None
        self.flush_timestamps()

    def flush_timestamps(self) -> None:
        """Persist macro/autoreply timestamps if dirty."""
        if self.macros_dirty and self.macros_file and self.macro_defs:
            macros.save_macros(self.macros_file, self.macro_defs, self.session_key)
            self.macros_dirty = False
        if self.autoreplies_dirty and self.autoreplies_file and self.autoreply_rules:
            autoreply.save_autoreplies(self.autoreplies_file, self.autoreply_rules, self.session_key)
            self.autoreplies_dirty = False
        if self.gmcp_dirty and self.gmcp_snapshot_file and self.gmcp_data:
            gmcp_snapshot.save_gmcp_snapshot(self.gmcp_snapshot_file, self.session_key, self.gmcp_data)
            self.gmcp_dirty = False

    def close(self) -> None:
        """Cancel pending tasks and flush dirty timestamps."""
        for task in (self.discover_task, self.randomwalk_task):
            if task is not None and not task.done():
                task.cancel()
        self.discover_task = None
        self.randomwalk_task = None
        if self.save_timer is not None:
            self.save_timer.cancel()
            self.save_timer = None
        self.flush_timestamps()
