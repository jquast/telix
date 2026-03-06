"""
Textual TUI session manager for telix -- re-export hub.

All implementation lives in the ``client_tui_base``, ``client_tui_editors``,
and ``client_tui_dialogs`` sub-modules.  This file re-exports every public
name so existing ``from telix.client_tui import ...`` statements continue to
work unchanged.
"""

# local
# Re-export module-level path constants so monkeypatch targets are valid.
from .paths import DATA_DIR, CONFIG_DIR, SESSIONS_FILE  # noqa: F401

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
    run_editor_app,
    pause_before_exit,
    normalize_encoding,
    restore_blocking_fds,
    log_child_diagnostics,
    read_primary_selection,
    handle_arrow_navigation,
    patch_writer_thread_queue,
)

# -- dialogs, rooms, caps, tabbed editor, main app -------------------------
from .client_tui_dialogs import (  # noqa: F401
    HOME_STYLE,
    ARROW_STYLE,
    EDITOR_TABS,
    ID_COL_BASE,
    MARKED_STYLE,
    BLOCKED_STYLE,
    NAME_COL_BASE,
    BOOKMARK_STYLE,
    BUTTON_COL_MIN,
    BUTTON_COL_GROW,
    CapsPane,
    RoomTree,
    CapsScreen,
    RoomBrowserPane,
    ChatViewerScreen,
    RoomPickerScreen,
    TelnetSessionApp,
    RoomBrowserScreen,
    TabbedEditorScreen,
    ConfirmDialogScreen,
    RandomwalkDialogScreen,
    AutodiscoverDialogScreen,
    tui_main,
    edit_rooms_main,
    chat_viewer_main,
    confirm_dialog_main,
    unified_editor_main,
    randomwalk_dialog_main,
    autodiscover_dialog_main,
)

# -- editor panes -----------------------------------------------------------
from .client_tui_editors import (  # noqa: F401
    MacroEditPane,
    ThemeEditPane,
    AutoreplyTuple,
    HighlightTuple,
    MacroEditScreen,
    ProgressBarTuple,
    AutoreplyEditPane,
    HighlightEditPane,
    AutoreplyEditScreen,
    HighlightEditScreen,
    ProgressBarEditPane,
    ProgressBarEditScreen,
    invert_ts,
    show_help_main,
    edit_macros_main,
    edit_highlights_main,
    edit_autoreplies_main,
    edit_progressbars_main,
)
