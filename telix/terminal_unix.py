"""Unix-specific terminal operations for the telix REPL."""

import os
import sys
import fcntl
import struct
import logging
import termios
import contextlib
from collections.abc import Generator


def get_terminal_size() -> tuple[int, int]:
    """
    Return ``(rows, cols)`` of the controlling terminal.

    Falls back to the ``LINES`` / ``COLUMNS`` environment variables when
    the ioctl is not available (e.g. no controlling tty).
    """
    try:
        fmt = "hhhh"
        buf = b"\x00" * struct.calcsize(fmt)
        val = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, buf)
        rows, cols, _, _ = struct.unpack(fmt, val)
        return rows, cols
    except OSError:
        return (int(os.environ.get("LINES", "25")), int(os.environ.get("COLUMNS", "80")))


@contextlib.contextmanager
def blocking_fds() -> Generator[None, None, None]:
    """
    Context manager to ensure FDs 0/1/2 are blocking for a subprocess.

    asyncio's ``connect_write_pipe`` sets ``O_NONBLOCK`` on the PTY file
    description.  A Textual subprocess inherits non-blocking FDs, which can
    cause its ``WriterThread`` to silently fail.  This saves and restores
    the blocking state around subprocess calls.
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


def set_blocking_stdout(blocking: bool) -> bool:
    """
    Set ``O_NONBLOCK`` on stdout, returning the previous blocking state.

    :param blocking: The new blocking state.
    :returns: The previous blocking state.
    """
    fd = sys.stdout.fileno()
    try:
        was = os.get_blocking(fd)
        os.set_blocking(fd, blocking)
        return was
    except OSError:
        return blocking


def restore_io_blocking() -> None:
    """Set stdin and stdout back to blocking mode after a subprocess exits."""
    for fd in (0, 1):
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass


def flush_stdin() -> None:
    """Flush stale input from stdin via ``TCIFLUSH``."""
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, termios.error):
        pass


def restore_opost() -> None:
    r"""
    Ensure the terminal ``OPOST`` flag is set so ``\n`` maps to ``\r\n``.

    Textual puts the terminal in raw mode which disables output post-processing. If the driver fails to fully restore
    termios, newlines render as bare LF producing staircase output.
    """
    try:
        fd = sys.stdout.fileno()
        attrs = termios.tcgetattr(fd)
        if not (attrs[1] & termios.OPOST):
            attrs[1] |= termios.OPOST
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (OSError, termios.error, ValueError, AttributeError):
        pass


def pause_before_exit() -> None:
    """Prompt user to press RETURN so they can read error output."""
    sys.stdout.write("\r\nPress RETURN to continue...\r\n")
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    try:
        os.set_blocking(fd, True)
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] |= termios.ICANON | termios.ECHO | termios.ISIG
        try:
            termios.tcsetattr(fd, termios.TCSANOW, new)
            os.read(fd, 1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (OSError, termios.error, EOFError, KeyboardInterrupt):
        pass


def restore_blocking_fds(logfile: str = "") -> None:
    """
    Restore blocking mode on stdin/stdout/stderr.

    The parent process may set ``O_NONBLOCK`` on the shared PTY file
    description (via asyncio ``connect_read_pipe``).
    Since stdin, stdout, and stderr all reference the same kernel file
    description, the child subprocess inherits non-blocking mode.
    Textual's ``WriterThread`` does not handle ``BlockingIOError``,
    so a non-blocking stderr causes the thread to die silently.

    :param logfile: Optional path to the parent's logfile for child logging.
    """
    if logfile:
        logging.basicConfig(
            filename=logfile, level=logging.DEBUG, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
        )

    log = logging.getLogger(__name__)
    log.debug(
        "child pre-fix: fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s __stdin___isatty=%s "
        "stderr_isatty=%s __stderr___isatty=%s",
        os.get_blocking(0),
        os.get_blocking(1),
        os.get_blocking(2),
        sys.stdin.isatty(),
        sys.__stdin__.isatty(),
        sys.stderr.isatty(),
        sys.__stderr__.isatty(),
    )
    for fd in (0, 1, 2):
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass
    log.debug(
        "child post-fix: fd0_blocking=%s fd1=%s fd2=%s", os.get_blocking(0), os.get_blocking(1), os.get_blocking(2)
    )
