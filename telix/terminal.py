"""
Platform dispatcher for terminal operations.

Imports all public functions from :mod:`telix.terminal_unix` on POSIX
systems and from :mod:`telix.terminal_win32` on Windows.  All call sites
import this module rather than the platform-specific modules.
"""

import sys

if sys.platform == "win32":
    from .terminal_win32 import (
        flush_stdin,
        blocking_fds,
        restore_opost,
        get_terminal_size,
        pause_before_exit,
        restore_io_blocking,
        set_blocking_stdout,
        restore_blocking_fds,
    )
else:
    from .terminal_unix import (  # noqa: F401
        flush_stdin,
        blocking_fds,
        restore_opost,
        get_terminal_size,
        pause_before_exit,
        restore_io_blocking,
        set_blocking_stdout,
        restore_blocking_fds,
    )
