"""Per-connection session state for MUD client sessions."""

import typing
import asyncio
import argparse
import collections
import dataclasses
from collections.abc import Callable, Awaitable

import telnetlib3.stream_writer
import telnetlib3._session_context  # pylint: disable=no-name-in-module

from . import mslp, macros, trigger, ws_transport, gmcp_snapshot, ssh_transport

if typing.TYPE_CHECKING:
    from . import rooms, highlighter, progressbars


class CommandQueue:
    """Mutable state for a running command queue, enabling display and cancellation."""

    __slots__ = ("cancel_event", "commands", "current_idx", "render")

    def __init__(self, commands: list[str], render: Callable[[], None]) -> None:
        self.commands = commands
        self.current_idx = 0
        self.cancel_event = asyncio.Event()
        self.render = render


@dataclasses.dataclass
class RoomState:
    """Navigation and room graph state."""

    graph: "rooms.RoomStore | None" = None
    file: str = ""
    current_file: str = ""
    current: str = ""
    previous: str = ""
    arrival_timeout: float = 3.0
    changed: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


@dataclasses.dataclass
class WalkState:
    """Automated walk (autodiscover / randomwalk / travel) state."""

    discover_active: bool = False
    discover_current: int = 0
    discover_total: int = 0
    discover_task: "asyncio.Task[None] | None" = None
    randomwalk_active: bool = False
    randomwalk_current: int = 0
    randomwalk_total: int = 0
    randomwalk_task: "asyncio.Task[None] | None" = None
    randomwalk_room_change_cmd: str = ""
    travel_task: "asyncio.Task[None] | None" = None
    await_script: str = ""
    active_command: str | None = None
    active_command_time: float = 0.0
    blocked_exits: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    macro_start_room: str = ""
    last_walk_mode: str = ""
    last_walk_room: str = ""
    last_walk_strategy: str = "bfs"
    last_walk_noreply: bool = False
    last_walk_room_change_cmd: str = ""
    last_walk_visit_level: int = 2
    last_walk_visited: set[str] = dataclasses.field(default_factory=set)
    last_walk_tried: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    command_delay: float = 0.0


@dataclasses.dataclass
class MacroState:
    """Macro definitions and persistence state."""

    defs: list[macros.Macro] = dataclasses.field(default_factory=list)
    file: str = ""
    dirty: bool = False


@dataclasses.dataclass
class TriggerState:
    """Trigger rules and engine state."""

    rules: list[trigger.TriggerRule] = dataclasses.field(default_factory=list)
    file: str = ""
    engine: typing.Any | None = None
    wait_fn: typing.Any | None = None
    dirty: bool = False


@dataclasses.dataclass
class HighlightState:
    """Highlight rules, engine, and capture state."""

    rules: "list[highlighter.HighlightRule]" = dataclasses.field(default_factory=list)
    file: str = ""
    engine: typing.Any | None = None
    captures: dict[str, int] = dataclasses.field(default_factory=dict)
    capture_log: dict[str, list[dict[str, typing.Any]]] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ChatState:
    """Chat message and channel state."""

    messages: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    unread: int = 0
    channels: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    file: str = ""
    on_text: typing.Any | None = None
    on_channels: typing.Any | None = None


@dataclasses.dataclass
class GmcpState:
    """GMCP snapshot and callback state."""

    snapshot_file: str = ""
    dirty: bool = False
    on_ready: typing.Any | None = None
    on_room_info: typing.Any | None = None
    package_events: dict[str, asyncio.Event] = dataclasses.field(default_factory=dict)
    any_update: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


@dataclasses.dataclass
class ProgressState:
    """Progress bar configuration state."""

    configs: "list[progressbars.BarConfig]" = dataclasses.field(default_factory=list)
    file: str = ""


@dataclasses.dataclass
class ReplState:
    """REPL input and rendering configuration state."""

    enabled: bool = False
    key_dispatch: typing.Any | None = None
    cursor_style: str = ""
    send_line: typing.Any | None = None
    actions: dict[str, Callable[..., typing.Any]] = dataclasses.field(default_factory=dict)
    keyboard_escape: str = "\x1d"
    send_naws: typing.Any | None = None
    ansi_keys: bool = False
    history_file: str | None = None
    color_filter: typing.Any | None = None
    erase_eol: bool = False


@dataclasses.dataclass
class PromptState:
    """Prompt / GA pacing state."""

    wait_fn: typing.Any | None = None
    echo: typing.Any | None = None
    ready: typing.Any | None = None
    repaint_input: typing.Any | None = None


@dataclasses.dataclass
class CommandState:
    """Sent-command history and waiting infrastructure for scripts."""

    history: collections.deque[str] = dataclasses.field(default_factory=lambda: collections.deque(maxlen=200))
    waiters: list[asyncio.Future[str]] = dataclasses.field(default_factory=list)
    buf: collections.deque[str] = dataclasses.field(default_factory=lambda: collections.deque(maxlen=32))
    ever_had_waiter: bool = False

    def record(self, cmd: str) -> None:
        """Record a sent command; update history and wake any waiting scripts."""
        if not cmd.strip():
            return
        self.history.append(cmd)
        pending = self.waiters
        self.waiters = []
        if pending:
            for fut in pending:
                if not fut.done():
                    fut.set_result(cmd)
        elif self.ever_had_waiter:
            self.buf.append(cmd)


@dataclasses.dataclass
class ScriptState:
    """Scripting engine state."""

    manager: typing.Any | None = None


class TelixSessionContext(telnetlib3._session_context.TelnetSessionContext):
    """
    Per-connection runtime state for a MUD client session.

    Extends :class:`~telnetlib3._session_context.TelnetSessionContext` with
    MUD-specific state grouped into typed sub-objects.
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
        input_filter: typing.Any | None = None,
        trigger_engine: typing.Any | None = None,
        trigger_wait_fn: Callable[..., Awaitable[None]] | None = None,
        typescript_file: typing.IO[str] | None = None,
        gmcp_data: dict[str, typing.Any] | None = None,
        color_args: argparse.Namespace | None = None,
    ):
        """Initialize session context with default state."""
        super().__init__(
            raw_mode=raw_mode,
            ascii_eol=ascii_eol,
            input_filter=input_filter,
            autoreply_engine=trigger_engine,
            autoreply_wait_fn=trigger_wait_fn,
            typescript_file=typescript_file,
            gmcp_data=gmcp_data,
        )

        # back-reference to the writer (set by session_shell)
        self.writer = writer
        self.encoding = encoding or (getattr(writer, "encoding", "") if writer else "") or "utf-8"

        # identity
        self.session_key: str = session_key

        # sub-objects grouping flat attributes
        self.room = RoomState()
        self.walk = WalkState()
        self.macros = MacroState()
        self.triggers = TriggerState(engine=trigger_engine, wait_fn=trigger_wait_fn)
        self.highlights = HighlightState()
        self.chat = ChatState()
        self.gmcp = GmcpState()
        self.progress = ProgressState()
        self.repl = ReplState()
        self.prompt = PromptState()
        self.scripts = ScriptState()
        self.commands = CommandState()

        # asyncio.Event must be created after init (needs a running loop in some cases)
        # but dataclass default_factory handles this fine for non-async contexts.
        # For safety, re-create it here so it always belongs to the current loop context.
        self.room.changed = asyncio.Event()

        # command queue (top-level)
        self.command_queue: CommandQueue | None = None

        # top-level callbacks
        self.on_trigger_activity: Callable[[], None] | None = None

        # MSLP link collector
        self.mslp_collector: mslp.MslpCollector = mslp.MslpCollector()

        # CLI color arguments
        self.color_args: argparse.Namespace | None = color_args

        # debounced timestamp persistence timer (top-level)
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
            gmcp_data=writer.ctx.gmcp_data,
            color_args=getattr(writer.ctx, "color_args", None),
        )

    def mark_macros_dirty(self) -> None:
        """Mark macros as needing a save and schedule a debounced flush."""
        self.macros.dirty = True
        self.schedule_flush()

    def mark_triggers_dirty(self) -> None:
        """Mark triggers as needing a save and schedule a debounced flush."""
        self.triggers.dirty = True
        self.schedule_flush()

    def mark_gmcp_dirty(self) -> None:
        """Mark GMCP snapshot as needing a save and schedule a debounced flush."""
        self.gmcp.dirty = True
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
        """Persist macro/trigger timestamps if dirty."""
        if self.macros.dirty and self.macros.file and self.macros.defs:
            macros.save_macros(self.macros.file, self.macros.defs, self.session_key)
            self.macros.dirty = False
        if self.triggers.dirty and self.triggers.file and self.triggers.rules:
            trigger.save_triggers(self.triggers.file, self.triggers.rules, self.session_key)
            self.triggers.dirty = False
        if self.gmcp.dirty and self.gmcp.snapshot_file and self.gmcp_data:
            gmcp_snapshot.save_gmcp_snapshot(self.gmcp.snapshot_file, self.session_key, self.gmcp_data)
            self.gmcp.dirty = False

    def close(self) -> None:
        """Cancel pending tasks and flush dirty timestamps."""
        for task in (self.walk.discover_task, self.walk.randomwalk_task):
            if task is not None and not task.done():
                task.cancel()
        self.walk.discover_task = None
        self.walk.randomwalk_task = None
        if self.scripts.manager is not None:
            self.scripts.manager.stop_script(None)
            self.scripts.manager = None
        if self.save_timer is not None:
            self.save_timer.cancel()
            self.save_timer = None
        self.flush_timestamps()
        if self.room.graph is not None:
            self.room.graph.close()
            self.room.graph = None
        self.typescript_file = None
        self.prompt.echo = None
