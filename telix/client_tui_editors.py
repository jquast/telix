"""
Standalone entry points for the Textual TUI editor panes.

Contains the ``invert_ts`` helper and ``main()`` launcher functions for
macros, autoreplies, highlights, progress bars, help, and theme editing.
The concrete editor panes live in their own modules:
``client_tui_macros``, ``client_tui_autoreplies``, ``client_tui_highlights``,
and ``client_tui_bars``.
"""

# local
from . import client_tui_base
from .client_tui_bars import ThemeEditPane, ProgressBarTuple, ProgressBarEditPane, ProgressBarEditScreen  # noqa: F401
from .client_tui_macros import MacroEditPane, MacroEditScreen  # noqa: F401
from .client_tui_highlights import HighlightTuple, HighlightEditPane, HighlightEditScreen  # noqa: F401
from .client_tui_autoreplies import AutoreplyTuple, AutoreplyEditPane, AutoreplyEditScreen  # noqa: F401


def invert_ts(iso_str: str) -> str:
    """
    Return a sort key that orders ISO timestamps most-recent-first.

    Empty strings sort last (after any real timestamp).
    """
    if not iso_str:
        return "\xff"
    digits = "".join(c for c in iso_str if c.isdigit())
    return "".join(chr(ord("9") - ord(c) + ord("0")) for c in digits)


def edit_macros_main(
    path: str, session_key: str = "", rooms_file: str = "", current_room_file: str = "", logfile: str = ""
) -> None:
    """Launch standalone macro editor TUI."""
    client_tui_base.launch_editor(
        MacroEditScreen(path=path, session_key=session_key, rooms_file=rooms_file, current_room_file=current_room_file),
        session_key=session_key,
        logfile=logfile,
    )


def edit_autoreplies_main(
    path: str, session_key: str = "", select_pattern: str = "", gmcp_snapshot_path: str = "", logfile: str = ""
) -> None:
    """Launch standalone autoreply editor TUI."""
    client_tui_base.launch_editor(
        AutoreplyEditScreen(
            path=path, session_key=session_key, select_pattern=select_pattern, gmcp_snapshot_path=gmcp_snapshot_path
        ),
        session_key=session_key,
        logfile=logfile,
    )


def edit_highlights_main(path: str, session_key: str = "", logfile: str = "") -> None:
    """Launch standalone highlight editor TUI."""
    client_tui_base.launch_editor(
        HighlightEditScreen(path=path, session_key=session_key), session_key=session_key, logfile=logfile
    )


def edit_progressbars_main(path: str, session_key: str = "", gmcp_snapshot_path: str = "", logfile: str = "") -> None:
    """Launch standalone progress bar editor TUI."""
    client_tui_base.launch_editor(
        ProgressBarEditScreen(path=path, session_key=session_key, gmcp_snapshot_path=gmcp_snapshot_path),
        session_key=session_key,
        logfile=logfile,
    )


def show_help_main(topic: str = "keybindings", logfile: str = "") -> None:
    """Launch standalone help viewer TUI."""
    client_tui_base.launch_editor(client_tui_base.CommandHelpScreen(topic=topic), logfile=logfile)
