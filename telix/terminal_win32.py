"""Windows terminal operations for the telix REPL (stubs)."""

import os
import sys
import msvcrt
import contextlib
from collections.abc import Generator


def get_terminal_size() -> tuple[int, int]:
    """
    Return ``(rows, cols)`` of the controlling terminal.

    Uses :func:`os.get_terminal_size` which is supported on Windows.
    """
    try:
        sz = os.get_terminal_size()
        return sz.lines, sz.columns
    except OSError:
        return (int(os.environ.get("LINES", "25")), int(os.environ.get("COLUMNS", "80")))


@contextlib.contextmanager
def blocking_fds() -> Generator[None, None, None]:
    """
    Context manager stub -- no ``O_NONBLOCK`` concept on Windows IOCP.

    Yields immediately with no side effects.
    """
    yield


def set_blocking_stdout(blocking: bool) -> bool:
    """
    No-op on Windows -- console handles have no ``O_NONBLOCK`` flag.

    :param blocking: Ignored.
    :returns: *blocking* unchanged.
    """
    return blocking


def restore_io_blocking() -> None:
    """No-op on Windows -- console handles have no ``O_NONBLOCK`` flag."""


def flush_stdin() -> None:
    """Flush stale input from the console input buffer."""
    try:
        msvcrt.FlushConsoleInputBuffer(msvcrt.get_osfhandle(0))  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        pass


def restore_opost() -> None:
    """No-op on Windows -- CRLF translation is handled differently."""


def pause_before_exit() -> None:
    """Prompt user to press a key so they can read error output."""
    sys.stdout.write("\r\nPress RETURN to continue...\r\n")
    sys.stdout.flush()
    try:
        msvcrt.getwch()  # type: ignore[attr-defined]
    except (OSError, KeyboardInterrupt):
        pass


def restore_blocking_fds(logfile: str = "") -> None:
    """No-op on Windows -- console handles have no ``O_NONBLOCK`` flag."""
