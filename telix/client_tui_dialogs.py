"""
Confirmation dialogs, walk dialogs, and the tabbed editor screen.

Contains ``ConfirmDialogScreen``, ``RandomwalkDialogScreen``,
``AutodiscoverDialogScreen``, ``TabbedEditorScreen``, and their
standalone entry points. Room browser, captures viewer, and main app
live in ``client_tui_rooms``, ``client_tui_captures``, and
``client_tui_app`` respectively.
"""

# std imports
import sys
import json
import typing
from typing import Any

# 3rd party
import textual.app
import textual.events
import textual.screen
import textual.binding
import textual.widgets
import textual.containers

# local
from . import client_tui_base, client_tui_editors
from .client_tui_app import TelnetSessionApp, tui_main  # noqa: F401

__all__ = ["TelnetSessionApp", "tui_main"]
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
from .client_tui_captures import CapsPane, CapsScreen, ChatViewerScreen, chat_viewer_main  # noqa: F401

EDITOR_TABS: list[tuple[str, str]] = [
    ("Highlights", "highlights"),
    ("Rooms", "rooms"),
    ("Macros", "macros"),
    ("Autoreplies", "autoreplies"),
    ("Chats", "captures"),
    ("Bars", "bars"),
    ("Theme", "theme"),
]

GLOBAL_TABS: set[str] = {"theme"}


class TabbedEditorScreen(textual.screen.Screen[None]):
    """Full-screen tabbed editor combining all editor panes."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("escape", "close_or_back", "Close", priority=True),
        textual.binding.Binding("f1", "show_help", "Help", show=False, priority=True),
    ]

    CSS = """
    TabbedEditorScreen {
        height: 100%;
        width: 100%;
    }
    #te-tab-bar {
        height: 1;
        background: $surface;
    }
    #te-tab-bar Button {
        min-width: 0;
        height: 1;
        margin: 0 0 0 0;
        padding: 0 1;
        border: none;
        background: $surface-lighten-1;
    }
    #te-tab-bar Button.active-tab {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    #te-content {
        height: 1fr;
    }
    """

    def __init__(self, params: dict[str, typing.Any]) -> None:
        """
        Initialize tabbed editor from a parameters dict.

        :param params: Dict with keys for each pane's constructor args, plus ``initial_tab``,
            ``initial_channel``, and ``hide_globals``.
        """
        super().__init__()
        self.params = params
        self.initial_tab = params.get("initial_tab", "highlights")
        self.hide_globals: bool = params.get("hide_globals", False)
        self.panes: dict[str, typing.Any] = {}
        self.loaded: set[str] = set()
        self.dirty: set[str] = set()

    def visible_tabs(self) -> list[tuple[str, str]]:
        """Return tabs filtered by ``hide_globals``."""
        if self.hide_globals:
            return [(label, tid) for label, tid in EDITOR_TABS if tid not in GLOBAL_TABS]
        return list(EDITOR_TABS)

    def compose(self) -> textual.app.ComposeResult:
        """Build tab bar and content switcher with lazy-loaded panes."""
        tabs = self.visible_tabs()
        with textual.containers.Horizontal(id="te-tab-bar"):
            for label, tab_id in tabs:
                btn = textual.widgets.Button(label, id=f"te-btn-{tab_id}")
                if tab_id == self.initial_tab:
                    btn.add_class("active-tab")
                yield btn

        with textual.widgets.ContentSwitcher(id="te-content", initial=self.initial_tab):
            for label, tab_id in tabs:
                pane = self.create_pane(tab_id)
                self.panes[tab_id] = pane
                yield pane

        yield textual.widgets.Footer()

    PANE_FACTORIES: typing.ClassVar[dict[str, tuple[type, dict[str, str]]]] = {
        "highlights": (client_tui_editors.HighlightEditPane, {"path": "highlights_file", "session_key": "session_key"}),
        "rooms": (
            RoomBrowserPane,
            {
                "rooms_path": "rooms_file",
                "session_key": "session_key",
                "current_room_file": "current_room_file",
                "fasttravel_file": "fasttravel_file",
            },
        ),
        "macros": (
            client_tui_editors.MacroEditPane,
            {
                "path": "macros_file",
                "session_key": "session_key",
                "rooms_file": "rooms_file",
                "current_room_file": "current_room_file",
            },
        ),
        "autoreplies": (
            client_tui_editors.AutoreplyEditPane,
            {
                "path": "autoreplies_file",
                "session_key": "session_key",
                "select_pattern": "select_pattern",
                "gmcp_snapshot_path": "gmcp_snapshot_file",
            },
        ),
        "captures": (
            CapsPane,
            {
                "chat_file": "chat_file",
                "session_key": "session_key",
                "initial_channel": "initial_channel",
                "capture_file": "capture_file",
            },
        ),
        "bars": (
            client_tui_editors.ProgressBarEditPane,
            {"path": "progressbars_file", "session_key": "session_key", "gmcp_snapshot_path": ("gmcp_snapshot_file")},
        ),
        "theme": (client_tui_editors.ThemeEditPane, {"session_key": "session_key"}),
    }

    def create_pane(self, tab_id: str) -> textual.containers.Vertical:
        """Instantiate the pane widget for *tab_id*."""
        cls, param_map = self.PANE_FACTORIES[tab_id]
        kwargs = {k: self.params.get(v, "") for k, v in param_map.items()}
        pane = cls(**kwargs)
        pane.id = tab_id
        return pane  # type: ignore[no-any-return]

    def on_mount(self) -> None:
        """Mark the initial tab as loaded and focus its default widget."""
        self.loaded.add(self.initial_tab)
        pane = self.panes.get(self.initial_tab)
        if pane and hasattr(pane, "focus_default"):
            self.call_after_refresh(pane.focus_default)

    def action_show_help(self) -> None:
        """Open the keybindings help screen."""
        self.app.push_screen(client_tui_base.CommandHelpScreen(topic="keybindings"))

    def on_key(self, event: textual.events.Key) -> None:
        """Left/right switch tabs only when a tab-bar button has focus."""
        if event.key not in ("left", "right"):
            return
        tab_buttons = list(self.query("#te-tab-bar Button"))
        if self.focused not in tab_buttons:
            return
        if event.key == "left":
            self.action_prev_tab()
        else:
            self.action_next_tab()
        event.prevent_default()

    def action_next_tab(self) -> None:
        """Switch to the next tab (wraps around)."""
        ids = [tid for _, tid in self.visible_tabs()]
        current = self.query_one("#te-content", textual.widgets.ContentSwitcher).current or ""
        idx = ids.index(current) if current in ids else 0
        self.action_switch_tab(ids[(idx + 1) % len(ids)])

    def action_prev_tab(self) -> None:
        """Switch to the previous tab (wraps around)."""
        ids = [tid for _, tid in self.visible_tabs()]
        current = self.query_one("#te-content", textual.widgets.ContentSwitcher).current or ""
        idx = ids.index(current) if current in ids else 0
        self.action_switch_tab(ids[(idx - 1) % len(ids)])

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch to *tab_id*, loading it lazily if needed."""
        switcher = self.query_one("#te-content", textual.widgets.ContentSwitcher)
        if switcher.current == tab_id:
            return
        switcher.current = tab_id
        for btn in self.query("#te-tab-bar Button"):
            btn.remove_class("active-tab")
            if btn.id == f"te-btn-{tab_id}":
                btn.add_class("active-tab")
        if tab_id not in self.loaded:
            self.loaded.add(tab_id)
            pane = self.panes.get(tab_id)
            if pane and hasattr(pane, "load_from_file"):
                pane.load_from_file()
                if hasattr(pane, "refresh_table"):
                    pane.refresh_table()
        pane = self.panes.get(tab_id)
        if pane and hasattr(pane, "focus_default"):
            self.call_after_refresh(pane.focus_default)

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle tab bar button clicks."""
        btn_id = event.button.id or ""
        if btn_id.startswith("te-btn-"):
            tab_id = btn_id[len("te-btn-") :]
            self.action_switch_tab(tab_id)

    def action_close_or_back(self) -> None:
        """Close form if visible in an editor pane, otherwise exit app."""
        current = self.query_one("#te-content", textual.widgets.ContentSwitcher).current
        pane = self.panes.get(current or "")
        if pane and hasattr(pane, "form_visible") and pane.form_visible:
            if hasattr(pane, "action_cancel_or_close"):
                pane.action_cancel_or_close()
            return
        self.save_all_dirty()
        self.app.exit()

    def save_all_dirty(self) -> None:
        """Auto-save any panes that have been loaded and have a save method."""
        for tab_id in self.loaded:
            pane = self.panes.get(tab_id)
            if pane and hasattr(pane, "save_to_file"):
                pane.save_to_file()

    def handle_travel(self) -> None:
        """Save all editors and exit when room travel is requested."""
        self.save_all_dirty()
        self.app.exit()


def run_unified_editor(params: dict[str, Any]) -> None:
    """
    Launch the tabbed editor in the current (worker) thread.

    Called by the REPL's :func:`~telix.client_repl_dialogs.launch_unified_editor` via a
    :class:`threading.Thread`.  FD blocking is already restored by the caller's
    :func:`~telix.terminal_unix.blocking_fds` context manager.

    :param params: Parameter dict for all editor panes (same structure as the JSON blob
        read by :func:`unified_editor_main`).
    """
    client_tui_base.launch_editor_in_thread(TabbedEditorScreen(params), session_key=params.get("session_key", ""))


def unified_editor_main() -> None:
    """
    Launch the tabbed editor TUI as a standalone process.

    Reads a single JSON blob from ``sys.argv[1]`` containing all parameters for every pane. Called
    from the REPL via ``launch_unified_editor()``.
    """
    params = json.loads(sys.argv[1])
    logfile = params.get("logfile", "")
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()

    screen = TabbedEditorScreen(params)
    session_key = params.get("session_key", "")
    app = client_tui_base.EditorApp(screen, session_key=session_key)  # type: ignore[arg-type]
    app.run()


class ConfirmDialogScreen(textual.screen.Screen[bool]):
    """Confirmation dialog with optional warning."""

    BINDINGS = [textual.binding.Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    ConfirmDialogScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: round $surface-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #confirm-body {
        margin-bottom: 1;
    }
    #confirm-warning {
        color: $error;
        margin-bottom: 1;
    }
    #confirm-buttons {
        height: 3;
        align-horizontal: right;
    }
    #confirm-buttons Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }
    """

    def __init__(
        self, title: str, body: str, warning: str = "", result_file: str = "", show_dont_ask: bool = True
    ) -> None:
        """Initialize confirm dialog with title, body, and optional warning."""
        super().__init__()
        self.title = title
        self.body = body
        self.warning = warning
        self.result_file = result_file
        self.show_dont_ask = show_dont_ask

    def compose(self) -> textual.app.ComposeResult:
        """Build the confirm dialog layout."""
        with textual.containers.Vertical(id="confirm-dialog"):
            yield textual.widgets.Static(self.title, id="confirm-title")  # type: ignore[arg-type]
            yield textual.widgets.Static(self.body, id="confirm-body")
            if self.warning:
                yield textual.widgets.Static(self.warning, id="confirm-warning")
            with textual.containers.Horizontal(id="confirm-buttons"):
                yield textual.widgets.Button("Cancel", variant="error", id="confirm-cancel")
                yield textual.widgets.Button("OK", variant="success", id="confirm-ok")

    def on_mount(self) -> None:
        """Focus OK button on mount."""
        self.query_one("#confirm-ok", textual.widgets.Button).focus()

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle OK/Cancel button presses."""
        if event.button.id == "confirm-ok":
            self.write_result(True)
            self.dismiss(True)
        elif event.button.id == "confirm-cancel":
            self.write_result(False)
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Handle Escape key."""
        self.write_result(False)
        self.dismiss(False)

    def write_result(self, confirmed: bool) -> None:
        """Write result to file for the parent process to read."""
        if not self.result_file:
            return
        result = json.dumps({"confirmed": confirmed})
        with open(self.result_file, "w", encoding="utf-8") as f:
            f.write(result)


def run_confirm_dialog(title: str, body: str, warning: str = "", result_file: str = "") -> None:
    """
    Launch the confirm dialog in the current (worker) thread.

    :param title: Dialog title.
    :param body: Body text.
    :param warning: Optional warning text displayed in red.
    :param result_file: Path to the JSON file where the result is written.
    """
    client_tui_base.launch_editor_in_thread(
        ConfirmDialogScreen(title=title, body=body, warning=warning, result_file=result_file)
    )


def confirm_dialog_main(title: str, body: str, warning: str = "", result_file: str = "", logfile: str = "") -> None:
    """Launch standalone confirm dialog TUI."""
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    screen = ConfirmDialogScreen(title=title, body=body, warning=warning, result_file=result_file)
    app = client_tui_base.EditorApp(screen)  # type: ignore[arg-type]
    app.run()


class RandomwalkDialogScreen(textual.screen.Screen[bool]):
    """Random walk confirmation dialog with visit-level parameter."""

    BINDINGS = [
        textual.binding.Binding("escape", "cancel", "Cancel", show=False),
        textual.binding.Binding("f1", "show_help", "Help", show=False),
    ]

    DEFAULT_CSS = """
    RandomwalkDialogScreen {
        align: center middle;
    }
    #rw-dialog {
        width: 100%;
        height: 100%;
        border: round $surface-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    #rw-title {
        text-style: bold;
        text-align: center;
    }
    #rw-body {
        margin-bottom: 1;
    }
    #rw-warning {
        color: $error;
        margin-bottom: 1;
    }
    #rw-hint {
        margin-bottom: 1;
    }
    #rw-options-col {
        height: auto;
        margin-bottom: 1;
    }
    .rw-option {
        height: 3;
        margin-bottom: 1;
    }
    .rw-option Label {
        padding-top: 1;
        width: auto;
        margin-right: 1;
    }
    .rw-option Input {
        width: 8;
    }
    .rw-option Switch {
        width: auto;
    }
    #rw-switches {
        height: auto;
        margin-bottom: 1;
        border: round $surface-lighten-2;
        padding: 0 1;
    }
    .rw-switch-row {
        height: 3;
    }
    .rw-switch-cell {
        width: 1fr;
        height: 3;
    }
    .rw-switch-cell Label {
        padding-top: 1;
        width: auto;
        margin-right: 1;
    }
    .rw-switch-cell Switch {
        width: auto;
    }
    #rw-error {
        color: $error;
        height: 1;
    }
    #rw-buttons {
        height: 3;
        align-horizontal: right;
    }
    #rw-buttons Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }
    """

    def __init__(
        self,
        result_file: str = "",
        default_visit_level: int = 2,
        default_auto_search: bool = False,
        default_auto_evaluate: bool = False,
        default_auto_survey: bool = False,
        default_autoreplies: bool = True,
    ) -> None:
        super().__init__()
        self.result_file = result_file
        self.default_visit_level = default_visit_level
        self.default_auto_search = default_auto_search
        self.default_auto_evaluate = default_auto_evaluate
        self.default_auto_survey = default_auto_survey
        self.default_autoreplies = default_autoreplies

    def compose(self) -> textual.app.ComposeResult:
        with textual.containers.Vertical(id="rw-dialog"):
            yield textual.widgets.Static("Random Walk", id="rw-title")
            yield textual.widgets.Static(
                "Random walk explores rooms by picking "
                "random exits, "
                "preferring unvisited rooms. It never "
                "returns through "
                "the entrance you came from. Autoreplies "
                "fire in each "
                "room. Stops when all reachable rooms are "
                "visited the "
                "required number of times.",
                id="rw-body",
            )
            yield textual.widgets.Static(
                "WARNING: This can lead to dangerous "
                "areas, death traps, or aggressive "
                "monsters! Your character may die. "
                "Use with caution.",
                id="rw-warning",
            )
            yield textual.widgets.Static(
                "Tip: Use the Block button in the Rooms screen to exclude dangerous areas.", id="rw-hint"
            )
            with textual.containers.Vertical(id="rw-options-col"), textual.containers.Horizontal(classes="rw-option"):
                lbl = textual.widgets.Label("Visit level:")
                lbl.tooltip = "Minimum number of times each reachable room must be visited before the walk stops."
                yield lbl
                yield textual.widgets.Input(value=str(self.default_visit_level), id="rw-visit-level", type="integer")
            with textual.containers.Vertical(id="rw-switches"):
                with textual.containers.Horizontal(classes="rw-switch-row"):
                    with textual.containers.Horizontal(classes="rw-switch-cell"):
                        yield textual.widgets.Label("Auto search:")
                        yield textual.widgets.Switch(value=(self.default_auto_search), id="rw-auto-search")
                    with textual.containers.Horizontal(classes="rw-switch-cell"):
                        yield textual.widgets.Label("Auto consider:")
                        yield textual.widgets.Switch(
                            value=(self.default_auto_evaluate),
                            id="rw-auto-consider",
                            disabled=(not self.default_autoreplies),
                        )
                with textual.containers.Horizontal(classes="rw-switch-row"):
                    with textual.containers.Horizontal(classes="rw-switch-cell"):
                        yield textual.widgets.Label("Auto survey:")
                        yield textual.widgets.Switch(
                            value=(self.default_auto_survey),
                            id="rw-auto-survey",
                            disabled=(not self.default_autoreplies),
                        )
                    with textual.containers.Horizontal(classes="rw-switch-cell"):
                        yield textual.widgets.Label("Autoreplies:")
                        yield textual.widgets.Switch(value=(self.default_autoreplies), id="rw-autoreplies")
            yield textual.widgets.Static("", id="rw-error")
            with textual.containers.Horizontal(id="rw-buttons"):
                yield textual.widgets.Button("Help", variant="primary", id="rw-help")
                yield textual.widgets.Button("Cancel", variant="error", id="rw-cancel")
                yield textual.widgets.Button("OK", variant="success", id="rw-ok")

    def on_mount(self) -> None:
        """Focus the OK button on mount."""
        self.query_one("#rw-ok", textual.widgets.Button).focus()

    def action_show_help(self) -> None:
        """Show room-mapping help screen."""
        self.app.push_screen(client_tui_base.CommandHelpScreen(topic="room-mapping"))

    def on_switch_changed(self, event: textual.widgets.Switch.Changed) -> None:
        """Disable consider/survey switches when autoreplies is OFF."""
        if event.switch.id == "rw-autoreplies":
            self.query_one("#rw-auto-consider", textual.widgets.Switch).disabled = not event.value
            self.query_one("#rw-auto-survey", textual.widgets.Switch).disabled = not event.value

    def validate_and_dismiss(self) -> None:
        raw = self.query_one("#rw-visit-level", textual.widgets.Input).value.strip()
        try:
            level = int(raw)
        except ValueError:
            self.query_one("#rw-error", textual.widgets.Static).update("Visit level must be a number.")
            return
        if level < 1:
            self.query_one("#rw-error", textual.widgets.Static).update("Visit level must be at least 1.")
            return
        auto_search = self.query_one("#rw-auto-search", textual.widgets.Switch).value
        auto_evaluate = self.query_one("#rw-auto-consider", textual.widgets.Switch).value
        auto_survey = self.query_one("#rw-auto-survey", textual.widgets.Switch).value
        autoreplies = self.query_one("#rw-autoreplies", textual.widgets.Switch).value
        self.write_result(True, level, auto_search, auto_evaluate, auto_survey, autoreplies)
        self.dismiss(True)

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle OK, Cancel, and Help button presses."""
        if event.button.id == "rw-help":
            self.action_show_help()
        elif event.button.id == "rw-ok":
            self.validate_and_dismiss()
        elif event.button.id == "rw-cancel":
            self.write_result(
                False,
                self.default_visit_level,
                self.default_auto_search,
                self.default_auto_evaluate,
                self.default_auto_survey,
                self.default_autoreplies,
            )
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Cancel the dialog and write default values."""
        self.write_result(
            False,
            self.default_visit_level,
            self.default_auto_search,
            self.default_auto_evaluate,
            self.default_auto_survey,
            self.default_autoreplies,
        )
        self.dismiss(False)

    def write_result(
        self,
        confirmed: bool,
        visit_level: int,
        auto_search: bool = False,
        auto_evaluate: bool = False,
        auto_survey: bool = False,
        autoreplies: bool = True,
    ) -> None:
        if not self.result_file:
            return
        cmd = f"`randomwalk 999 {visit_level}"
        if auto_search:
            cmd += " autosearch"
        if auto_evaluate:
            cmd += " autoevaluate"
        if auto_survey:
            cmd += " autosurvey"
        if not autoreplies:
            cmd += " noreply"
        cmd += "`"
        result = json.dumps(
            {
                "confirmed": confirmed,
                "visit_level": visit_level,
                "auto_search": auto_search,
                "auto_evaluate": auto_evaluate,
                "auto_survey": auto_survey,
                "autoreplies": autoreplies,
                "command": cmd,
            }
        )
        with open(self.result_file, "w", encoding="utf-8") as f:
            f.write(result)


def run_randomwalk_dialog(
    result_file: str,
    default_visit_level: int = 2,
    default_auto_search: bool = False,
    default_auto_evaluate: bool = False,
    default_auto_survey: bool = False,
    default_autoreplies: bool = True,
) -> None:
    """
    Launch the random walk dialog in the current (worker) thread.

    :param result_file: Path to the JSON file where the result is written.
    :param default_visit_level: Initial visit-level selection.
    :param default_auto_search: Initial auto-search toggle state.
    :param default_auto_evaluate: Initial auto-evaluate toggle state.
    :param default_auto_survey: Initial auto-survey toggle state.
    :param default_autoreplies: Initial autoreplies toggle state.
    """
    client_tui_base.launch_editor_in_thread(
        RandomwalkDialogScreen(
            result_file=result_file,
            default_visit_level=default_visit_level,
            default_auto_search=default_auto_search,
            default_auto_evaluate=default_auto_evaluate,
            default_auto_survey=default_auto_survey,
            default_autoreplies=default_autoreplies,
        )
    )


def randomwalk_dialog_main(
    result_file: str = "",
    default_visit_level: str = "2",
    default_auto_search: str = "0",
    default_auto_evaluate: str = "0",
    default_auto_survey: str = "0",
    default_autoreplies: str = "1",
    logfile: str = "",
) -> None:
    """Launch standalone random walk dialog TUI."""
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    screen = RandomwalkDialogScreen(
        result_file=result_file,
        default_visit_level=int(default_visit_level),
        default_auto_search=(default_auto_search == "1"),
        default_auto_evaluate=(default_auto_evaluate == "1"),
        default_auto_survey=(default_auto_survey == "1"),
        default_autoreplies=(default_autoreplies == "1"),
    )
    app = client_tui_base.EditorApp(screen)  # type: ignore[arg-type]
    app.run()


class AutodiscoverDialogScreen(textual.screen.Screen[bool]):
    """Autodiscover confirmation dialog with BFS/DFS strategy selection."""

    BINDINGS = [
        textual.binding.Binding("escape", "cancel", "Cancel", show=False),
        textual.binding.Binding("f1", "show_help", "Help", show=False),
    ]

    DEFAULT_CSS = """
    AutodiscoverDialogScreen {
        align: center middle;
    }
    #ad-dialog {
        width: 100%;
        height: 100%;
        border: round $surface-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    #ad-title {
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #ad-body {
        margin-bottom: 1;
    }
    #ad-warning {
        color: $error;
        margin-bottom: 1;
    }
    #ad-hint {
        margin-bottom: 1;
    }
    #ad-strategy-row {
        height: auto;
        margin-bottom: 1;
    }
    .ad-strategy {
        width: 1fr;
        height: auto;
    }
    #ad-switches {
        height: auto;
        margin-bottom: 1;
        border: round $surface-lighten-2;
        padding: 0 1;
    }
    .ad-switch-row {
        height: 3;
    }
    .ad-switch-cell {
        width: 1fr;
        height: 3;
    }
    .ad-switch-cell Label {
        padding-top: 1;
        width: auto;
        margin-right: 1;
    }
    .ad-switch-cell Switch {
        width: auto;
    }
    #ad-buttons {
        height: 3;
        align-horizontal: right;
    }
    #ad-buttons Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }
    """

    def __init__(
        self,
        result_file: str = "",
        default_strategy: str = "bfs",
        default_auto_search: bool = False,
        default_auto_evaluate: bool = False,
        default_auto_survey: bool = False,
        default_autoreplies: bool = True,
    ) -> None:
        super().__init__()
        self.result_file = result_file
        self.default_strategy = default_strategy
        self.default_auto_search = default_auto_search
        self.default_auto_evaluate = default_auto_evaluate
        self.default_auto_survey = default_auto_survey
        self.default_autoreplies = default_autoreplies

    def compose(self) -> textual.app.ComposeResult:
        with textual.containers.Vertical(id="ad-dialog"):
            yield textual.widgets.Static("Autodiscover", id="ad-title")
            yield textual.widgets.Static(
                "Autodiscover explores exits from nearby "
                "rooms "
                "that lead to unvisited places. It will "
                "travel "
                "to each frontier exit, check the room, "
                "then "
                "return before trying the next branch.",
                id="ad-body",
            )
            yield textual.widgets.Static(
                "WARNING: This can lead to dangerous "
                "areas, "
                "death traps, or aggressive monsters! "
                "Your "
                "character may die. Use with caution.",
                id="ad-warning",
            )
            yield textual.widgets.Static(
                "Tip: Use the Block button in the Rooms screen to exclude dangerous areas.", id="ad-hint"
            )
            with textual.containers.Horizontal(id="ad-strategy-row"), textual.widgets.RadioSet(id="ad-strategy-set"):
                yield textual.widgets.RadioButton(
                    "BFS: explore nearest exits first", id="ad-bfs", value=(self.default_strategy == "bfs")
                )
                yield textual.widgets.RadioButton(
                    "DFS: explore farthest exits first", id="ad-dfs", value=(self.default_strategy == "dfs")
                )
            with textual.containers.Vertical(id="ad-switches"):
                with textual.containers.Horizontal(classes="ad-switch-row"):
                    with textual.containers.Horizontal(classes="ad-switch-cell"):
                        yield textual.widgets.Label("Auto search:")
                        yield textual.widgets.Switch(value=(self.default_auto_search), id="ad-auto-search")
                    with textual.containers.Horizontal(classes="ad-switch-cell"):
                        yield textual.widgets.Label("Auto consider:")
                        yield textual.widgets.Switch(
                            value=(self.default_auto_evaluate),
                            id="ad-auto-consider",
                            disabled=(not self.default_autoreplies),
                        )
                with textual.containers.Horizontal(classes="ad-switch-row"):
                    with textual.containers.Horizontal(classes="ad-switch-cell"):
                        yield textual.widgets.Label("Auto survey:")
                        yield textual.widgets.Switch(
                            value=(self.default_auto_survey),
                            id="ad-auto-survey",
                            disabled=(not self.default_autoreplies),
                        )
                    with textual.containers.Horizontal(classes="ad-switch-cell"):
                        yield textual.widgets.Label("Autoreplies:")
                        yield textual.widgets.Switch(value=(self.default_autoreplies), id="ad-autoreplies")
            with textual.containers.Horizontal(id="ad-buttons"):
                yield textual.widgets.Button("Help", variant="primary", id="ad-help")
                yield textual.widgets.Button("Cancel", variant="error", id="ad-cancel")
                yield textual.widgets.Button("OK", variant="success", id="ad-ok")

    def on_mount(self) -> None:
        """Focus the OK button on mount."""
        self.query_one("#ad-ok", textual.widgets.Button).focus()

    def action_show_help(self) -> None:
        """Show room-mapping help screen."""
        self.app.push_screen(client_tui_base.CommandHelpScreen(topic="room-mapping"))

    def get_strategy(self) -> str:
        """Return the selected strategy string."""
        if self.query_one("#ad-dfs", textual.widgets.RadioButton).value:
            return "dfs"
        return "bfs"

    def on_switch_changed(self, event: textual.widgets.Switch.Changed) -> None:
        """Disable consider/survey switches when autoreplies is OFF."""
        if event.switch.id == "ad-autoreplies":
            self.query_one("#ad-auto-consider", textual.widgets.Switch).disabled = not event.value
            self.query_one("#ad-auto-survey", textual.widgets.Switch).disabled = not event.value

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle OK and Cancel button presses."""
        if event.button.id == "ad-help":
            self.action_show_help()
        elif event.button.id == "ad-ok":
            auto_search = self.query_one("#ad-auto-search", textual.widgets.Switch).value
            auto_evaluate = self.query_one("#ad-auto-consider", textual.widgets.Switch).value
            auto_survey = self.query_one("#ad-auto-survey", textual.widgets.Switch).value
            autoreplies = self.query_one("#ad-autoreplies", textual.widgets.Switch).value
            self.write_result(True, self.get_strategy(), auto_search, auto_evaluate, auto_survey, autoreplies)
            self.dismiss(True)
        elif event.button.id == "ad-cancel":
            self.write_result(
                False,
                self.default_strategy,
                self.default_auto_search,
                self.default_auto_evaluate,
                self.default_auto_survey,
                self.default_autoreplies,
            )
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Cancel the dialog and write default values."""
        self.write_result(
            False,
            self.default_strategy,
            self.default_auto_search,
            self.default_auto_evaluate,
            self.default_auto_survey,
            self.default_autoreplies,
        )
        self.dismiss(False)

    def write_result(
        self,
        confirmed: bool,
        strategy: str,
        auto_search: bool = False,
        auto_evaluate: bool = False,
        auto_survey: bool = False,
        autoreplies: bool = True,
    ) -> None:
        """Write result JSON to disk for the parent process."""
        if not self.result_file:
            return
        cmd = f"`autodiscover {strategy}"
        if auto_search:
            cmd += " autosearch"
        if auto_evaluate:
            cmd += " autoevaluate"
        if auto_survey:
            cmd += " autosurvey"
        if not autoreplies:
            cmd += " noreply"
        cmd += "`"
        result = json.dumps(
            {
                "confirmed": confirmed,
                "strategy": strategy,
                "auto_search": auto_search,
                "auto_evaluate": auto_evaluate,
                "auto_survey": auto_survey,
                "autoreplies": autoreplies,
                "command": cmd,
            }
        )
        with open(self.result_file, "w", encoding="utf-8") as f:
            f.write(result)


def run_autodiscover_dialog(
    result_file: str,
    default_strategy: str = "bfs",
    default_auto_search: bool = False,
    default_auto_evaluate: bool = False,
    default_auto_survey: bool = False,
    default_autoreplies: bool = True,
) -> None:
    """
    Launch the autodiscover dialog in the current (worker) thread.

    :param result_file: Path to the JSON file where the result is written.
    :param default_strategy: Initial strategy selection (``"bfs"`` or ``"dfs"``).
    :param default_auto_search: Initial auto-search toggle state.
    :param default_auto_evaluate: Initial auto-evaluate toggle state.
    :param default_auto_survey: Initial auto-survey toggle state.
    :param default_autoreplies: Initial autoreplies toggle state.
    """
    client_tui_base.launch_editor_in_thread(
        AutodiscoverDialogScreen(
            result_file=result_file,
            default_strategy=default_strategy,
            default_auto_search=default_auto_search,
            default_auto_evaluate=default_auto_evaluate,
            default_auto_survey=default_auto_survey,
            default_autoreplies=default_autoreplies,
        )
    )


def autodiscover_dialog_main(
    result_file: str = "",
    default_strategy: str = "bfs",
    default_auto_search: str = "0",
    default_auto_evaluate: str = "0",
    default_auto_survey: str = "0",
    default_autoreplies: str = "1",
    logfile: str = "",
) -> None:
    """Launch standalone autodiscover dialog TUI."""
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    screen = AutodiscoverDialogScreen(
        result_file=result_file,
        default_strategy=default_strategy,
        default_auto_search=(default_auto_search == "1"),
        default_auto_evaluate=(default_auto_evaluate == "1"),
        default_auto_survey=(default_auto_survey == "1"),
        default_autoreplies=(default_autoreplies == "1"),
    )
    app = client_tui_base.EditorApp(screen)  # type: ignore[arg-type]
    app.run()
