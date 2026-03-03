"""REPL and TUI components for linemode telnet client sessions."""

# std imports
import os
import re
import sys
import time
import typing
import asyncio
import logging
import termios
import contextlib
import collections
from typing import TYPE_CHECKING
from collections.abc import Callable, Generator

# 3rd party
import blessed
import telnetlib3.telopt
import blessed.line_editor
import telnetlib3.client_shell

from .macros import build_macro_dispatch
from .autoreply import AutoreplyEngine
from .highlighter import HighlightEngine

# local
# Re-export from sub-modules so existing ``from .client_repl import X``
# in tests and other modules continues to work without changes.
# pylint: disable=unused-import,useless-import-alias
from .client_repl_render import (  # noqa: F401
    HOLD,
    PHASES,
    SEXTANT,
    WARM_UP,
    DMZ_CHAR,
    DURATION,
    ELLIPSIS,
    BAR_WIDTH,
    GLOW_DOWN,
    FLASH_HOLD,
    CURSOR_HIDE,
    CURSOR_SHOW,
    BAR_CAP_LEFT,
    SEXTANT_BITS,
    STYLE_NORMAL,
    BAR_CAP_RIGHT,
    CURSOR_STYLES,
    FLASH_RAMP_UP,
    CURSOR_DEFAULT,
    FLASH_DURATION,
    FLASH_INTERVAL,
    FLASH_RAMP_DOWN,
    SEPARATOR_WIDTH,
    STOPLIGHT_WIDTH,
    STYLE_AUTOREPLY,
    CURSOR_STEADY_BAR,
    CURSOR_BLINKING_BAR,
    CURSOR_STEADY_BLOCK,
    DEFAULT_CURSOR_STYLE,
    CURSOR_BLINKING_BLOCK,
    CURSOR_COLOR_RESET_OSC,
    CURSOR_STEADY_UNDERLINE,
    CURSOR_BLINKING_UNDERLINE,
    Stoplight,
    ActivityDot,
    ToolbarSlot,
    ToolbarRenderer,
    sgr_bg,
    sgr_fg,
    dmz_line,
    idle_rgb,
    lerp_hsv,
    lerp_rgb,
    wcswidth,
    fmt_value,
    segmented,
    vital_bar,
    cursor_osc,
    hsv_to_rgb,
    rgb_to_hsv,
    write_hint,
    flash_color,
    idle_ar_rgb,
    make_styles,
    vital_color,
    activity_hint,
    cursor_ar_osc,
    layout_toolbar,
    until_progress,
    center_truncate,
    set_session_key,
    editor_cursor_col,
    scramble_password,
)
from .client_repl_travel import (  # noqa: F401
    DEFAULT_WALK_LIMIT,
    randomwalk,
    fast_travel,
    autodiscover,
    handle_travel_commands,
)
from .client_repl_dialogs import (  # noqa: F401
    show_help,
    editor_active,
    editor_buffer,
    reload_macros,
    confirm_dialog,
    launch_tui_editor,
    randomwalk_dialog,
    launch_chat_viewer,
    reload_autoreplies,
    autodiscover_dialog,
    launch_room_browser,
    reload_progressbars,
    launch_unified_editor,
)
from .client_repl_commands import (  # noqa: F401  # noqa: F401
    DELAY_RE,
    REPEAT_RE,
    TRAVEL_RE,
    BACKTICK_RE,
    COMMAND_DELAY,
    MOVE_MAX_RETRIES,
    send_chained,
    collapse_runs,
    clear_command_queue,
    render_command_queue,
    render_active_command,
)
from .client_repl_commands import expand_commands as expand_commands
from .client_repl_commands import expand_commands_ex as expand_commands_ex
from .client_repl_commands import execute_macro_commands as execute_macro_commands
from .session_context import CommandQueue

# pylint: enable=unused-import,useless-import-alias

if TYPE_CHECKING:
    import blessed.keyboard

    from . import client_shell
    from .macros import Macro
    from .session_context import SessionContext

EDIT_THEME_RE = re.compile(r"^`edit\s+theme`$", re.IGNORECASE)

PASSWORD_CHAR = "\u273b"

log = logging.getLogger(__name__)


def load_history(history: "blessed.line_editor.LineHistory", path: str) -> None:
    """
    Populate *history* entries from a newline-delimited file.

    :param history: :class:`~blessed.line_editor.LineHistory` instance.
    :param path: Path to the history file.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line:
                    history.entries.append(line)
    except OSError:
        pass


def save_history_entry(line: str, path: str) -> None:
    """
    Append a single history *line* to the file at *path*.

    :param line: The line to persist.
    :param path: Path to the history file (created if absent).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# Number of bottom rows reserved for the input line + toolbar.
RESERVE_INITIAL = 1
RESERVE_WITH_TOOLBAR = 2

# Lazy blessed Terminal singleton -- created on first use.
# Both blessed.Terminal and client_shell.Terminal are named "Terminal"
# in their respective modules; ``blessed_term`` and ``tty_shell`` are
# used throughout to distinguish the two when both are in scope.
blessed_term: "blessed.Terminal | None" = None


def get_term() -> "blessed.Terminal":
    """Return the module-level blessed Terminal singleton."""
    global blessed_term
    if blessed_term is None:
        blessed_term = blessed.Terminal(force_styling=True)
    return blessed_term


@contextlib.contextmanager
def blocking_fds() -> Generator[None, None, None]:
    """
    Context manager to ensure FDs 0/1/2 are blocking for a subprocess.

    asyncio's ``connect_write_pipe`` sets ``O_NONBLOCK`` on the PTY file
    description.  A Textual subprocess inherits non-blocking FDs, which can
    cause its ``WriterThread`` to silently fail mouse-enable escape sequences.
    This saves and restores the blocking state around subprocess calls.
    """
    saved = {}
    for fd in (0, 1, 2):
        try:
            saved[fd] = os.get_blocking(fd)
            if not saved[fd]:
                os.set_blocking(fd, True)
        except OSError:
            pass
    try:
        yield
    finally:
        for fd, was_blocking in saved.items():
            try:
                if not was_blocking:
                    os.set_blocking(fd, False)
            except OSError:
                pass


def terminal_cleanup() -> str:
    """Reset SGR, cursor, alt-screen, mouse tracking, and bracketed paste."""
    blessed_term = get_term()
    return (
        str(blessed_term.normal)
        + str(blessed_term.cursor_normal)
        + str(blessed_term.exit_fullscreen)
        + "\x1b[?1000l"  # xterm -- disable basic mouse
        + "\x1b[?1002l"  # xterm -- disable button-event mouse
        + "\x1b[?1003l"  # xterm -- disable any-event mouse
        + "\x1b[?1006l"  # xterm -- disable SGR mouse ext
        + "\x1b[?1016l"  # xterm -- disable SGR-Pixel mouse ext
        + "\x1b[?2004l"  # xterm -- disable bracketed paste
        + "\x1b[?2048l"  # xterm -- disable in-band resize
        + "\x1b[r"  # DECSTBM -- reset scroll region to default
        + CURSOR_COLOR_RESET_OSC  # OSC 112 -- reset cursor color
        + "\x1b[<u"  # kitty -- disable kitty keyboard protocol
    )


# Maximum bytes retained in the output replay ring buffer for Ctrl-L repaint.
REPLAY_BUFFER_MAX = 65536

__all__ = ("ReplSession", "ScrollRegion", "repl_event_loop", "split_incomplete_esc")


def split_incomplete_esc(data: bytes) -> tuple[bytes, bytes]:
    """
    Split *data* into (complete, holdback) at a trailing incomplete escape.

    If *data* ends mid-escape-sequence the incomplete tail is returned as
    *holdback* so the caller can buffer it until more bytes arrive.
    Handles CSI (``ESC [``) with arbitrarily long parameter/intermediate
    bytes, OSC (``ESC ]``), DCS (``ESC P``), and plain two-byte ``ESC X``
    sequences.

    :returns: ``(flush_now, hold_back)`` -- concatenation equals *data*.
    """
    n = len(data)
    if n == 0:
        return data, b""

    idx = data.rfind(0x1B)
    if idx == -1:
        return data, b""

    pos = idx + 1

    if pos >= n:
        # Lone ESC at the very end.
        return data[:idx], data[idx:]

    nxt = data[pos]

    if nxt == 0x5B:  # '[' -- CSI
        pos += 1
        # Parameter bytes 0x30-0x3F, intermediate bytes 0x20-0x2F.
        while pos < n and 0x20 <= data[pos] <= 0x3F:
            pos += 1
        # Final byte 0x40-0x7E completes the sequence.
        if pos < n and 0x40 <= data[pos] <= 0x7E:
            return data, b""
        return data[:idx], data[idx:]

    if nxt in (0x5D, 0x50):  # ']' OSC  /  'P' DCS
        # Terminated by BEL (0x07) or ST (ESC \).
        while pos < n:
            if data[pos] == 0x07:
                return data, b""
            if data[pos] == 0x1B and pos + 1 < n and data[pos + 1] == 0x5C:
                return data, b""
            pos += 1
        return data[:idx], data[idx:]

    if 0x40 <= nxt <= 0x5F:
        # Two-byte escape -- already complete (Fe sequence).
        return data, b""

    # Unknown sequence type; assume complete.
    return data, b""


class OutputRingBuffer:
    """Rolling buffer of raw display output for Ctrl-L screen repaint."""

    def __init__(self, max_bytes: int = REPLAY_BUFFER_MAX) -> None:
        self.chunks: collections.deque[bytes] = collections.deque()
        self.total = 0
        self.max = max_bytes

    def append(self, data: bytes) -> None:
        """Append a chunk, discarding oldest data when over capacity."""
        self.chunks.append(data)
        self.total += len(data)
        while self.total > self.max and self.chunks:
            removed = self.chunks.popleft()
            self.total -= len(removed)

    def replay(self) -> bytes:
        """Return all buffered output concatenated."""
        return b"".join(self.chunks)


# Set by restore_after_subprocess so the main event loop can defer
# re-enabling in-band resize (DEC 2048) until the post-action render
# is complete and stale terminal input has been flushed.
subprocess_needs_rearm = False


def restore_after_subprocess(replay_buf: "OutputRingBuffer | None", reserve: int = RESERVE_WITH_TOOLBAR) -> None:
    """
    Restore terminal state after a TUI subprocess exits.

    Restores stdin blocking mode, resets SGR/mouse/alt-screen via
    :func:`terminal_cleanup`, clears the screen, re-establishes the
    DECSTBM scroll region, replays the output ring buffer, and clears
    the reserved input rows.

    The cursor is left hidden and output is **not** flushed -- the
    caller's post-action render (input editor + toolbar) will show the
    cursor and flush once, avoiding the visible blink that separate
    flush cycles produce.

    :param replay_buf: Ring buffer to replay, or ``None`` to skip replay.
    :param reserve: Number of bottom rows reserved for the input area.
    """
    global subprocess_needs_rearm
    subprocess_needs_rearm = True
    try:
        os.set_blocking(sys.stdin.fileno(), True)
    except OSError:
        pass
    blessed_term = get_term()
    sys.stdout.write(CURSOR_HIDE)
    sys.stdout.write(terminal_cleanup())
    try:
        tsize = os.get_terminal_size()
    except OSError:
        tsize = os.terminal_size((80, 24))
    scroll_bottom = max(0, tsize.lines - reserve - 2)
    sys.stdout.write(blessed_term.clear + blessed_term.home)
    sys.stdout.write(blessed_term.change_scroll_region(0, scroll_bottom))
    sys.stdout.write(blessed_term.move_yx(0, 0))
    if replay_buf is not None:
        data = replay_buf.replay()
        if data:
            sys.stdout.write(data.decode("utf-8", errors="replace"))
    sys.stdout.write(blessed_term.save)
    dmz = scroll_bottom + 1
    input_row = tsize.lines - reserve
    if dmz < input_row:
        sys.stdout.write(blessed_term.move_yx(dmz, 0) + blessed_term.clear_eol + dmz_line(tsize.columns))
    for r in range(input_row, tsize.lines):
        sys.stdout.write(blessed_term.move_yx(r, 0) + blessed_term.clear_eol)
    # NOTE: in-band window resize (DEC mode 2048) is NOT re-enabled here.
    # Re-enabling it immediately causes the terminal to send a resize
    # notification that arrives before the main event loop is ready to
    # process it, producing a storm of redundant full-screen repaints.
    # Instead, the main loop re-enables it after the post-action render
    # is complete and stale input has been flushed.


def repaint_screen(
    replay_buf: OutputRingBuffer | None, scroll: "ScrollRegion | None" = None, active: bool = False
) -> None:
    """
    Clear screen and replay recent output from the ring buffer.

    Re-establishes the DECSTBM scroll region and replays buffered output so recent MUD text reappears with colors
    intact.

    :param active: Use gold DMZ color when autoreply/walk/discover is active.
    """
    reserve = scroll.reserve if scroll is not None else RESERVE_WITH_TOOLBAR
    try:
        tsize = os.get_terminal_size()
    except OSError:
        return
    if scroll is not None:
        scroll.update_size(tsize.lines, tsize.columns)
    fd = sys.stdout.fileno()
    was_blocking = os.get_blocking(fd)
    os.set_blocking(fd, True)
    try:
        blessed_term = get_term()
        scroll_bottom = max(0, tsize.lines - reserve - 2)
        sys.stdout.write(CURSOR_HIDE)
        sys.stdout.write(blessed_term.clear + blessed_term.home)
        sys.stdout.write(blessed_term.change_scroll_region(0, scroll_bottom))
        sys.stdout.write(blessed_term.move_yx(0, 0))
        if replay_buf is not None:
            data = replay_buf.replay()
            if data:
                sys.stdout.write(data.decode("utf-8", errors="replace"))
        sys.stdout.write(blessed_term.save)
        dmz = scroll_bottom + 1
        input_row = tsize.lines - reserve
        if dmz < input_row:
            sys.stdout.write(blessed_term.move_yx(dmz, 0) + blessed_term.clear_eol + dmz_line(tsize.columns, active))
        for r in range(input_row, tsize.lines):
            sys.stdout.write(blessed_term.move_yx(r, 0) + blessed_term.clear_eol)
        sys.stdout.write(blessed_term.move_yx(input_row, 0))
        sys.stdout.write(CURSOR_SHOW)
        sys.stdout.flush()
    finally:
        os.set_blocking(fd, was_blocking)


if sys.platform != "win32":
    import fcntl
    import struct
    import termios

    def get_terminal_size() -> tuple[int, int]:
        """Return ``(rows, cols)`` of the controlling terminal."""
        try:
            fmt = "hhhh"
            buf = b"\x00" * struct.calcsize(fmt)
            val = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, buf)
            rows, cols, _, _ = struct.unpack(fmt, val)
            return rows, cols
        except OSError:
            return (int(os.environ.get("LINES", "25")), int(os.environ.get("COLUMNS", "80")))

    class ScrollRegion:
        """
        Context manager that sets a VT100 scroll region (DECSTBM).

        Confines terminal output to the top portion, reserving
        the bottom line(s) for the REPL input.  Follows the same
        pattern as ``blessed.Terminal.scroll_region``.

        :param stdout: asyncio StreamWriter for local terminal output.
        :param rows: Total terminal height.
        :param cols: Total terminal width.
        :param reserve_bottom: Number of bottom lines to reserve.
        """

        def __init__(self, stdout: asyncio.StreamWriter, rows: int, cols: int, reserve_bottom: int = 1) -> None:
            """Initialize scroll region with output stream and dimensions."""
            self.stdout = stdout
            self.rows = rows
            self.cols = cols
            self.reserve = reserve_bottom
            self.active = False
            self.dirty = False

        @property
        def scroll_bottom(self) -> int:
            """0-indexed last row of the scroll region."""
            return max(0, self.rows - self.reserve - 2)

        @property
        def scroll_rows(self) -> int:
            """0-indexed last row of the scroll region (alias for scroll_bottom)."""
            return self.scroll_bottom

        @property
        def input_row(self) -> int:
            """0-indexed row for the input line."""
            return self.rows - self.reserve

        @property
        def resize_pending(self) -> bool:
            """Check and clear the resize-pending flag."""
            if self.dirty:
                self.dirty = False
                return True
            return False

        def grow_reserve(self, new_reserve: int) -> None:
            """
            Increase the reserved bottom area and reapply scroll region.

            Emits newlines inside the current scroll region first so that any server text on the rows about to be
            claimed is scrolled up rather than silently overwritten (e.g. a password prompt arriving just as the GMCP
            status bar appears).
            """
            if new_reserve <= self.reserve:
                return
            extra = new_reserve - self.reserve
            old_input_row = self.input_row
            blessed_term = get_term()
            if self.active:
                old_bottom = self.scroll_bottom
                self.stdout.write(blessed_term.move_yx(old_bottom, 0).encode())
                self.stdout.write(b"\n" * extra)
            self.reserve = new_reserve
            if self.active:
                for r in range(old_input_row, old_input_row + new_reserve):
                    self.stdout.write((blessed_term.move_yx(r, 0) + blessed_term.clear_eol).encode())
                self.set_scroll_region()
                self.stdout.write(blessed_term.restore.encode())
                if extra > 0:
                    self.stdout.write(blessed_term.move_up(extra).encode())
                self.stdout.write(blessed_term.save.encode())
                for r in range(self.input_row, self.input_row + new_reserve):
                    self.stdout.write((blessed_term.move_yx(r, 0) + blessed_term.clear_eol).encode())
                self.dirty = True

        def update_size(self, rows: int, cols: int) -> None:
            """
            Update dimensions and reapply scroll region.

            No content scrolling occurs here -- ``on_resize_repaint``
            replays the buffer and saves the cursor at the correct
            position afterward.
            """
            old_input_row = self.input_row
            self.rows = rows
            self.cols = cols
            blessed_term = get_term()
            if self.active:
                for r in range(old_input_row, old_input_row + self.reserve):
                    self.stdout.write((blessed_term.move_yx(r, 0) + blessed_term.clear_eol).encode())
                self.set_scroll_region()
                self.stdout.write(blessed_term.save.encode())
                for r in range(self.input_row, self.input_row + self.reserve):
                    self.stdout.write((blessed_term.move_yx(r, 0) + blessed_term.clear_eol).encode())
                self.dirty = True

        def set_scroll_region(self) -> None:
            """Write DECSTBM escape sequence to set scroll region."""
            blessed_term = get_term()
            bottom = self.scroll_bottom
            self.stdout.write(blessed_term.change_scroll_region(0, bottom).encode())
            dmz = bottom + 1
            if dmz < self.input_row:
                self.stdout.write(
                    (blessed_term.move_yx(dmz, 0) + blessed_term.clear_eol + dmz_line(self.cols)).encode()
                )
            self.stdout.write(blessed_term.move_yx(bottom, 0).encode())

        def reset_scroll_region(self) -> None:
            """Reset scroll region to full terminal height."""
            blessed_term = get_term()
            self.stdout.write(blessed_term.change_scroll_region(0, self.rows - 1).encode())

        def save_and_goto_input(self) -> None:
            """Save cursor, move to input line, clear it."""
            blessed_term = get_term()
            self.stdout.write(blessed_term.save.encode())
            self.stdout.write(blessed_term.move_yx(self.input_row, 0).encode())
            self.stdout.write(blessed_term.clear_eol.encode())

        def restore_cursor(self) -> None:
            """Restore cursor to saved position in scroll region."""
            self.stdout.write(get_term().restore.encode())

        def __enter__(self) -> "ScrollRegion":
            self.set_scroll_region()
            self.active = True
            return self

        def __exit__(self, *_: typing.Any) -> None:
            self.active = False
            self.reset_scroll_region()
            blessed_term = get_term()
            self.stdout.write(blessed_term.move_yx(self.rows - 1, 0).encode())

    import contextlib

    @contextlib.asynccontextmanager
    async def repl_scaffold(
        telnet_writer: "telnetlib3.TelnetWriter | telnetlib3.TelnetWriterUnicode",
        tty_shell: "client_shell.Terminal",
        stdout: asyncio.StreamWriter,
        reserve_bottom: int = 1,
        on_resize: "Callable[[int, int], None] | None" = None,
    ) -> "typing.Any":
        """
        Set up NAWS patch, scroll region, and resize handler.

        Yields ``(scroll, rows_cols)`` where *rows_cols* is a mutable
        ``[rows, cols]`` list kept up-to-date by the resize handler.
        Restores the original ``handle_send_naws`` in a ``finally`` block.

        :param on_resize: Optional extra callback invoked after scroll
            region update, receiving ``(new_rows, new_cols)``.
        """
        rows, cols = get_terminal_size()
        rows_cols = [rows, cols]
        scroll_region: ScrollRegion | None = None

        orig_send_naws = getattr(telnet_writer, "handle_send_naws", None)

        def adjusted_send_naws() -> tuple[int, int]:
            if scroll_region is not None and scroll_region.active:
                _, cur_cols = get_terminal_size()
                return (scroll_region.scroll_rows, cur_cols)
            return get_terminal_size()

        telnet_writer.handle_send_naws = adjusted_send_naws  # type: ignore[method-assign]

        try:
            if telnet_writer.local_option.enabled(telnetlib3.telopt.NAWS) and not telnet_writer.is_closing():
                telnet_writer._send_naws()

            with ScrollRegion(stdout, rows, cols, reserve_bottom=reserve_bottom) as scroll:
                scroll_region = scroll

                def handle_resize(new_rows: int, new_cols: int) -> None:
                    rows_cols[0] = new_rows
                    rows_cols[1] = new_cols
                    scroll.update_size(new_rows, new_cols)
                    if on_resize is not None:
                        on_resize(new_rows, new_cols)

                tty_shell.on_resize = handle_resize
                try:
                    yield scroll, rows_cols
                finally:
                    tty_shell.on_resize = None
        finally:
            if orig_send_naws is not None:
                telnet_writer.handle_send_naws = orig_send_naws  # type: ignore[method-assign]

    async def run_repl_tasks(server_coro: "typing.Any", input_coro: "typing.Any") -> None:
        """Run server and input coroutines; cancel the other when one finishes."""
        server_task = asyncio.ensure_future(server_coro)
        input_task = asyncio.ensure_future(input_coro)
        _, pending = await asyncio.wait([server_task, input_task], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    class KeyDispatch:
        """Route blessed keystrokes to hotkey handlers before the line editor."""

        def __init__(self) -> None:
            self.by_name: dict[str, Callable[..., typing.Any]] = {}
            self.by_seq: dict[str, Callable[..., typing.Any]] = {}

        def register(self, blessed_name: str, handler: Callable[..., typing.Any]) -> None:
            """Register a handler for a blessed key name."""
            self.by_name[blessed_name] = handler

        def register_seq(self, char: str, handler: Callable[..., typing.Any]) -> None:
            """Register a handler for a raw character sequence."""
            self.by_seq[char] = handler

        def set_macros(self, macros: "list[Macro]", ctx: "SessionContext", logger: logging.Logger) -> None:
            """Replace all macro bindings from a macro definition list."""
            macro_handlers = build_macro_dispatch(macros, ctx, logger)
            for key_name, handler in macro_handlers.items():
                if len(key_name) == 1:
                    self.by_seq[key_name] = handler
                else:
                    self.by_name[key_name] = handler

        def lookup(self, key: "blessed.keyboard.Keystroke") -> Callable[..., typing.Any] | None:
            """Look up a handler for a blessed Keystroke, or ``None``."""
            name = getattr(key, "name", None)
            if name and name in self.by_name:
                return self.by_name[name]
            key_str = str(key)
            if key_str in self.by_seq:
                return self.by_seq[key_str]
            return None

    LINE_HOLD_TIMEOUT = 0.15

    class LineHoldBuffer:
        r"""
        Hold back incomplete trailing lines from display.

        Server output split across TCP segments may arrive mid-line.  This
        buffer accumulates text and splits it into "ready to emit" (complete
        lines terminated by ``\n``) and a held-back trailing fragment.

        :param highlight_engine_getter: callable returning the current
            :class:`HighlightEngine` (or ``None``).
        """

        def __init__(self, highlight_engine_getter: Callable[[], typing.Any]) -> None:
            self._pending: str = ""
            self.get_engine = highlight_engine_getter

        def add(self, text: str) -> tuple[str, str]:
            r"""
            Accept new server text, return ``(emit_now, held_back)``.

            Complete lines (everything up to and including the last ``\n``) are run through the highlight engine and
            returned as *emit_now*. The trailing incomplete fragment is stored internally and returned as *held_back*
            (for the caller to decide whether to schedule a timer).
            """
            combined = self._pending + text
            nl_pos = combined.rfind("\n")
            if nl_pos == -1:
                self._pending = combined
                return ("", combined)
            emit_raw = combined[: nl_pos + 1]
            self._pending = combined[nl_pos + 1 :]
            emit_now = self.highlight_lines(emit_raw)
            return (emit_now, self._pending)

        def flush_raw(self) -> str:
            """Return and clear held text without highlight processing."""
            text = self._pending
            self._pending = ""
            return text

        def flush_for_prompt(self) -> str:
            """Return and clear held text with highlight processing."""
            text = self._pending
            self._pending = ""
            if not text:
                return ""
            return self.highlight_lines(text)

        @property
        def pending(self) -> str:
            """The currently held-back text."""
            return self._pending

        def highlight_lines(self, text: str) -> str:
            """Run each complete line through the highlight engine."""
            engine = self.get_engine()
            if engine is None or not engine.enabled:
                return text
            text, _ = engine.process_block(text)
            parts = text.split("\n")
            result: list[str] = []
            for i, part in enumerate(parts):
                is_last = i == len(parts) - 1
                if is_last:
                    if part:
                        highlighted, matched = engine.process_line(part)
                        result.append(highlighted)
                    else:
                        result.append(part)
                else:
                    highlighted, matched = engine.process_line(part)
                    result.append(highlighted)
            return "\n".join(result)

    class ReplSession:
        """
        Encapsulates the REPL event loop state and logic.

        Replaces the former ``repl_event_loop()`` monolithic function,
        converting captured locals and closures into explicit instance
        attributes and methods.

        :param telnet_reader: Server-side reader stream.
        :param telnet_writer: Server-side writer stream.
        :param tty_shell: ``Terminal`` instance from ``client_shell``.
        :param stdout: asyncio StreamWriter for local terminal output.
        :param history_file: Optional path for persistent line history.
        :param banner_lines: Lines to display after the scroll region is active.
        """

        def __init__(
            self,
            telnet_reader: "telnetlib3.TelnetReader | telnetlib3.TelnetReaderUnicode",
            telnet_writer: "telnetlib3.TelnetWriter | telnetlib3.TelnetWriterUnicode",
            tty_shell: "client_shell.Terminal",
            stdout: asyncio.StreamWriter,
            history_file: str | None = None,
            banner_lines: list[str] | None = None,
        ) -> None:
            """Initialize REPL session with telnet streams and TTY shell."""
            self.telnet_reader = telnet_reader
            self.telnet_writer = telnet_writer
            self.tty_shell = tty_shell
            self.stdout = stdout
            self.history_file = history_file
            self.banner_lines = banner_lines

            self.ctx: SessionContext = telnet_writer.ctx  # type: ignore[assignment]
            set_session_key(self.ctx.session_key)
            self.is_ssl = telnet_writer.get_extra_info("ssl_object") is not None
            self.conn_info = self.ctx.session_key + (" SSL" if self.is_ssl else "")

            self.mode_switched = False
            self.server_done = False
            self.ga_detected = False
            self.prompt_pending = False
            self.gmcp_keys_registered = False
            self.last_resize_size: list[int] = [0, 0]
            self.last_input_style: dict[str, str] | None = None
            self.scroll: ScrollRegion | None = None
            self.autoreply_engine: AutoreplyEngine | None = None
            self.ar_rules_ref: object = None
            self.prompt_ready = asyncio.Event()
            self.prompt_ready.set()

            # Late-initialized in init_* methods.
            self.blessed_term: blessed.Terminal = None  # type: ignore[assignment]
            self.replay_buf: OutputRingBuffer = None  # type: ignore[assignment]
            self.history: blessed.line_editor.LineHistory = None  # type: ignore[assignment]
            self.editor: blessed.line_editor.LineEditor = None  # type: ignore[assignment]
            self.stoplight: Stoplight = None  # type: ignore[assignment]
            self.toolbar: ToolbarRenderer = None  # type: ignore[assignment]
            self.dispatch: KeyDispatch = None  # type: ignore[assignment]
            self.macro_defs: list[Macro] | None = None
            self.loop: asyncio.AbstractEventLoop = None  # type: ignore[assignment]
            self.dialogs_mod: typing.Any = None
            self.line_hold: LineHoldBuffer = LineHoldBuffer(lambda: self.ctx.highlight_engine)
            self.line_hold_timer: asyncio.TimerHandle | None = None

        def init_terminal(self) -> None:
            """Import blessed, create terminal singleton, styles, replay buffer."""
            import telix.client_repl_dialogs as dialogs_mod  # noqa: PLC0415 - circular

            self.dialogs_mod = dialogs_mod
            self.loop = asyncio.get_event_loop()
            self.blessed_term = get_term()
            make_styles()
            self.replay_buf = OutputRingBuffer()

        def init_editor(self) -> None:
            """Create line history and editor."""
            self.history = blessed.line_editor.LineHistory()  # pylint: disable=no-member
            if self.history_file:
                load_history(self.history, self.history_file)

            term_cols = self.blessed_term.width
            editor_style = {k: v for k, v in STYLE_NORMAL.items() if k != "cursor_sgr"}
            self.editor = blessed.line_editor.LineEditor(  # pylint: disable=no-member
                history=self.history,
                password=bool(self.telnet_writer.will_echo),
                max_width=term_cols,
                limit=1024,
                keymap={},
                **editor_style,
            )

        def init_ui(self) -> None:
            """Create stoplight, toolbar, key dispatch, macros."""
            self.stoplight = Stoplight.create()
            self.ctx.tx_dot = self.stoplight.tx
            self.ctx.cx_dot = self.stoplight.cx
            self.ctx.rx_dot = self.stoplight.rx

            self.dispatch = KeyDispatch()
            self.macro_defs = self.ctx.macro_defs or None
            if self.macro_defs is not None:
                self.dispatch.set_macros(self.macro_defs, self.ctx, self.telnet_writer.log)
            self.ctx.key_dispatch = self.dispatch

        def echo_autoreply(self, cmd: str) -> None:
            """Echo an autoreply command into the scroll region."""
            assert self.scroll is not None
            is_pw = self.telnet_writer.will_echo
            display_cmd = scramble_password() if is_pw else cmd
            self.stdout.write(self.blessed_term.restore.encode())
            colored = f"{self.blessed_term.cyan}{display_cmd}{self.blessed_term.normal}\r\n"
            self.stdout.write(colored.encode())
            self.replay_buf.append(colored.encode())
            self.stdout.write(self.blessed_term.save.encode())
            ts = self.ctx.typescript_file
            if ts is not None:
                if is_pw:
                    ts.write("\r\n")
                else:
                    ts.write(cmd + "\r\n")
                ts.flush()
            cursor_col = self.editor_cursor()
            self.stdout.write(self.blessed_term.move_yx(self.scroll.input_row, cursor_col).encode())

        def render_editor(self, bt: "blessed.Terminal", row: int, width: int) -> str:
            """Render the line editor, scrambling password text."""
            raw = self.editor.render(bt, row, width)
            if self.editor.password_mode and self.editor._buf:
                raw = raw.replace(PASSWORD_CHAR * len(self.editor._buf), scramble_password())
            return raw

        def editor_cursor(self) -> int:
            """Return cursor column, pinned to scramble length in password mode."""
            return editor_cursor_col(self.editor)

        def insert_into_prompt(self, text: str) -> None:
            """Insert text into the line editor buffer."""
            self.editor.insert_text(text)

        def on_autoreply_activity(self) -> None:
            """Kick the toolbar progress ticker when an autoreply delay starts."""
            if self.autoreply_engine is None or self.scroll is None:
                return
            self.toolbar.schedule_until_progress(self.loop, self.autoreply_engine, self.editor, self.blessed_term)

        def on_prompt_signal(self, cmd: bytes) -> None:
            """
            Handle GA / EOR prompt signals.

            The prompt text typically appears in the same TCP segment as the
            IAC GA/EOR, so it hasn't been delivered to ``read_server`` yet
            when this callback fires.  We set ``prompt_pending`` and let the
            reader loop flush ``line_hold`` with highlight processing once
            the text has been added to the buffer.
            """
            self.ga_detected = True
            self.prompt_ready.set()
            self.prompt_pending = True
            self.telnet_reader._wakeup_waiter()

        async def wait_for_prompt(self) -> None:
            """Wait for a prompt signal if GA has been detected."""
            if not self.ga_detected:
                return
            try:
                await asyncio.wait_for(self.prompt_ready.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self.prompt_ready.clear()

        def refresh_autoreply_engine(self) -> None:
            """Rebuild the autoreply engine when rules change."""
            cur_rules = self.ctx.autoreply_rules or None
            if cur_rules is self.ar_rules_ref:
                return
            self.ar_rules_ref = cur_rules
            prev_enabled = self.autoreply_engine.enabled if self.autoreply_engine is not None else True
            if self.autoreply_engine is not None:
                self.autoreply_engine.cancel()
                self.autoreply_engine = None
            if cur_rules:
                self.autoreply_engine = AutoreplyEngine(
                    cur_rules,
                    self.ctx,
                    self.telnet_writer.log,
                    insert_fn=self.insert_into_prompt,
                    echo_fn=self.echo_autoreply,
                    wait_fn=self.wait_for_prompt,
                )
                self.autoreply_engine.enabled = prev_enabled
            self.ctx.autoreply_engine = self.autoreply_engine

        def refresh_highlight_engine(self) -> None:
            """Rebuild the highlight engine when rules or autoreplies change."""
            hl_rules = self.ctx.highlight_rules or []
            ar_rules = self.ctx.autoreply_rules or []
            prev_enabled = self.ctx.highlight_engine.enabled if self.ctx.highlight_engine is not None else True
            self.ctx.highlight_engine = HighlightEngine(hl_rules, ar_rules, self.blessed_term, self.ctx)
            self.ctx.highlight_engine.enabled = prev_enabled

        def cancel_line_hold_timer(self) -> None:
            """Cancel any pending line-hold flush timer."""
            if self.line_hold_timer is not None:
                self.line_hold_timer.cancel()
                self.line_hold_timer = None

        def schedule_line_hold_flush(self) -> None:
            """Schedule a timer to flush held-back text after timeout."""
            self.cancel_line_hold_timer()
            self.line_hold_timer = self.loop.call_later(LINE_HOLD_TIMEOUT, self.flush_line_hold_timer)

        def flush_line_hold_timer(self) -> None:
            """Timer callback: flush held text raw (no highlight processing)."""
            self.line_hold_timer = None
            text = self.line_hold.flush_raw()
            if not text:
                return
            bt = self.blessed_term
            self.stdout.write(bt.restore.encode())
            encoded = text.encode()
            self.stdout.write(encoded)
            self.replay_buf.append(encoded)
            self.stdout.write(bt.save.encode())
            if self.autoreply_engine is not None:
                self.autoreply_engine.feed(text)
            assert self.scroll is not None
            self.update_input_style()
            self.stdout.write(self.render_editor(bt, self.scroll.input_row, self.input_width()).encode())
            cursor_col = self.editor_cursor()
            self.show_cursor_or_light(self.scroll.input_row, cursor_col)

        def repaint_after_toggle(self) -> None:
            """Repaint toolbar and input line after a toggle key."""
            if self.scroll is None:
                return
            bt = self.blessed_term
            self.update_input_style()
            self.toolbar.hide_cursor()
            self.stdout.write(self.render_editor(bt, self.scroll.input_row, self.input_width()).encode())
            self.toolbar.render(self.autoreply_engine)
            cursor_col = self.editor_cursor()
            self.show_cursor_or_light(self.scroll.input_row, cursor_col)

        def toggle_highlights(self) -> None:
            """Toggle the highlight engine on/off."""
            engine = self.ctx.highlight_engine
            if engine is None:
                return
            engine.enabled = not engine.enabled
            state = "ON" if engine.enabled else "OFF"
            self.echo_autoreply(f"HIGHLIGHTS {state}")
            self.repaint_after_toggle()

        def reg_close(self) -> None:
            """Handle Ctrl+] -- close the connection."""
            self.server_done = True
            self.telnet_writer.close()

        def has_gmcp(self) -> bool:
            """Return whether GMCP data is available."""
            return bool(self.ctx.gmcp_data)

        def toggle_autoreplies(self) -> None:
            """Toggle the autoreply engine on/off."""
            if self.autoreply_engine is None:
                return
            self.autoreply_engine.enabled = not self.autoreply_engine.enabled
            state = "ON" if self.autoreply_engine.enabled else "OFF"
            self.echo_autoreply(f"AUTOREPLIES {state}")
            self.repaint_after_toggle()

        def on_walk_done(self, task: "asyncio.Task[None]") -> None:
            """Repaint the input line when a walk task finishes."""
            if self.scroll is None:
                return
            bt = self.blessed_term
            self.update_input_style()
            self.toolbar.hide_cursor()
            self.stdout.write(self.render_editor(bt, self.scroll.input_row, self.input_width()).encode())
            self.toolbar.render(self.autoreply_engine)
            cursor_col = self.editor_cursor()
            self.show_cursor_or_light(self.scroll.input_row, cursor_col)

        def discover_mode(self) -> None:
            """Launch or cancel autodiscover mode."""
            if self.ctx.discover_active:
                task = self.ctx.discover_task
                if task is not None:
                    task.cancel()
                return
            cmd = autodiscover_dialog(replay_buf=self.replay_buf, session_key=self.ctx.session_key)
            if cmd is None:
                return
            task = asyncio.ensure_future(handle_travel_commands([cmd], self.ctx, self.telnet_writer.log))
            task.add_done_callback(self.on_walk_done)
            self.ctx.discover_task = task

        def resume_last_walk(self) -> None:
            """Resume the most recent walk (autodiscover, randomwalk, or travel)."""
            echo_fn = self.ctx.echo_command
            mode = self.ctx.last_walk_mode
            if not mode:
                if echo_fn is not None:
                    echo_fn("RESUME: no previous walk to resume")
                return
            if self.ctx.last_walk_room != self.ctx.current_room_num:
                if echo_fn is not None:
                    echo_fn("RESUME: room changed since last walk, cannot resume")
                return
            if mode == "autodiscover":
                if self.ctx.discover_active:
                    task = self.ctx.discover_task
                    if task is not None:
                        task.cancel()
                    return
                t = asyncio.ensure_future(
                    autodiscover(
                        self.ctx,
                        self.telnet_writer.log,
                        resume=True,
                        strategy=self.ctx.last_walk_strategy,
                        noreply=self.ctx.last_walk_noreply,
                    )
                )
                t.add_done_callback(self.on_walk_done)
                self.ctx.discover_task = t
            elif mode == "randomwalk":
                if self.ctx.randomwalk_active:
                    task = self.ctx.randomwalk_task
                    if task is not None:
                        task.cancel()
                    return
                t = asyncio.ensure_future(
                    randomwalk(self.ctx, self.telnet_writer.log, resume=True, noreply=self.ctx.last_walk_noreply)
                )
                t.add_done_callback(self.on_walk_done)
                self.ctx.randomwalk_task = t
            elif echo_fn is not None:
                echo_fn(f"RESUME: cannot resume mode '{mode}'")

        def randomwalk_mode(self) -> None:
            """Launch or cancel random walk mode."""
            if self.ctx.randomwalk_active:
                task = self.ctx.randomwalk_task
                if task is not None:
                    task.cancel()
                return
            cmd = randomwalk_dialog(replay_buf=self.replay_buf, session_key=self.ctx.session_key)
            if cmd is None:
                return
            task = asyncio.ensure_future(handle_travel_commands([cmd], self.ctx, self.telnet_writer.log))
            task.add_done_callback(self.on_walk_done)
            self.ctx.randomwalk_task = task

        def register_gmcp_keys(self) -> None:
            """Register GMCP-dependent hotkeys (F3-F7) once."""
            if self.gmcp_keys_registered:
                return
            self.gmcp_keys_registered = True
            self.dispatch.register("KEY_F3", self.randomwalk_mode)
            self.dispatch.register("KEY_F4", self.discover_mode)
            self.dispatch.register("KEY_F5", self.resume_last_walk)
            self.dispatch.register("KEY_F7", lambda: launch_unified_editor("rooms", self.ctx, self.replay_buf))
            self.dispatch.register("KEY_F10", lambda: launch_unified_editor("captures", self.ctx, self.replay_buf))
            self.dispatch.register("KEY_F11", lambda: launch_unified_editor("bars", self.ctx, self.replay_buf))
            self.dispatch.register("KEY_F12", lambda: launch_unified_editor("theme", self.ctx, self.replay_buf))
            self.toolbar.schedule_eta_refresh(self.loop, self.autoreply_engine, self.editor, self.blessed_term)

        def update_input_style(self) -> None:
            """Update editor style based on autoreply / walk state."""
            assert self.scroll is not None
            self.editor.set_password_mode(bool(self.telnet_writer.will_echo))
            engine = self.autoreply_engine
            ar_active = engine is not None and (engine.exclusive_active or engine.reply_pending)
            disc = self.ctx.discover_active
            rwalk = self.ctx.randomwalk_active
            style = STYLE_AUTOREPLY if (disc or rwalk or ar_active) else STYLE_NORMAL
            changed = self.last_input_style is not style
            self.last_input_style = style
            for attr, val in style.items():
                setattr(self.editor, attr, val)
            if changed:
                active = style is STYLE_AUTOREPLY
                self.toolbar.last_ar_bg = active
                dmz_row = self.scroll.scroll_bottom + 1
                if dmz_row < self.scroll.input_row:
                    self.stdout.write(
                        (self.blessed_term.move_yx(dmz_row, 0) + dmz_line(self.scroll.cols, active)).encode()
                    )
                ac_age = time.monotonic() - self.ctx.active_command_time
                cmd_visible = self.ctx.command_queue is not None or (
                    self.ctx.active_command is not None and ac_age < FLASH_DURATION
                )
                if not cmd_visible:
                    self.stdout.write(
                        self.render_editor(self.blessed_term, self.scroll.input_row, self.input_width()).encode()
                    )

        @property
        def is_autoreply_bg(self) -> bool:
            """Return ``True`` when the input line uses the autoreply color scheme."""
            engine = self.autoreply_engine
            ar = engine is not None and (engine.exclusive_active or engine.reply_pending)
            return self.ctx.discover_active or self.ctx.randomwalk_active or ar

        @property
        def bg_sgr(self) -> str:
            """Return the current input-line background SGR sequence."""
            if self.is_autoreply_bg:
                return STYLE_AUTOREPLY["bg_sgr"]
            return STYLE_NORMAL["bg_sgr"]

        HELP_HINT = "press F1 for help"

        def activity_hint(self) -> str:
            """Build a short status string for the current activity."""
            return activity_hint(self.autoreply_engine, self.blessed_term.width)

        def hint_text(self) -> str:
            """Return the current hint string (activity or help)."""
            ar = self.is_autoreply_bg
            hint = self.activity_hint() if ar else self.HELP_HINT
            return hint if hint else self.HELP_HINT

        def input_width(self) -> int:
            """Return editor width, reserving space for the right-aligned hint."""
            bt = self.blessed_term
            hint = self.hint_text()
            if hint:
                w = max(2, bt.width - len(hint))
            else:
                w = bt.width
            self.editor.max_width = w
            return w

        def render_input_hint(self, row: int) -> None:
            """Draw a dim right-aligned hint on the input row."""
            hint = self.hint_text()
            if not hint:
                return
            bt = self.blessed_term
            hint_w = len(hint)
            col = bt.width - hint_w
            if col < 2:
                return
            ar = self.is_autoreply_bg
            bg = STYLE_AUTOREPLY["bg_sgr"] if ar else STYLE_NORMAL["bg_sgr"]
            prog = until_progress(self.autoreply_engine)
            self.stdout.write(bt.move_yx(row, col).encode())
            write_hint(hint, self.stdout, bt, progress=prog, bg_sgr=bg, autoreply=ar)
            if prog is not None:
                self.toolbar.schedule_until_progress(self.loop, self.autoreply_engine, self.editor, bt)

        def show_cursor_or_light(self, row: int, col: int) -> None:
            """
            Show cursor or draw modem-light glyph at the edit position.

            If the stoplight is animating, draw the sextant character at
            ``(row, col)`` and keep the terminal cursor hidden.  Otherwise
            set the cursor color and schedule the terminal cursor to show
            after a short debounce delay.
            Also draws a right-aligned cancel hint when applicable.
            """
            bt = self.blessed_term
            ar = self.is_autoreply_bg
            self.render_input_hint(row)
            drew = self.toolbar.cursor_light(bt, row, col, ar)
            if not drew:
                style = STYLE_AUTOREPLY if ar else STYLE_NORMAL
                osc = cursor_ar_osc() if ar else cursor_osc()
                self.stdout.write(bt.move_yx(row, col).encode())
                self.stdout.write(osc.encode())
                self.stdout.write(style["cursor_sgr"].encode())
                self.toolbar.schedule_cursor_show(self.loop)
                self.stdout.write(bt.normal.encode())

        def rearm_after_subprocess(self) -> None:
            """
            Flush stale input and re-enable in-band resize after subprocess.

            Called by the main event loop after the post-action render is
            complete.  Discards any terminal input that arrived while the
            subprocess was running (stale resize notifications, key echos),
            records the current terminal size to suppress a redundant
            ``on_resize_repaint``, and re-enables DEC mode 2048.
            """
            global subprocess_needs_rearm
            if not subprocess_needs_rearm:
                return
            subprocess_needs_rearm = False
            try:
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            except (OSError, termios.error):
                pass
            try:
                tsize = os.get_terminal_size()
                self.last_resize_size[:] = [tsize.lines, tsize.columns]
            except OSError:
                pass
            if self.tty_shell._resize_pending.is_set():
                self.tty_shell._resize_pending.clear()
            sys.stdout.write("\x1b[?2048h")
            sys.stdout.flush()

        def on_resize_repaint(self, rows: int, cols: int) -> None:
            """Repaint screen after terminal resize."""
            if [rows, cols] == self.last_resize_size:
                return
            self.last_resize_size[:] = [rows, cols]
            bt = get_term()
            sr = self.scroll
            reserve = sr.reserve if sr is not None else RESERVE_WITH_TOOLBAR
            self.toolbar.hide_cursor()
            self.stdout.write((bt.clear + bt.home + bt.move_yx(0, 0)).encode())
            data = self.replay_buf.replay()
            if data:
                self.stdout.write(data)
            self.stdout.write(bt.save.encode())
            input_row = rows - reserve
            for r in range(input_row, rows):
                self.stdout.write((bt.move_yx(r, 0) + bt.clear_eol).encode())
            ar_bg = self.is_autoreply_bg
            if sr is not None:
                dmz = sr.scroll_bottom + 1
                if dmz < sr.input_row:
                    self.stdout.write((bt.move_yx(dmz, 0) + dmz_line(cols, ar_bg)).encode())
            cs = self.ctx.cursor_style or DEFAULT_CURSOR_STYLE
            style = STYLE_AUTOREPLY if ar_bg else STYLE_NORMAL
            osc = cursor_ar_osc() if ar_bg else cursor_osc()
            self.stdout.write(CURSOR_STYLES.get(cs, CURSOR_STEADY_BLOCK).encode())
            self.stdout.write(osc.encode())
            self.stdout.write(style["cursor_sgr"].encode())
            self.toolbar.schedule_cursor_show(self.loop)

        def fire_resize(self) -> None:
            """Handle resize: update scroll region, NAWS, re-render UI."""
            assert self.scroll is not None
            bt = get_term()
            new_rows, new_cols = bt.height, bt.width
            if self.tty_shell.on_resize is not None:
                self.tty_shell.on_resize(new_rows, new_cols)
            if self.telnet_writer.local_option.enabled(telnetlib3.telopt.NAWS) and not self.telnet_writer.is_closing():
                self.telnet_writer._send_naws()
            self.toolbar.hide_cursor()
            self.update_input_style()
            self.stdout.write(self.render_editor(bt, self.scroll.input_row, self.input_width()).encode())
            self.toolbar.render(self.autoreply_engine)
            cursor_col = self.editor_cursor()
            self.show_cursor_or_light(self.scroll.input_row, cursor_col)

        def register_callbacks(self) -> None:
            """Wire up IAC callbacks, hotkeys, and context hooks."""
            self.telnet_writer.set_iac_callback(telnetlib3.telopt.GA, self.on_prompt_signal)
            self.telnet_writer.set_iac_callback(telnetlib3.telopt.CMD_EOR, self.on_prompt_signal)

            self.ctx.wait_for_prompt = self.wait_for_prompt
            self.ctx.echo_command = self.echo_autoreply
            self.ctx.prompt_ready = self.prompt_ready
            self.ctx.on_autoreply_activity = self.on_autoreply_activity

            self.refresh_autoreply_engine()
            self.refresh_highlight_engine()

            self.dispatch.register_seq("\x1d", self.reg_close)  # Ctrl+]
            assert self.scroll is not None
            scroll = self.scroll
            replay_buf = self.replay_buf
            self.dispatch.register_seq(
                "\x0c", lambda: repaint_screen(replay_buf, scroll=scroll, active=self.is_autoreply_bg)
            )  # Ctrl+L

            self.dispatch.register("KEY_F1", lambda: show_help(replay_buf=self.replay_buf))
            self.dispatch.register("KEY_F6", lambda: launch_unified_editor("highlights", self.ctx, self.replay_buf))
            self.dispatch.register("KEY_F8", lambda: launch_unified_editor("macros", self.ctx, self.replay_buf))
            self.dispatch.register("KEY_F9", lambda: launch_unified_editor("autoreplies", self.ctx, self.replay_buf))
            self.dispatch.register("KEY_F21", self.toggle_autoreplies)  # Shift+F9
            self.dispatch.register("KEY_F18", self.toggle_highlights)  # Shift+F6

            self.ctx.on_gmcp_ready = self.register_gmcp_keys

        def submit_command_queue(
            self,
            commands: list[str],
            chained_task_ref: list["asyncio.Task[None] | None"],
            immediate_set: frozenset[int] = frozenset(),
        ) -> None:
            """Create a command queue and start chained send."""
            assert self.scroll is not None
            scroll = self.scroll
            q = CommandQueue(
                commands,
                render=lambda: render_command_queue(
                    self.ctx.command_queue,
                    scroll,
                    self.stdout,
                    flash_elapsed=time.monotonic() - self.ctx.active_command_time,
                    hint=self.activity_hint(),
                    progress=until_progress(self.autoreply_engine),
                    base_bg_sgr=self.bg_sgr,
                    autoreply=self.is_autoreply_bg,
                ),
            )
            self.ctx.command_queue = q
            self.ctx.active_command_time = time.monotonic()
            q.render()
            task = asyncio.ensure_future(
                send_chained(commands, self.ctx, self.telnet_writer.log, queue=q, immediate_set=immediate_set)
            )
            task.add_done_callback(lambda f: clear_command_queue(self.ctx))
            chained_task_ref[0] = task

        async def read_server(self) -> None:
            """Read and display server output until EOF or kludge switch."""
            assert self.scroll is not None
            scroll = self.scroll
            bt = self.blessed_term
            esc_hold = b""
            rx_dot = self.stoplight.rx
            while not self.server_done:
                out = await self.telnet_reader.read(2**24)
                if not out:
                    if self.telnet_reader.at_eof():
                        local_close = self.server_done
                        self.server_done = True
                        self.cancel_line_hold_timer()
                        held = self.line_hold.flush_raw()
                        if held:
                            self.stdout.write(bt.restore.encode())
                            held_enc = held.encode()
                            self.stdout.write(held_enc)
                            self.replay_buf.append(held_enc)
                            self.stdout.write(bt.save.encode())
                        if esc_hold:
                            self.stdout.write(bt.restore.encode())
                            self.stdout.write(esc_hold)
                            self.replay_buf.append(esc_hold)
                            self.stdout.write(bt.save.encode())
                        cf = self.ctx.color_filter
                        if cf is not None:
                            flush = cf.flush()
                            if flush:
                                self.stdout.write(flush.encode())
                        self.stdout.write(bt.restore.encode())
                        if local_close:
                            msg = b"\r\nConnection closed by client.\r\n"
                        else:
                            msg = b"\r\nConnection reset by peer.\r\n"
                        self.stdout.write(msg + bt.clear_eos.encode())
                        return
                    if self.prompt_pending:
                        self.cancel_line_hold_timer()
                        held = self.line_hold.flush_for_prompt()
                        if held:
                            self.stdout.write(bt.restore.encode())
                            held_enc = held.encode()
                            self.stdout.write(held_enc)
                            self.replay_buf.append(held_enc)
                            self.stdout.write(bt.save.encode())
                        self.prompt_pending = False
                        if self.autoreply_engine is not None:
                            self.autoreply_engine.on_prompt()
                        self.update_input_style()
                    continue
                rx_dot.trigger()
                if isinstance(out, bytes):
                    out = out.decode("utf-8", errors="replace")
                cf = self.ctx.color_filter
                if cf is not None:
                    out = cf.filter(out)
                out = telnetlib3.client_shell._transform_output(out, self.telnet_writer, True)
                if self.ctx.erase_eol:
                    out = out.replace("\r\n", "\x1b[K\r\n")
                ts = self.ctx.typescript_file
                if ts is not None:
                    ts.write(out)
                    ts.flush()
                self.refresh_autoreply_engine()
                self.refresh_highlight_engine()
                is_prompt = self.prompt_pending
                if self.dialogs_mod.editor_active:
                    self.dialogs_mod.editor_buffer.append(out.encode())
                    continue
                emit_now, held_back = self.line_hold.add(out)
                if held_back and is_prompt:
                    self.cancel_line_hold_timer()
                    emit_now += self.line_hold.flush_for_prompt()
                    held_back = ""
                    self.prompt_pending = False
                if held_back:
                    self.schedule_line_hold_flush()
                if not emit_now and not self.dialogs_mod.editor_buffer:
                    continue
                if emit_now and not held_back:
                    self.cancel_line_hold_timer()
                self.toolbar.hide_cursor()
                self.stdout.write(bt.restore.encode())
                if self.dialogs_mod.editor_buffer:
                    for chunk in self.dialogs_mod.editor_buffer:
                        self.stdout.write(chunk)
                        self.replay_buf.append(chunk)
                    self.dialogs_mod.editor_buffer.clear()
                encoded = esc_hold + emit_now.encode()
                encoded, esc_hold = split_incomplete_esc(encoded)
                if encoded:
                    self.stdout.write(encoded)
                    self.replay_buf.append(encoded)
                self.stdout.write(bt.save.encode())
                if self.autoreply_engine is not None:
                    self.autoreply_engine.feed(emit_now)
                    if is_prompt:
                        self.autoreply_engine.on_prompt()
                        self.prompt_pending = False
                cq_s = self.ctx.command_queue
                ac_s = self.ctx.active_command
                ac_elapsed = time.monotonic() - self.ctx.active_command_time
                hint = self.activity_hint()
                prog = until_progress(self.autoreply_engine)
                bg = self.bg_sgr
                ar = self.is_autoreply_bg
                if cq_s is not None:
                    cursor_col = render_command_queue(
                        cq_s,
                        scroll,
                        self.stdout,
                        flash_elapsed=ac_elapsed,
                        hint=hint,
                        progress=prog,
                        base_bg_sgr=bg,
                        autoreply=ar,
                    )
                elif ac_s is not None and ac_elapsed < FLASH_DURATION:
                    cursor_col = render_active_command(
                        ac_s,
                        scroll,
                        self.stdout,
                        flash_elapsed=ac_elapsed,
                        hint=hint,
                        progress=prog,
                        base_bg_sgr=bg,
                        autoreply=ar,
                    )
                else:
                    self.update_input_style()
                    self.stdout.write(self.render_editor(bt, scroll.input_row, self.input_width()).encode())
                    cursor_col = self.editor_cursor()
                needs_reflash = self.toolbar.render(self.autoreply_engine)
                if needs_reflash and not self.toolbar.flash_active:
                    self.toolbar.flash_active = True
                    self.toolbar.schedule_flash(self.loop, self.autoreply_engine, self.editor, bt)
                self.show_cursor_or_light(scroll.input_row, cursor_col)
                if self.telnet_writer.mode != "local":
                    self.mode_switched = True
                    self.server_done = True
                    return

        async def read_input(self) -> None:
            """Read keyboard input until server done or EOF."""
            assert self.scroll is not None
            scroll = self.scroll
            bt = self.blessed_term
            tx_dot = self.stoplight.tx
            self.update_input_style()
            self.stdout.write(self.render_editor(bt, scroll.input_row, self.input_width()).encode())
            chained_task_ref: list[asyncio.Task[None] | None] = [None]
            with bt.raw(), bt.notify_on_resize():
                while not self.server_done:
                    key = await bt.async_inkey(timeout=0.1)

                    if key.name == "RESIZE_EVENT":
                        self.tty_shell._resize_pending.set()
                        continue

                    if not key:
                        if self.tty_shell._resize_pending.is_set():
                            self.tty_shell._resize_pending.clear()
                            self.fire_resize()
                        continue

                    if self.tty_shell._resize_pending.is_set():
                        self.tty_shell._resize_pending.clear()
                        self.fire_resize()

                    action = self.dispatch.lookup(key)
                    if action is not None:
                        result = action()
                        if asyncio.iscoroutine(result):
                            await result
                        self.rearm_after_subprocess()
                        self.toolbar.hide_cursor()
                        self.update_input_style()
                        self.stdout.write(self.render_editor(bt, scroll.input_row, self.input_width()).encode())
                        self.toolbar.render(self.autoreply_engine)
                        cursor_col = self.editor_cursor()
                        self.show_cursor_or_light(scroll.input_row, cursor_col)
                        continue

                    result = self.editor.feed_key(key)

                    if result.eof:
                        self.server_done = True
                        self.telnet_writer.close()
                        return

                    if result.interrupt:
                        self.toolbar.hide_cursor()
                        self.update_input_style()
                        self.stdout.write(self.render_editor(bt, scroll.input_row, self.input_width()).encode())
                        cursor_col = self.editor_cursor()
                        self.show_cursor_or_light(scroll.input_row, cursor_col)
                        continue

                    if result.line is not None:
                        line = result.line

                        cq = self.ctx.command_queue
                        if cq is not None and not cq.cancelled:
                            cq.cancelled = True
                            cq.cancel_event.set()
                            chained = chained_task_ref[0]
                            if chained is not None and not chained.done():
                                chained.cancel()
                            self.ctx.command_queue = None

                        if self.history_file and not self.telnet_writer.will_echo:
                            save_history_entry(line, self.history_file)

                        ts = self.ctx.typescript_file
                        if ts is not None:
                            if self.telnet_writer.will_echo:
                                ts.write("\r\n")
                            else:
                                ts.write(line + "\r\n")
                            ts.flush()

                        is_pw = self.telnet_writer.will_echo
                        echo = scramble_password() if is_pw else line
                        self.stdout.write(bt.restore.encode())
                        colored = f"{bt.yellow}{echo}{bt.normal}\r\n"
                        self.stdout.write(colored.encode())
                        self.replay_buf.append(colored.encode())
                        self.stdout.write(bt.save.encode())

                        if self.ga_detected:
                            try:
                                await asyncio.wait_for(self.prompt_ready.wait(), timeout=2.0)
                            except asyncio.TimeoutError:
                                pass

                        if self.autoreply_engine is not None:
                            self.autoreply_engine.cancel()
                        disc_task = self.ctx.discover_task
                        if disc_task is not None and not disc_task.done():
                            disc_task.cancel()
                        rw_task = self.ctx.randomwalk_task
                        if rw_task is not None and not rw_task.done():
                            rw_task.cancel()
                        ft_task = self.ctx.travel_task
                        if ft_task is not None and not ft_task.done():
                            ft_task.cancel()
                            self.ctx.travel_task = None

                        expanded = expand_commands_ex(line)
                        parts = expanded.commands
                        imm = expanded.immediate_set

                        while parts and DELAY_RE.match(parts[0]):
                            dm = DELAY_RE.match(parts[0])
                            assert dm is not None
                            value = float(dm.group(1))
                            unit = dm.group(2)
                            delay = value / 1000.0 if unit == "ms" else value
                            if delay > 0:
                                await asyncio.sleep(delay)
                            parts = parts[1:]

                        if parts and EDIT_THEME_RE.match(parts[0]):
                            launch_unified_editor("theme", self.ctx, self.replay_buf)
                            parts = parts[1:]
                        elif parts and TRAVEL_RE.match(parts[0]):
                            remainder = await handle_travel_commands(parts, self.ctx, self.telnet_writer.log)
                            if remainder:
                                tx_dot.trigger()
                                self.telnet_writer.write(
                                    remainder[0] + "\r\n"  # type: ignore[arg-type]
                                )
                                if self.ga_detected:
                                    self.prompt_ready.clear()
                                if len(remainder) > 1:
                                    self.submit_command_queue(remainder, chained_task_ref)
                        elif parts:
                            tx_dot.trigger()
                            self.telnet_writer.write(parts[0] + "\r\n")  # type: ignore[arg-type]
                            if self.ga_detected:
                                self.prompt_ready.clear()
                            if len(parts) > 1:
                                self.submit_command_queue(parts, chained_task_ref, immediate_set=imm)
                        else:
                            tx_dot.trigger()
                            self.telnet_writer.write("\r\n")  # type: ignore[arg-type]

                    if result.changed:
                        self.toolbar.hide_cursor()
                        cq2 = self.ctx.command_queue
                        if cq2 is not None:
                            ac_elapsed2 = time.monotonic() - self.ctx.active_command_time
                            cursor_col = render_command_queue(
                                cq2,
                                scroll,
                                self.stdout,
                                flash_elapsed=ac_elapsed2,
                                hint=self.activity_hint(),
                                progress=until_progress(self.autoreply_engine),
                                base_bg_sgr=self.bg_sgr,
                                autoreply=self.is_autoreply_bg,
                            )
                        else:
                            self.update_input_style()
                            self.stdout.write(self.render_editor(bt, scroll.input_row, self.input_width()).encode())
                            cursor_col = self.editor_cursor()
                        needs_reflash = self.toolbar.render(self.autoreply_engine)
                        if needs_reflash and not self.toolbar.flash_active:
                            self.toolbar.flash_active = True
                            self.toolbar.schedule_flash(self.loop, self.autoreply_engine, self.editor, bt)
                        self.show_cursor_or_light(scroll.input_row, cursor_col)

        def cleanup(self) -> None:
            """Cancel autoreply engine, restore cursor, clear kludge DMZ."""
            if self.autoreply_engine is not None:
                self.autoreply_engine.cancel()
            self.ctx.close()
            if self.toolbar.cursor_show_handle is not None:
                self.toolbar.cursor_show_handle.cancel()
                self.toolbar.cursor_show_handle = None
            self.toolbar.cursor_hidden = False
            self.stdout.write(CURSOR_DEFAULT.encode())
            self.stdout.write(CURSOR_COLOR_RESET_OSC.encode())
            if self.mode_switched:
                assert self.scroll is not None
                dmz_row = self.scroll.scroll_bottom + 1
                self.stdout.write(self.blessed_term.save.encode())
                self.stdout.write(self.blessed_term.move_yx(dmz_row, 0).encode())
                self.stdout.write(self.blessed_term.normal.encode())
                self.stdout.write(self.blessed_term.clear_eos.encode())
                self.stdout.write(self.blessed_term.restore.encode())

        async def run(self) -> bool:
            """
            Run the REPL event loop.

            :returns: ``True`` if the server switched to kludge mode,
                ``False`` if the connection closed normally.
            """
            self.init_terminal()
            self.init_editor()
            self.init_ui()

            async with repl_scaffold(
                self.telnet_writer,
                self.tty_shell,
                self.stdout,
                reserve_bottom=RESERVE_INITIAL,
                on_resize=self.on_resize_repaint,
            ) as (scroll, _):
                self.scroll = scroll
                self.blessed_term = get_term()
                self.toolbar = ToolbarRenderer(
                    ctx=self.ctx, scroll=scroll, out=self.stdout, stoplight=self.stoplight, rprompt_text=self.conn_info
                )

                if self.banner_lines:
                    for bl in self.banner_lines:
                        self.stdout.write(f"{bl}\r\n".encode())

                self.stdout.write(self.blessed_term.save.encode())
                cs = self.ctx.cursor_style or DEFAULT_CURSOR_STYLE
                self.stdout.write(CURSOR_STYLES.get(cs, CURSOR_STEADY_BLOCK).encode())

                self.register_callbacks()

                try:
                    await run_repl_tasks(self.read_server(), self.read_input())
                finally:
                    self.cleanup()

            return self.mode_switched

    async def repl_event_loop(
        telnet_reader: "telnetlib3.TelnetReader | telnetlib3.TelnetReaderUnicode",
        telnet_writer: "telnetlib3.TelnetWriter | telnetlib3.TelnetWriterUnicode",
        tty_shell: "client_shell.Terminal",
        stdout: asyncio.StreamWriter,
        history_file: str | None = None,
        banner_lines: list[str] | None = None,
    ) -> bool:
        """
        Event loop with REPL input at the bottom of the screen.

        Uses blessed ``async_inkey()`` for keystroke input and a headless
        :class:`~blessed.line_editor.LineEditor` for line editing with
        history and auto-suggest.

        :param tty_shell: ``Terminal`` instance from ``client_shell``.
        :param banner_lines: Lines to display after scroll region is active.
        :returns: ``True`` if the server switched to kludge mode
            (caller should fall through to the standard event loop),
            ``False`` if the connection closed normally.
        """
        session = ReplSession(
            telnet_reader, telnet_writer, tty_shell, stdout, history_file=history_file, banner_lines=banner_lines
        )
        return await session.run()
