"""
Textual TUI session manager for telix -- re-export hub.

Implementation is split across several sub-modules:

- ``client_tui_base`` -- sessions, base editors, app foundation
- ``client_tui_macros`` -- macro editor pane and screen
- ``client_tui_autoreplies`` -- autoreply editor pane and screen
- ``client_tui_highlights`` -- highlight editor pane and screen
- ``client_tui_bars`` -- progress bar and theme editor panes and screens
- ``client_tui_editors`` -- shared helpers and standalone entry points
- ``client_tui_rooms`` -- room browser and picker screens
- ``client_tui_captures`` -- highlight captures and chat viewer screens
- ``client_tui_app`` -- main Textual application and entry point
- ``client_tui_dialogs`` -- confirm/walk dialogs and tabbed editor screen

This file re-exports every public name so existing
``from telix.client_tui import ...`` statements continue to work unchanged.
"""

# local
# Re-export module-level path constants so monkeypatch targets are valid.
from .paths import DATA_DIR, CONFIG_DIR, SESSIONS_FILE  # noqa: F401
from .client_tui_app import TelnetSessionApp, tui_main  # noqa: F401
from .client_tui_bars import ThemeEditPane, ProgressBarTuple, ProgressBarEditPane, ProgressBarEditScreen  # noqa: F401

# -- base layer -------------------------------------------------------------
from .client_tui_base import (  # noqa: F401
    ENCODINGS,
    BATCH_SIZE,
    DEFAULTS_KEY,
    TOOLTIP_CACHE,
    FLAG_TO_WIDGET,
    TERMINAL_CLEANUP,
    PRIMARY_PASTE_COMMANDS,
    HelpPane,
    EditorApp,
    EditListPane,
    SessionConfig,
    EditListScreen,
    CommandHelpScreen,
    SessionEditScreen,
    SessionListScreen,
    int_val,
    float_val,
    build_command,
    launch_editor,
    load_sessions,
    relative_time,
    save_sessions,
    build_tooltips,
    get_help_topic,
    build_ssh_command,
    normalize_encoding,
    restore_blocking_fds,
    log_child_diagnostics,
    read_primary_selection,
    handle_arrow_navigation,
    patch_writer_thread_queue,
)

# -- rooms, captures, dialogs, app -----------------------------------------
from .client_tui_rooms import (  # noqa: F401
    HOME_STYLE,
    ARROW_STYLE,
    ID_COL_BASE,
    MARKED_STYLE,
    BLOCKED_STYLE,
    NAME_COL_BASE,
    BOOKMARK_STYLE,
    BUTTON_COL_MIN,
    BUTTON_COL_GROW,
    RoomTree,
    RoomBrowserPane,
    RoomPickerScreen,
    RoomBrowserScreen,
    edit_rooms_main,
)

# -- editor panes -----------------------------------------------------------
from .client_tui_macros import MacroEditPane, MacroEditScreen  # noqa: F401
from .client_tui_dialogs import (  # noqa: F401
    EDITOR_TABS,
    TabbedEditorScreen,
    ConfirmDialogScreen,
    RandomwalkDialogScreen,
    AutodiscoverDialogScreen,
    confirm_dialog_main,
    unified_editor_main,
    randomwalk_dialog_main,
    autodiscover_dialog_main,
)
from .client_tui_editors import (  # noqa: F401
    invert_ts,
    show_help_main,
    edit_macros_main,
    edit_highlights_main,
    edit_autoreplies_main,
    edit_progressbars_main,
)
from .client_tui_captures import CapsPane, CapsScreen, ChatViewerScreen, chat_viewer_main  # noqa: F401
from .client_tui_highlights import HighlightTuple, HighlightEditPane, HighlightEditScreen  # noqa: F401
from .client_tui_autoreplies import AutoreplyTuple, AutoreplyEditPane, AutoreplyEditScreen  # noqa: F401
