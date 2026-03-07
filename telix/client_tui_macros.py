"""Macro editor pane and screen for the telix TUI."""

# std imports
import os
import sys
from typing import Any, ClassVar

# 3rd party
import textual.app
import textual.binding
import textual.widgets
import textual.containers
import blessed.line_editor

# local
from . import macros, client_repl, client_tui_base


class MacroEditPane(client_tui_base.EditListPane):
    """Pane widget for macro key binding editing."""

    growable_keys: list[str] = ["toggle-text"]

    BINDINGS: ClassVar[list[textual.binding.Binding]] = [
        textual.binding.Binding("escape", "cancel_or_close", "Cancel", priority=True),
        textual.binding.Binding("f1", "show_help", "Help", show=True),
        textual.binding.Binding("plus", "reorder_hint", "Change Priority", key_display="+/=/-", show=True),
        textual.binding.Binding("enter", "save_hint", "Save", show=True),
        textual.binding.Binding("l", "sort_last", "Recent", show=True),
    ]

    DEFAULT_CSS = (
        client_tui_base.EditListPane.DEFAULT_CSS
        + """
    #macro-form { padding: 0; }
    #macro-toggle-text-row { margin: 0 0 1 0; }
    #macro-form .switch-row { height: 3; margin: 0; }
    #macro-key-label {
        width: 16; height: 1; padding: 0 1;
        margin: 1 0 0 1;
        background: $surface-darken-1; color: $text;
    }
    #macro-key-label.capturing { background: $error; color: $background; }
    #macro-capture { width: auto; min-width: 13; margin-left: 1; }
    #macro-capture-status { width: 1fr; height: 1; color: $error; padding: 0 1; }
    #macro-form .form-gap { width: 10; }
    .command-text-area { height: 1fr; min-height: 3; }
    """
    )

    def __init__(self, path: str, session_key: str = "", rooms_file: str = "", current_room_file: str = "") -> None:
        """Initialize macro editor with file path and session key."""
        super().__init__()
        self.path = path
        self.session_key = session_key
        self.rooms_path = rooms_file
        self.current_room_path = current_room_file
        self.macros: list[tuple[str, str, bool, str, bool, str, bool, str]] = []
        self.sort_mode: str = ""
        self.captured_key: str = ""

    @property
    def prefix(self) -> str:
        return "macro"

    @property
    def noun(self) -> str:
        return "Macro"

    @property
    def items(self) -> list[Any]:
        return self.macros

    def compose(self) -> textual.app.ComposeResult:
        """Build the macro editor layout."""
        with textual.containers.Vertical(id="macro-panel", classes="edit-panel"):
            with textual.containers.Horizontal(id="macro-body", classes="edit-body"):
                with textual.containers.Vertical(id="macro-button-col", classes="edit-button-col"):
                    yield textual.widgets.Button("Add", variant="success", id="macro-add")
                    yield textual.widgets.Button("Edit", variant="warning", id="macro-edit")
                    yield textual.widgets.Button("Copy", id="macro-copy", classes="edit-copy")
                    yield textual.widgets.Button("Delete", variant="error", id="macro-delete")
                    yield textual.widgets.Button("Help", variant="success", id="macro-help")
                    yield textual.widgets.Button("Save", variant="primary", id="macro-save")
                    yield textual.widgets.Button("Cancel", id="macro-close")
                with textual.containers.Vertical(id="macro-right", classes="edit-right"):
                    yield textual.widgets.Input(
                        placeholder="Search macros\u2026", id="macro-search", classes="edit-search"
                    )
                    yield textual.widgets.DataTable(id="macro-table", classes="edit-table")
                    with textual.containers.Vertical(id="macro-form", classes="edit-form"):
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Enabled:", classes="toggle-label")
                            yield textual.widgets.Switch(value=True, id="macro-enabled")
                            yield textual.widgets.Label("", classes="form-gap")
                            yield textual.widgets.Label("Key", classes="form-label-mid")
                            yield textual.widgets.Button("Capture", id="macro-capture")
                            yield textual.widgets.Static("(none)", id="macro-key-label")
                            yield textual.widgets.Static("", id="macro-capture-status")
                        with textual.containers.Horizontal(id="macro-toggle-row", classes="field-row"):
                            yield textual.widgets.Label("Toggle:", classes="toggle-label")
                            yield textual.widgets.Switch(value=False, id="macro-toggle")
                        with textual.containers.Horizontal(id="macro-toggle-text-row", classes="field-row"):
                            yield textual.widgets.Label("Off command", classes="form-label")
                            yield textual.widgets.Input(
                                placeholder="off command with ; separators", id="macro-toggle-text"
                            )
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Button("Travel", id="macro-fast-travel", classes="insert-btn")
                            yield textual.widgets.Button("Return", id="macro-return", classes="insert-btn")
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Button("Autodiscover", id="macro-autodiscover", classes="insert-btn")
                            yield textual.widgets.Button("Random Walk", id="macro-btn-randomwalk", classes="insert-btn")
                            yield textual.widgets.Button("Delay", id="macro-delay", classes="insert-btn")
                            yield textual.widgets.Button("When", id="macro-btn-when", classes="insert-btn")
                            yield textual.widgets.Button("Until", id="macro-btn-until", classes="insert-btn")
                        yield textual.widgets.Label("Command Text", classes="form-label", id="macro-text-label")
                        yield textual.widgets.TextArea(id="macro-text", classes="command-text-area")
                        with textual.containers.Horizontal(id="macro-form-buttons", classes="edit-form-buttons"):
                            yield textual.widgets.Label(" ", classes="form-btn-spacer")
                            yield textual.widgets.Button("Cancel", variant="default", id="macro-cancel-form")
                            yield textual.widgets.Button("OK", variant="success", id="macro-ok")
                    yield textual.widgets.Static("", id="macro-count")

    def on_mount(self) -> None:
        """Load macros from file and populate table."""
        table = self.query_one("#macro-table", textual.widgets.DataTable)
        table.cursor_type = "row"
        table.add_column("Key", width=14, key="key")
        table.add_column("Command Text", key="text")
        table.add_column("Last", width=8, key="last")
        self.load_from_file()
        self.refresh_table()
        self.query_one("#macro-form").display = False

    def load_from_file(self) -> None:
        loaded: list[macros.Macro] = []
        if os.path.exists(self.path):
            try:
                loaded = macros.load_macros(self.path, self.session_key)
            except (ValueError, FileNotFoundError):
                pass
        loaded = macros.ensure_builtin_macros(loaded)
        self.macros = [
            (m.key, m.text, m.enabled, m.last_used, m.toggle, m.toggle_text, m.builtin, m.builtin_name) for m in loaded
        ]

    def matches_search(self, idx: int, query: str) -> bool:
        """Match macro key, text, or toggle_text against search query."""
        key, text, _enabled, _lu, _toggle, toggle_text, _builtin, _bname = self.macros[idx]
        q = query.lower()
        return q in key.lower() or q in text.lower() or q in toggle_text.lower()

    def refresh_table(self) -> None:
        from . import client_tui_editors

        table = self.query_one("#macro-table", textual.widgets.DataTable)
        table.clear()
        q = self.search_query
        self.filtered_indices = []
        order = list(range(len(self.macros)))
        if self.sort_mode == "last_used":
            order.sort(key=lambda i: client_tui_editors.invert_ts(self.macros[i][3]))
        for i in order:
            key, text, enabled, last_used, toggle, toggle_text, builtin, bname = self.macros[i]
            if q and not self.matches_search(i, q):
                continue
            status = "" if enabled else " (off)"
            if toggle:
                status += " (toggle)"
            if builtin:
                status += " (builtin)"
            lu = client_tui_base.relative_time(last_used) if last_used else ""
            self.filtered_indices.append(i)
            table.add_row(key, text + status, lu, key=str(len(self.filtered_indices) - 1))
        self.update_count_label()

    def action_sort_last(self) -> None:
        """Toggle sorting macros by last used time."""
        self.sort_mode = "last_used" if self.sort_mode != "last_used" else ""
        self.refresh_table()

    def show_form(
        self,
        key_val: str = "",
        text_val: str = "",
        enabled: bool = True,
        last_used: str = "",
        toggle: bool = False,
        toggle_text: str = "",
        builtin: bool = False,
        builtin_name: str = "",
    ) -> None:
        self.captured_key = key_val
        label = self.query_one("#macro-key-label", textual.widgets.Static)
        display = self.blessed_display(key_val) if key_val else "(none)"
        label.update(display)
        label.remove_class("capturing")
        self.query_one("#macro-capture-status", textual.widgets.Static).update("")
        text_area = self.query_one("#macro-text", textual.widgets.TextArea)
        text_area.text = text_val
        text_area.disabled = builtin
        self.query_one("#macro-enabled", textual.widgets.Switch).value = enabled
        toggle_switch = self.query_one("#macro-toggle", textual.widgets.Switch)
        toggle_switch.value = toggle
        toggle_switch.disabled = builtin
        toggle_text_input = self.query_one("#macro-toggle-text", textual.widgets.Input)
        toggle_text_input.value = toggle_text
        toggle_text_input.disabled = builtin
        text_label = self.query_one("#macro-text-label", textual.widgets.Label)
        text_label.update("On command" if toggle else "Command Text")
        self.query_one("#macro-toggle-text-row").display = toggle
        self.query_one("#macro-search", textual.widgets.Input).display = False
        self.query_one("#macro-table").display = False
        self.query_one("#macro-form").display = True
        self.set_action_buttons_disabled(True)
        if builtin:
            self.query_one("#macro-capture", textual.widgets.Button).focus()
        else:
            text_area.focus()

    def action_delete(self) -> None:
        """Block deletion of builtin macros."""
        idx = self.selected_idx()
        if idx is not None and idx < len(self.macros) and self.macros[idx][6]:
            self.notify("Cannot delete a builtin macro.")
            return
        super().action_delete()

    def on_switch_changed(self, event: textual.widgets.Switch.Changed) -> None:
        """Show/hide toggle text row when the toggle switch changes."""
        if event.switch.id == "macro-toggle":
            on = event.value
            text_label = self.query_one("#macro-text-label", textual.widgets.Label)
            text_label.update("On command" if on else "Command Text")
            self.query_one("#macro-toggle-text-row").display = on

    def submit_form(self) -> None:
        """Accept the current inline form values."""
        key_val = self.captured_key.strip()
        text_val = self.query_one("#macro-text", textual.widgets.TextArea).text
        enabled = self.query_one("#macro-enabled", textual.widgets.Switch).value
        toggle = self.query_one("#macro-toggle", textual.widgets.Switch).value
        toggle_text = self.query_one("#macro-toggle-text", textual.widgets.Input).value
        lu = self.macros[self.editing_idx][3] if self.editing_idx is not None else ""
        builtin = self.macros[self.editing_idx][6] if self.editing_idx is not None else False
        bname = self.macros[self.editing_idx][7] if self.editing_idx is not None else ""
        self.finalize_edit((key_val, text_val, enabled, lu, toggle, toggle_text, builtin, bname), bool(key_val))

    @staticmethod
    def blessed_display(blessed_name: str) -> str:
        r"""Format a blessed key name for display (strip KEY\_ prefix)."""
        if blessed_name.startswith("KEY_"):
            return blessed_name[4:]
        return blessed_name

    def _blessed_capture(self) -> None:
        """Suspend textual and use blessed.inkey() to capture a keystroke."""
        bt = client_repl.get_term()
        result_key = ""
        result_display = ""
        rejected = ""
        with self.app.suspend():
            os.set_blocking(sys.stdin.fileno(), True)
            with bt.raw():
                key = bt.inkey()
            if key.name == "KEY_ESCAPE":
                pass
            elif key.name:
                blessed_name = key.name
                display = self.blessed_display(blessed_name)
                if blessed_name in blessed.line_editor.DEFAULT_KEYMAP:
                    rejected = f"Rejected: {display} -- reserved by line editor"
                else:
                    result_key = blessed_name
                    result_display = display
            elif len(str(key)) == 1 and str(key).isprintable():
                rejected = f"Rejected: '{key}' -- use F-keys, Ctrl+key, or Alt+key"
            else:
                rejected = "Rejected: unknown key"
        label = self.query_one("#macro-key-label", textual.widgets.Static)
        label.remove_class("capturing")
        if result_key:
            self.captured_key = result_key
            label.update(result_display)
            self.query_one("#macro-capture-status", textual.widgets.Static).update("")
        elif rejected:
            self.query_one("#macro-capture-status", textual.widgets.Static).update(rejected)

    def do_extra_button(self, suffix: str, btn: str) -> None:
        """Handle macro-specific buttons (travel, capture, etc.)."""
        if suffix == "fast-travel":
            self.pick_room_for_travel()
        elif suffix == "capture":
            label = self.query_one("#macro-key-label", textual.widgets.Static)
            label.add_class("capturing")
            self._blessed_capture()
        else:
            super().do_extra_button(suffix, btn)

    def save_to_file(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        macro_list = [
            macros.Macro(
                key=k, text=t, enabled=ena, last_used=lu, toggle=tog, toggle_text=tt, builtin=bi, builtin_name=bn
            )
            for k, t, ena, lu, tog, tt, bi, bn in self.macros
        ]
        macros.save_macros(self.path, macro_list, self.session_key)


class MacroEditScreen(client_tui_base.EditListScreen):
    """Thin screen wrapper for the macro editor."""

    def __init__(self, path: str, session_key: str = "", rooms_file: str = "", current_room_file: str = "") -> None:
        super().__init__()
        self.pane = MacroEditPane(
            path=path, session_key=session_key, rooms_file=rooms_file, current_room_file=current_room_file
        )
