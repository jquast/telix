"""Per-connection session state for MUD client sessions."""

import asyncio
from typing import IO, TYPE_CHECKING, Any
from collections.abc import Callable, Awaitable

import telnetlib3.stream_writer
import telnetlib3._session_context  # pylint: disable=no-name-in-module

from . import mslp, macros, autoreply, ws_transport, gmcp_snapshot, ssh_transport

if TYPE_CHECKING:
    from . import rooms, highlighter, progressbars


class CommandQueue:
    """Mutable state for a running command queue, enabling display and cancellation."""

    __slots__ = ("cancel_event", "cancelled", "commands", "current_idx", "render")

    def __init__(self, commands: list[str], render: Callable[[], None]) -> None:
        self.commands = commands
        self.current_idx = 0
        self.cancelled = False
        self.cancel_event = asyncio.Event()
        self.render = render


class TelixSessionContext(telnetlib3._session_context.TelnetSessionContext):
    """
    Per-connection runtime state for a MUD client session.

    Extends :class:`~telnetlib3._session_context.TelnetSessionContext` with
    MUD-specific state (rooms, macros, autoreplies, highlights, chat, etc.).
    Created in ``session_shell`` and attached as ``writer.ctx``.

    :param session_key: Session identifier (``"host:port"``).
    """

    def __init__(
        self,
        session_key: str = "",
        writer: (
            telnetlib3.stream_writer.TelnetWriterUnicode | ws_transport.WebSocketWriter | ssh_transport.SSHWriter | None
        ) = None,
        encoding: str = "",
        raw_mode: bool | None = None,
        ascii_eol: bool = False,
        input_filter: Any | None = None,
        autoreply_engine: Any | None = None,
        autoreply_wait_fn: Callable[..., Awaitable[None]] | None = None,
        typescript_file: IO[str] | None = None,
        gmcp_data: dict[str, Any] | None = None,
    ):
        """Initialize session context with default state."""
        super().__init__()

        # back-reference to the writer (set by session_shell), kind of annoying, but
        # the 'ctx' is passed around everywhere, so naturally we need access to it
        self.writer = writer
        self.encoding = encoding or (getattr(writer, "encoding", "") if writer else "") or "utf-8"

        # identity
        self.session_key: str = session_key

        # room / navigation
        self.room_graph: rooms.RoomStore | None = None
        self.rooms_file: str = ""
        self.current_room_file: str = ""
        self.current_room_num: str = ""
        self.previous_room_num: str = ""
        self.macro_start_room: str = ""
        self.room_changed: asyncio.Event = asyncio.Event()
        self.room_arrival_timeout: float = 3.0

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
        self.macro_defs: list[macros.Macro] = []
        self.macros_file: str = ""
        self.autoreply_rules: list[autoreply.AutoreplyRule] = []
        self.autoreplies_file: str = ""

        # highlighters
        self.highlight_rules: list[highlighter.HighlightRule] = []
        self.highlights_file: str = ""
        self.highlight_engine: highlighter.HighlightEngine | None = None

        # prompt / GA pacing
        self.wait_for_prompt: Callable[..., Awaitable[None]] | None = None
        self.echo_command: Callable[[str], None] | None = None
        self.prompt_ready: asyncio.Event | None = None

        # GMCP
        self.gmcp_data: dict[str, Any] = gmcp_data if gmcp_data is not None else {}
        self.on_gmcp_ready: Callable[[], None] | None = None
        self.gmcp_snapshot_file: str = ""
        self.gmcp_dirty: bool = False

        # progress bars
        self.progressbar_configs: list[progressbars.BarConfig] = []
        self.progressbars_file: str = ""

        # highlight captures
        self.captures: dict[str, int] = {}
        self.capture_log: dict[str, list[dict[str, Any]]] = {}

        # chat (GMCP Comm.Channel)
        self.chat_messages: list[dict[str, Any]] = []
        self.chat_unread: int = 0
        self.chat_channels: list[dict[str, Any]] = []
        self.chat_file: str = ""
        self.on_room_info: Callable[[dict[str, Any]], None] | None = None
        self.on_chat_text: Callable[[dict[str, Any]], None] | None = None
        self.on_chat_channels: Callable[[list[dict[str, Any]]], None] | None = None
        self.on_autoreply_activity: Callable[[], None] | None = None

        # MSLP link collector
        self.mslp_collector: mslp.MslpCollector = mslp.MslpCollector()

        # rendering / input config
        # (raw_mode, ascii_eol, input_filter, typescript_file
        #  inherited from TelnetSessionContext)
        self.color_filter: Any | None = None
        self.erase_eol: bool = False
        self.repl_enabled: bool = False
        self.ansi_keys: bool = False
        self.history_file: str | None = None

        # REPL internals
        self.key_dispatch: Any | None = None
        self.cursor_style: str = ""
        self.send_line: Callable[[str], None] | None = None
        self.repl_actions: dict[str, Callable[..., Any]] = {}
        self.keyboard_escape: str = "\x1d"
        # autoreply_wait_fn inherited from TelnetSessionContext
        self.send_naws: Callable[[], None] | None = None

        # debounced timestamp persistence
        self.macros_dirty: bool = False
        self.autoreplies_dirty: bool = False
        self.save_timer: asyncio.TimerHandle | None = None

    @classmethod
    def create_using_telnet_ctx(
        cls,
        writer: (telnetlib3.stream_writer.TelnetWriterUnicode | ws_transport.WebSocketWriter),
        session_key: str,
        encoding: str,
    ) -> "TelixSessionContext":
        # writer: ws_transport.WebSocketWriter
        """Class factory method, makes TelixSessionContext from TelnetSessionContext."""
        return cls(
            session_key,
            writer,
            encoding,
            writer.ctx.raw_mode,
            writer.ctx.ascii_eol,
            writer.ctx.input_filter,
            writer.ctx.autoreply_engine,
            writer.ctx.autoreply_wait_fn,
            writer.ctx.typescript_file,
            gmcp_data=getattr(writer.ctx, "gmcp_data", None),
        )

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
