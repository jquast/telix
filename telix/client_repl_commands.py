"""Command expansion, queuing, chained command sending, and macro execution."""

# std imports
import re
import enum
import time
import typing
import asyncio
import logging
import dataclasses
from typing import TYPE_CHECKING
from collections.abc import Callable, Awaitable

from .repl_theme import hex_to_rgb, get_repl_palette

# local
from .client_repl_render import ELLIPSIS, get_term, wcswidth, write_hint, session_key

if TYPE_CHECKING:
    from .session_context import CommandQueue, SessionContext

REPEAT_RE = re.compile(r"^(\d+)([A-Za-z].*)$")
BACKTICK_RE = re.compile(r"`[^`]*`")
# NUL-bracketed sentinels for backslash-escaped separators (\; \| \` \\),
# swapped in before splitting and restored afterward.
ESCAPE_RE = re.compile(r"\\([;|`\\])")
ESC_MAP = {";": "\x00ES\x00", "|": "\x00EP\x00", "`": "\x00EB\x00", "\\": "\x00EBS\x00"}
ESC_RESTORE = {v: k for k, v in ESC_MAP.items()}

DELAY_RE = re.compile(r"^`delay\s+(\d+(?:\.\d+)?)(ms|s)`$")
WHEN_RE = re.compile(r"^`when\s+(\w+%?)\s*(>=|<=|>|<|=)\s*(\d+)`$", re.IGNORECASE)
UNTIL_RE = re.compile(r"^`until(?:\s+(\d+(?:\.\d+)?))?\s+(.+)`$")
UNTILS_RE = re.compile(r"^`untils(?:\s+(\d+(?:\.\d+)?))?\s+(.+)`$")


class StepResult(enum.Enum):
    """Outcome of a single :func:`dispatch_one` call."""

    SENT = "sent"
    HANDLED = "handled"
    ABORT = "abort"


@dataclasses.dataclass
class DispatchHooks:
    """
    Caller-specific callbacks for :func:`dispatch_one`.

    :param ctx: Session context.
    :param log: Logger instance.
    :param wait_fn: Async callable to wait for GA/EOR prompt.
    :param send_fn: Callable that sends a command string to the server.
    :param echo_fn: Callable that echoes a command for display.
    :param on_status: Callback to set a status string, or ``None``.
    :param on_progress: Callback ``(start, deadline)`` to set progress bar, or ``None``.
    :param on_progress_clear: Callback to clear the progress bar, or ``None``.
    :param on_activity: Callback to signal activity (e.g. toolbar refresh), or ``None``.
    :param prompt_ready: Event to clear before waiting for the next prompt.
    """

    ctx: "SessionContext"
    log: logging.Logger
    wait_fn: Callable[[], Awaitable[None]] | None
    send_fn: Callable[[str], None]
    echo_fn: Callable[[str], None] | None
    on_status: Callable[[str], None] | None = None
    on_progress: Callable[[float, float], None] | None = None
    on_progress_clear: Callable[[], None] | None = None
    on_activity: Callable[[], None] | None = None
    prompt_ready: asyncio.Event | None = None
    search_buffer: typing.Any | None = None


class ExpandedCommands(typing.NamedTuple):
    """
    Result of :func:`expand_commands_ex`.

    :param commands: Flat list of individual commands.
    :param immediate_set: Indices of commands whose preceding separator was ``|`` (send immediately, no GA/EOR wait).
    """

    commands: list[str]
    immediate_set: frozenset[int]


def expand_commands_ex(line: str) -> ExpandedCommands:
    r"""
    Split *line* on ``;`` and ``|`` (outside backticks) and expand repeat prefixes.

    Whitespace around separators is optional, including newlines --
    ``a;b``, ``a ; b``, and ``a;\nb`` are all equivalent.

    Backtick-enclosed tokens (e.g. ```travel 123```, ```delay 1s```,
    ```until 4 died\\.```) are preserved verbatim -- they are not split
    on ``;`` or ``|`` and repeat expansion is not applied.

    ``;`` means *wait for GA/EOR* before the next command (default).
    ``|`` means *send immediately* without waiting.

    Backslash-escaped separators (``\;``, ``\|``, ``\```) produce the
    literal character without triggering a split.

    A segment like ``5e`` becomes ``['e', 'e', 'e', 'e', 'e']``.
    Only a leading integer followed immediately by an alphabetic
    character triggers expansion (e.g. ``5east`` -> 5 × ``east``).
    Segments without a leading digit are passed through unchanged.

    :param line: Raw user input line.
    :returns: :class:`ExpandedCommands` with commands and immediate indices.
    """
    # Protect backslash-escaped separators before any splitting.
    escaped = ESCAPE_RE.sub(lambda m: ESC_MAP[m.group(1)], line)

    placeholders: list[str] = []

    def replace_bt(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        return f"\x00BT{len(placeholders) - 1}\x00"

    protected = BACKTICK_RE.sub(replace_bt, escaped)

    # Split on ; and | while capturing the separator.
    SEP_RE = re.compile(r"([;|])")
    tokens = SEP_RE.split(protected)

    # tokens is alternating [segment, sep, segment, sep, ...].
    # Walk through tracking which separator precedes each segment.
    result: list[str] = []
    immediate_indices: set[int] = set()
    prev_sep = ";"  # first command has no preceding separator
    for tok_idx, tok in enumerate(tokens):
        if tok_idx % 2 == 1:
            # This is a separator.
            prev_sep = tok
            continue
        # This is a segment.
        stripped = tok.strip()
        if not stripped:
            continue

        # Restore backtick placeholders.
        while "\x00BT" in stripped:
            for i, orig in enumerate(placeholders):
                stripped = stripped.replace(f"\x00BT{i}\x00", orig)

        is_immediate = prev_sep == "|"

        if stripped.startswith("`") and stripped.endswith("`"):
            cmd_idx = len(result)
            result.append(stripped)
            if is_immediate:
                immediate_indices.add(cmd_idx)
            continue

        m = REPEAT_RE.match(stripped)
        if m:
            count = min(int(m.group(1)), 200)
            cmd = m.group(2)
            first_idx = len(result)
            result.extend([cmd] * count)
            if is_immediate:
                immediate_indices.add(first_idx)
        else:
            cmd_idx = len(result)
            result.append(stripped)
            if is_immediate:
                immediate_indices.add(cmd_idx)

    # Restore backslash-escaped characters in final commands.
    for i, cmd in enumerate(result):
        for placeholder, literal in ESC_RESTORE.items():
            if placeholder in cmd:
                cmd = cmd.replace(placeholder, literal)
        result[i] = cmd

    return ExpandedCommands(result, frozenset(immediate_indices))


def expand_commands(line: str) -> list[str]:
    """
    Split *line* on ``;`` and ``|`` (outside backticks) and expand repeat prefixes.

    Convenience wrapper around :func:`expand_commands_ex` that returns
    only the command list (discarding separator metadata).

    :param line: Raw user input line.
    :returns: Flat list of individual commands.
    """
    return expand_commands_ex(line).commands


def get_search_buffer(ctx: "SessionContext") -> typing.Any | None:
    """
    Return the :class:`SearchBuffer` for *ctx*, or ``None``.

    Works for both macro execution (where ``ctx.autoreply_engine`` is the
    engine) and autoreply execution (where the engine *is* ``self`` and
    ``ctx.autoreply_engine`` points to it).

    :param ctx: Session context.
    :returns: The engine's :class:`SearchBuffer`, or ``None``.
    """
    engine = ctx.autoreply_engine
    if engine is not None:
        return engine.buffer
    return None


async def dispatch_one(
    cmd: str, idx: int, sent_count: int, immediate_set: frozenset[int], hooks: DispatchHooks, mask_send: bool = False
) -> StepResult:
    """
    Dispatch a single backtick command or plain send.

    Handles ``delay``, ``when``, ``until``, ``untils``, and plain send
    commands.  Caller keeps its own loop structure.

    :param cmd: Command string (may be backtick-enclosed).
    :param idx: Index of this command in the expanded sequence.
    :param sent_count: Number of plain sends already issued.
    :param immediate_set: Indices of commands that skip GA/EOR wait.
    :param hooks: Caller-specific callbacks.
    :param mask_send: If ``True``, mask plain command text in status.
    :returns: :class:`StepResult` indicating what happened.
    """
    from .autoreply import check_condition  # noqa: PLC0415 - circular

    dm = DELAY_RE.match(cmd)
    if dm:
        value = float(dm.group(1))
        unit = dm.group(2)
        delay = value / 1000.0 if unit == "ms" else value
        if delay > 0:
            if hooks.on_status is not None:
                hooks.on_status(f"delay {cmd.strip()}")
            now = time.monotonic()
            if hooks.on_progress is not None:
                hooks.on_progress(now, now + delay)
            if hooks.on_activity is not None:
                hooks.on_activity()
            await asyncio.sleep(delay)
            if hooks.on_progress_clear is not None:
                hooks.on_progress_clear()
        return StepResult.HANDLED

    wm = WHEN_RE.match(cmd)
    if wm:
        vital, op, val = wm.group(1), wm.group(2), wm.group(3)
        if hooks.on_status is not None:
            hooks.on_status(f"when {vital}{op}{val}")
        ok, desc = check_condition({vital: f"{op}{val}"}, hooks.ctx)
        if not ok:
            hooks.log.info("%s: when condition failed: %s", hooks.log.name, desc)
            if hooks.on_status is not None:
                hooks.on_status("")
            return StepResult.ABORT
        return StepResult.HANDLED

    um = UNTIL_RE.match(cmd)
    if um:
        timeout = float(um.group(1) or "4")
        pattern_str = um.group(2)
        if hooks.on_status is not None:
            hooks.on_status(f"until /{pattern_str}/")
        buf = hooks.search_buffer or get_search_buffer(hooks.ctx)
        if buf is not None:
            now = time.monotonic()
            if hooks.on_progress is not None:
                hooks.on_progress(now, now + timeout)
            if hooks.on_activity is not None:
                hooks.on_activity()
            compiled = re.compile(pattern_str, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            match = await buf.wait_for_pattern(compiled, timeout)
            if hooks.on_progress_clear is not None:
                hooks.on_progress_clear()
            if match is None:
                hooks.log.info("%s: until timed out for %r", hooks.log.name, pattern_str)
                if hooks.on_status is not None:
                    hooks.on_status("")
                return StepResult.ABORT
        return StepResult.HANDLED

    us = UNTILS_RE.match(cmd)
    if us:
        timeout = float(us.group(1) or "4")
        pattern_str = us.group(2)
        if hooks.on_status is not None:
            hooks.on_status(f"untils /{pattern_str}/")
        buf = hooks.search_buffer or get_search_buffer(hooks.ctx)
        if buf is not None:
            now = time.monotonic()
            if hooks.on_progress is not None:
                hooks.on_progress(now, now + timeout)
            if hooks.on_activity is not None:
                hooks.on_activity()
            compiled = re.compile(pattern_str, re.MULTILINE | re.DOTALL)
            match = await buf.wait_for_pattern(compiled, timeout)
            if hooks.on_progress_clear is not None:
                hooks.on_progress_clear()
            if match is None:
                hooks.log.info("%s: untils timed out for %r", hooks.log.name, pattern_str)
                if hooks.on_status is not None:
                    hooks.on_status("")
                return StepResult.ABORT
        return StepResult.HANDLED

    if sent_count > 0 and idx not in immediate_set:
        if hooks.on_status is not None:
            hooks.on_status("waiting for prompt")
        if hooks.prompt_ready is not None:
            hooks.prompt_ready.clear()
        if hooks.wait_fn is not None:
            await hooks.wait_fn()
    if hooks.on_status is not None:
        hooks.on_status("send: (masked)" if mask_send else f"send: {cmd}")
    hooks.send_fn(cmd)
    if hooks.echo_fn is not None:
        hooks.echo_fn(cmd)
    return StepResult.SENT


TRAVEL_RE = re.compile(r"^`(travel|return" r"|autodiscover|randomwalk|resume|home)\s*(.*?)`$", re.IGNORECASE)

COMMAND_DELAY = 0.25
MOVE_MAX_RETRIES = 2


def is_known_exit(cmd: str, ctx: "SessionContext") -> bool:
    """
    Return ``True`` if *cmd* matches a known exit from the current room.

    Falls back to ``True`` when room data is unavailable so that
    movement pacing is used conservatively.
    """
    room_num = ctx.current_room_num
    if not room_num:
        return True
    graph = ctx.room_graph
    if graph is None:
        return True
    adj = getattr(graph, "adj", None)
    if adj is None:
        return True
    exits = adj.get(room_num)
    if exits is None:
        return True
    return cmd in exits


def collapse_runs(commands: list[str], start: int = 0) -> list[tuple[str, int, int]]:
    """
    Collapse consecutive identical commands into display groups.

    :param commands: Full command list.
    :param start: Index to start collapsing from (earlier entries are skipped).
    :returns: List of ``(display_text, start_idx, end_idx)`` tuples.
    """
    if start >= len(commands):
        return []
    runs: list[tuple[str, int, int]] = []
    i = start
    while i < len(commands):
        cmd = commands[i]
        j = i
        while j + 1 < len(commands) and commands[j + 1] == cmd:
            j += 1
        count = j - i + 1
        text = f"{count}\u00d7{cmd}" if count > 1 else cmd
        runs.append((text, i, j))
        i = j + 1
    return runs


def active_cmd_fg() -> str:
    """Return the active command foreground color from the palette."""
    return get_repl_palette(session_key)["secondary"]


def pending_cmd_rgb() -> tuple[int, int, int]:
    """Return the pending command RGB color from the palette."""
    return hex_to_rgb(get_repl_palette(session_key)["pending_cmd"])


def render_active_command(
    command: str,
    scroll: "typing.Any",
    out: "asyncio.StreamWriter",
    flash_elapsed: float = -1.0,
    hint: str = "",
    progress: float | None = None,
    base_bg_sgr: str = "",
    autoreply: bool = False,
) -> int:
    """
    Render a single highlighted active command on the input row.

    The text is drawn in the secondary palette colour with no background
    flash animation.

    :param flash_elapsed: Unused (kept for API compatibility).
    :param hint: Right-aligned dim hint text (e.g. autoreply status).
    :param progress: Until timer progress ``0.0..1.0``, or ``None``.
    :param base_bg_sgr: Background SGR for the input row.
    :param autoreply: Use autoreply suggestion color for hints.
    :returns: Display width of the rendered command text.
    """
    blessed_term = get_term()
    cols = blessed_term.width
    normal = blessed_term.normal
    fg_sgr = str(blessed_term.color_hex(active_cmd_fg()))
    bg_sgr = base_bg_sgr

    hint_w = len(hint) if hint else 0
    avail = cols - hint_w
    text = command[: avail - 1] if wcswidth(command) >= avail else command
    w = wcswidth(text)

    out.write(blessed_term.move_yx(scroll.input_row, 0).encode())
    out.write(f"{fg_sgr}{bg_sgr}{text}{normal}".encode())
    pad = avail - w
    if pad > 0:
        out.write(f"{base_bg_sgr}{' ' * pad}{normal}".encode())
    write_hint(hint, out, blessed_term, progress=progress, bg_sgr=base_bg_sgr, autoreply=autoreply)
    out.write(normal.encode())
    return w


def clear_command_queue(ctx: "SessionContext") -> None:
    """Remove the command queue from *ctx* when chained send completes."""
    cq = ctx.command_queue
    if cq is not None:
        ctx.command_queue = None
        ctx.active_command = None


def render_command_queue(
    queue: "CommandQueue | None",
    scroll: "typing.Any",
    out: "asyncio.StreamWriter",
    flash_elapsed: float = -1.0,
    hint: str = "",
    progress: float | None = None,
    base_bg_sgr: str = "",
    autoreply: bool = False,
) -> int:
    """
    Render the command queue on the input row.

    The active run uses the secondary palette colour.  Pending runs use
    dim grey.  If the display is too wide it is truncated with an
    ellipsis.

    :param flash_elapsed: Unused (kept for API compatibility).
    :param hint: Right-aligned dim hint text (e.g. autoreply status).
    :param progress: Until timer progress ``0.0..1.0``, or ``None``.
    :param base_bg_sgr: Background SGR for the input row.
    :param autoreply: Use autoreply suggestion color for hints.
    :returns: Total display width of all rendered fragments.
    """
    if queue is None:
        return 0
    blessed_term = get_term()
    cols = blessed_term.width
    hint_w = len(hint) if hint else 0
    avail = cols - hint_w

    runs = collapse_runs(queue.commands, queue.current_idx)
    if not runs:
        return 0

    active_fg = str(blessed_term.color_hex(active_cmd_fg()))
    active_bg = base_bg_sgr
    pending_sgr = str(blessed_term.color_rgb(*pending_cmd_rgb()))
    normal = blessed_term.normal

    # Build fragments: (sgr, text) for each run.
    frags: list[tuple[str, str]] = []
    for text, start_idx, end_idx in runs:
        is_active = start_idx <= queue.current_idx <= end_idx
        sgr = f"{active_fg}{active_bg}" if is_active else pending_sgr
        frags.append((sgr, text))

    sep = " "
    total_w = 0
    built: list[tuple[str, str]] = []
    for idx, (sgr, text) in enumerate(frags):
        w = wcswidth(text) + (1 if idx > 0 else 0)
        if total_w + w > avail - 1 and built:
            built.append((pending_sgr, ELLIPSIS))
            total_w += 1
            break
        if idx > 0:
            built.append(("", sep))
        built.append((sgr, text))
        total_w += w

    out.write(blessed_term.move_yx(scroll.input_row, 0).encode())
    for sgr, text in built:
        out.write(f"{sgr}{text}{normal}".encode())
    pad = avail - total_w
    if pad > 0:
        out.write(f"{base_bg_sgr}{' ' * pad}{normal}".encode())
    write_hint(hint, out, blessed_term, progress=progress, bg_sgr=base_bg_sgr, autoreply=autoreply)
    out.write(normal.encode())
    return total_w


async def send_chained(
    commands: list[str],
    ctx: "SessionContext",
    log: logging.Logger,
    queue: "CommandQueue | None" = None,
    immediate_set: frozenset[int] = frozenset(),
) -> None:
    """
    Send multiple commands with GA/EOR pacing between each.

    The first command is assumed to have already been sent by the caller.
    This coroutine sends commands 2..N, waiting for the server prompt
    signal before each one.

    Commands whose index is in *immediate_set* (from a ``|`` separator)
    skip the GA/EOR wait and are sent immediately.

    When all commands in the list are identical (e.g. ``9e`` expanded to
    nine ``e`` commands), movement retry logic is applied: if the room
    does not change after a command, the same command is retried up to
    :data:`MOVE_MAX_RETRIES` times with a delay between attempts.

    :param commands: List of commands (index 1+ will be sent).
    :param ctx: Session context.
    :param log: Logger.
    :param queue: Optional command queue for display and cancellation.
    :param immediate_set: Indices of commands that skip GA/EOR wait.
    """
    wait_fn = ctx.wait_for_prompt
    echo_fn = ctx.echo_command
    prompt_ready = ctx.prompt_ready
    room_changed = ctx.room_changed

    is_repeated = len(commands) > 1 and len(set(commands)) == 1

    async def cancellable_sleep(delay: float) -> bool:
        """Sleep for *delay* seconds, returning ``True`` if cancelled."""
        if queue is None:
            await asyncio.sleep(delay)
            return False
        try:
            await asyncio.wait_for(queue.cancel_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    # Learned during this batch: True if cmd caused a room change,
    # False if it did not.  Unset commands fall back to is_known_exit.
    moves_room: dict[str, bool] = {}

    for idx, cmd in enumerate(commands[1:], 1):
        if queue is not None:
            if queue.cancelled:
                return
            queue.current_idx = idx
            queue.render()

        dm = DELAY_RE.match(cmd)
        if dm:
            value = float(dm.group(1))
            unit = dm.group(2)
            delay = value / 1000.0 if unit == "ms" else value
            if delay > 0:
                if await cancellable_sleep(delay):
                    return
            continue

        # Detect runs of identical commands (e.g. "9e;6n" expands to
        # e,e,...,n,n,...) -- these need movement pacing even in mixed
        # lists.  A command is "repeated" if it matches the previous one.
        prev_cmd = commands[idx - 1] if idx > 0 else ""
        is_run = is_repeated or cmd == prev_cmd
        use_move_pacing = is_run and moves_room.get(cmd, is_known_exit(cmd, ctx))
        prev_room = ctx.current_room_num if use_move_pacing else ""

        if not use_move_pacing:
            if is_run:
                # Repeated non-movement command (e.g. "10buy coffee"):
                # pace with a fixed delay, no GA/EOR wait needed.
                if await cancellable_sleep(COMMAND_DELAY):
                    return
            elif idx not in immediate_set:
                # Mixed commands: GA/EOR pacing.
                if prompt_ready is not None:
                    prompt_ready.clear()
                if wait_fn is not None:
                    await wait_fn()
            log.debug("chained command: %r", cmd)
            if echo_fn is not None:
                echo_fn(cmd)
            ctx.active_command_time = time.monotonic()
            if ctx.cx_dot is not None:
                ctx.cx_dot.trigger()
            if ctx.tx_dot is not None:
                ctx.tx_dot.trigger()
            ctx.writer.write(cmd + "\r\n")  # type: ignore[arg-type]
            ts = ctx.typescript_file
            if ts is not None and ctx.writer is not None and not ctx.writer.will_echo:
                ts.write(cmd + "\r\n")
                ts.flush()
            continue

        # Repeated commands: delay + room-change pacing with retry.
        for attempt in range(MOVE_MAX_RETRIES + 1):
            if queue is not None and queue.cancelled:
                return
            # Always delay -- the first repeated command needs spacing
            # from the caller's initial send, and retries need a longer
            # back-off to respect the server's rate limit.
            delay = COMMAND_DELAY if attempt == 0 else 1.0
            if await cancellable_sleep(delay):
                return
            if room_changed is not None:
                room_changed.clear()
            if prompt_ready is not None:
                prompt_ready.clear()
            if attempt == 0:
                log.debug("chained command: %r", cmd)
                if echo_fn is not None:
                    echo_fn(cmd)
            else:
                log.info("chained retry %d: %r", attempt, cmd)
            ctx.active_command_time = time.monotonic()
            if ctx.cx_dot is not None:
                ctx.cx_dot.trigger()
            if ctx.tx_dot is not None:
                ctx.tx_dot.trigger()
            ctx.writer.write(cmd + "\r\n")  # type: ignore[arg-type]
            ts = ctx.typescript_file
            if ts is not None and ctx.writer is not None and not ctx.writer.will_echo:
                ts.write(cmd + "\r\n")
                ts.flush()

            if not prev_room:
                break

            # Wait briefly for room change -- GMCP typically arrives
            # within 100-200ms.  A short timeout keeps movement brisk
            # while still detecting rate-limit rejections.
            actual = ctx.current_room_num
            if actual != prev_room:
                moves_room[cmd] = True
                break
            if room_changed is not None:
                try:
                    await asyncio.wait_for(room_changed.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                actual = ctx.current_room_num
            if actual != prev_room:
                moves_room[cmd] = True
                break

            # First attempt didn't change rooms -- this command is
            # not movement (e.g. "buy coffee").  Record the result
            # and fall back to GA/EOR pacing for subsequent repeats.
            if attempt == 0 and cmd not in moves_room:
                moves_room[cmd] = False
                log.debug("room unchanged after %r, switching to prompt pacing", cmd)
                break

            if attempt < MOVE_MAX_RETRIES:
                log.info("room unchanged after %r, retrying (%d/%d)", cmd, attempt + 1, MOVE_MAX_RETRIES)
            else:
                log.warning("room unchanged after %r, giving up after %d retries", cmd, MOVE_MAX_RETRIES)
                return


def macro_send(ctx: "SessionContext", log: logging.Logger, cmd: str) -> None:
    """
    Send a single command for macro execution.

    :param ctx: Session context.
    :param log: Logger.
    :param cmd: Command text.
    """
    log.info("macro: sending %r", cmd)
    if ctx.writer is not None and getattr(ctx.writer, "will_echo", False):
        ctx.active_command = "\u2593" * len(cmd)
    else:
        ctx.active_command = cmd
    ctx.active_command_time = time.monotonic()
    if ctx.cx_dot is not None:
        ctx.cx_dot.trigger()
    if ctx.tx_dot is not None:
        ctx.tx_dot.trigger()
    ctx.writer.write(cmd + "\r\n")  # type: ignore[arg-type]


async def execute_macro_commands(text: str, ctx: "SessionContext", log: logging.Logger) -> None:
    """
    Execute a macro text string, handling travel, delay, when, and until commands.

    Expands the text with :func:`expand_commands_ex`, then processes each
    part -- backtick-enclosed travel commands are routed through
    :func:`handle_travel_commands`, delay/when/until/untils commands are
    dispatched via :func:`dispatch_one`, and plain commands are sent to
    the server with GA/EOR pacing (or immediately if ``|`` separated).

    :param text: Raw macro text with ``;``/``|`` separators.
    :param ctx: Session context.
    :param log: Logger.
    """
    from .client_repl_travel import handle_travel_commands  # noqa: PLC0415 - circular

    expanded = expand_commands_ex(text)
    parts = list(expanded.commands)
    immediate_set = expanded.immediate_set
    if not parts:
        return

    ctx.macro_start_room = ctx.current_room_num

    hooks = DispatchHooks(
        ctx=ctx,
        log=log,
        wait_fn=ctx.wait_for_prompt,
        send_fn=lambda cmd: macro_send(ctx, log, cmd),
        echo_fn=ctx.echo_command,
        prompt_ready=ctx.prompt_ready,
        search_buffer=get_search_buffer(ctx),
    )
    sent_count = 0

    idx = 0
    while idx < len(parts):
        cmd = parts[idx]

        if TRAVEL_RE.match(cmd):
            remainder = await handle_travel_commands(parts[idx:], ctx, log)
            parts = remainder
            immediate_set = frozenset()
            idx = 0
            sent_count = 0
            continue

        result = await dispatch_one(cmd, idx, sent_count, immediate_set, hooks)
        if result is StepResult.ABORT:
            break
        if result is StepResult.SENT:
            sent_count += 1
        idx += 1

    ctx.macro_start_room = ""
