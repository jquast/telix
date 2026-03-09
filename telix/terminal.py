"""
Platform dispatcher for terminal operations.

Imports all public functions from :mod:`telix.terminal_unix` on POSIX
systems and from :mod:`telix.terminal_win32` on Windows.  All call sites
import this module rather than the platform-specific modules.
"""

import sys

if sys.platform == "win32":
    from .terminal_win32 import flush_stdin as flush_stdin
    from .terminal_win32 import blocking_fds as blocking_fds
    from .terminal_win32 import restore_opost as restore_opost
    from .terminal_win32 import get_terminal_size as get_terminal_size
    from .terminal_win32 import restore_io_blocking as restore_io_blocking
    from .terminal_win32 import set_blocking_stdout as set_blocking_stdout
    from .terminal_win32 import restore_blocking_fds as restore_blocking_fds
else:
    from .terminal_unix import flush_stdin as flush_stdin
    from .terminal_unix import blocking_fds as blocking_fds
    from .terminal_unix import restore_opost as restore_opost
    from .terminal_unix import get_terminal_size as get_terminal_size
    from .terminal_unix import restore_io_blocking as restore_io_blocking
    from .terminal_unix import set_blocking_stdout as set_blocking_stdout
    from .terminal_unix import restore_blocking_fds as restore_blocking_fds
