"""Per-connection session state for MUD client sessions."""

from __future__ import annotations

# std imports
import asyncio
from typing import Any, Union, Callable, Optional, Awaitable

# 3rd party
from telnetlib3.stream_writer import TelnetWriter, TelnetWriterUnicode
from telnetlib3._session_context import TelnetSessionContext  # pylint: disable=no-name-in-module


class _CommandQueue:
    """Mutable state for a running command queue, enabling display and cancellation."""

    __slots__ = ("commands", "current_idx", "cancelled", "cancel_event", "render")

    def __init__(self, commands: list[str], render: Callable[[], None]) -> None:
        self.commands = commands
        self.current_idx = 0
        self.cancelled = False
        self.cancel_event = asyncio.Event()
        self.render = render


class SessionContext(TelnetSessionContext):
    """
    Per-connection runtime state for a MUD client session.

    Extends :class:`~telnetlib3._session_context.TelnetSessionContext` with
    MUD-specific state (rooms, macros, autoreplies, highlights, chat, etc.).
    Created in ``_session_shell`` and attached as ``writer.ctx``.

    :param session_key: Session identifier (``"host:port"``).
    """

    def __init__(self, session_key: str = "") -> None:
        """Initialize session context with default state."""
        super().__init__()

        # back-reference to the writer (set by _session_shell)
        self.writer: Optional[Union[TelnetWriter, TelnetWriterUnicode]] = None

        # identity
        self.session_key: str = session_key

        # room / navigation
        self.room_graph: Any = None
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
        self.discover_task: Optional[asyncio.Task[None]] = None
        self.randomwalk_active: bool = False
        self.randomwalk_current: int = 0
        self.randomwalk_total: int = 0
        self.randomwalk_task: Optional[asyncio.Task[None]] = None
        self.randomwalk_auto_search: bool = False
        self.randomwalk_auto_evaluate: bool = False
        self.travel_task: Optional[asyncio.Task[None]] = None
        self.active_command: Optional[str] = None
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
        self.command_queue: Optional[_CommandQueue] = None

        # macros & autoreplies (autoreply_engine inherited from base)
        self.macro_defs: list[Any] = []
        self.macros_file: str = ""
        self.autoreply_rules: list[Any] = []
        self.autoreplies_file: str = ""

        # highlighters
        self.highlight_rules: list[Any] = []
        self.highlights_file: str = ""
        self.highlight_engine: Optional[Any] = None

        # prompt / GA pacing
        self.wait_for_prompt: Optional[Callable[..., Awaitable[None]]] = None
        self.echo_command: Optional[Callable[[str], None]] = None
        self.prompt_ready: Optional[asyncio.Event] = None

        # GMCP
        self.gmcp_data: dict[str, Any] = {}
        self.on_gmcp_ready: Optional[Callable[[], None]] = None
        self.gmcp_snapshot_file: str = ""
        self._gmcp_dirty: bool = False

        # progress bars
        self.progressbar_configs: list[Any] = []
        self.progressbars_file: str = ""

        # highlight captures
        self.captures: dict[str, int] = {}
        self.capture_log: dict[str, list[dict[str, Any]]] = {}

        # chat (GMCP Comm.Channel)
        self.chat_messages: list[dict[str, Any]] = []
        self.chat_unread: int = 0
        self.chat_channels: list[dict[str, Any]] = []
        self.chat_file: str = ""
        self.on_chat_text: Optional[Callable[[dict[str, Any]], None]] = None
        self.on_chat_channels: Optional[Callable[[list[dict[str, Any]]], None]] = None

        # rendering / input config
        # (raw_mode, ascii_eol, input_filter, color_filter, typescript_file
        #  inherited from TelnetSessionContext)
        self.repl_enabled: bool = False
        self.history_file: Optional[str] = None

        # modem activity dots (set by REPL, used by _send_chained et al.)
        self.rx_dot: Optional[Any] = None
        self.tx_dot: Optional[Any] = None
        self.cx_dot: Optional[Any] = None

        # REPL internals
        self.key_dispatch: Optional[Any] = None
        self.cursor_style: str = ""
        self.send_line: Optional[Callable[[str], None]] = None
        # autoreply_wait_fn inherited from TelnetSessionContext
        self.send_naws: Optional[Callable[[], None]] = None

        # debounced timestamp persistence
        self._macros_dirty: bool = False
        self._autoreplies_dirty: bool = False
        self._save_timer: Optional[asyncio.TimerHandle] = None

    def mark_macros_dirty(self) -> None:
        """Mark macros as needing a save and schedule a debounced flush."""
        self._macros_dirty = True
        self._schedule_flush()

    def mark_autoreplies_dirty(self) -> None:
        """Mark autoreplies as needing a save and schedule a debounced flush."""
        self._autoreplies_dirty = True
        self._schedule_flush()

    def mark_gmcp_dirty(self) -> None:
        """Mark GMCP snapshot as needing a save and schedule a debounced flush."""
        self._gmcp_dirty = True
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        """Schedule :meth:`flush_timestamps` after 30 seconds if not already pending."""
        if self._save_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_timer = loop.call_later(30, self._flush_timestamps_sync)

    def _flush_timestamps_sync(self) -> None:
        """Synchronous wrapper called by the event loop timer."""
        self._save_timer = None
        self.flush_timestamps()

    def flush_timestamps(self) -> None:
        """Persist macro/autoreply timestamps if dirty."""
        if self._macros_dirty and self.macros_file and self.macro_defs:
            from .macros import save_macros

            save_macros(self.macros_file, self.macro_defs, self.session_key)
            self._macros_dirty = False
        if self._autoreplies_dirty and self.autoreplies_file and self.autoreply_rules:
            from .autoreply import save_autoreplies

            save_autoreplies(self.autoreplies_file, self.autoreply_rules, self.session_key)
            self._autoreplies_dirty = False
        if self._gmcp_dirty and self.gmcp_snapshot_file and self.gmcp_data:
            from .gmcp_snapshot import save_gmcp_snapshot

            save_gmcp_snapshot(self.gmcp_snapshot_file, self.session_key, self.gmcp_data)
            self._gmcp_dirty = False

    def close(self) -> None:
        """Cancel pending tasks and flush dirty timestamps."""
        for task in (self.discover_task, self.randomwalk_task):
            if task is not None and not task.done():
                task.cancel()
        self.discover_task = None
        self.randomwalk_task = None
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        self.flush_timestamps()
