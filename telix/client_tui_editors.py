"""
Concrete editor panes for the Textual TUI.

Contains MacroEditPane, AutoreplyEditPane, HighlightEditPane,
ProgressBarEditPane, their screen wrappers, and standalone entry points.
Imports from ``client_tui_base`` only (no circular deps).
"""

# std imports
import os
import re
import sys
import json
import shutil
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

# 3rd party
import rich.text
import textual.app
import textual.theme
import textual.events
import textual.binding
import textual.widgets
import textual.css.query
import textual.containers
import blessed.line_editor
import textual.widgets.data_table

# local
from . import macros, autoreply, client_repl, highlighter, progressbars, client_tui_base, client_repl_render

if TYPE_CHECKING:
    RichText = rich.text.Text


def invert_ts(iso_str: str) -> str:
    """
    Return a sort key that orders ISO timestamps most-recent-first.

    Empty strings sort last (after any real timestamp).
    """
    if not iso_str:
        return "\xff"
    digits = "".join(c for c in iso_str if c.isdigit())
    return "".join(chr(ord("9") - ord(c) + ord("0")) for c in digits)


# ---------------------------------------------------------------------------
# Macro editor
# ---------------------------------------------------------------------------


class MacroEditPane(client_tui_base.EditListPane):
    """Pane widget for macro key binding editing."""

    growable_keys: list[str] = ["text", "toggle-text"]

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
    #macro-text-row { margin: 1 0; }
    #macro-toggle-text-row { margin: 0 0 1 0; }
    #macro-form .switch-row { height: 3; margin: 0; }
    #macro-key-label {
        width: 16; height: 1; padding: 0 1;
        margin: 1 0 0 1;
        background: $surface-darken-1; color: $text;
    }
    #macro-key-label.capturing { color: $warning; }
    #macro-capture { width: auto; min-width: 13; margin-left: 1; }
    #macro-capture-status { width: 1fr; height: 1; color: $error; padding: 0 1; }
    #macro-form .form-gap { width: 10; }
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
                        with textual.containers.Horizontal(id="macro-text-row", classes="field-row"):
                            yield textual.widgets.Label("Text", classes="form-label", id="macro-text-label")
                            yield textual.widgets.Input(placeholder="text with ; separators", id="macro-text")
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
            (m.key, m.text, m.enabled, m.last_used, m.toggle, m.toggle_text, m.builtin, m.builtin_name)
            for m in loaded
        ]

    def matches_search(self, idx: int, query: str) -> bool:
        """Match macro key, text, or toggle_text against search query."""
        key, text, _enabled, _lu, _toggle, toggle_text, _builtin, _bname = self.macros[idx]
        q = query.lower()
        return q in key.lower() or q in text.lower() or q in toggle_text.lower()

    def refresh_table(self) -> None:
        table = self.query_one("#macro-table", textual.widgets.DataTable)
        table.clear()
        q = self.search_query
        self.filtered_indices = []
        order = list(range(len(self.macros)))
        if self.sort_mode == "last_used":
            order.sort(key=lambda i: invert_ts(self.macros[i][3]))
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
        text_input = self.query_one("#macro-text", textual.widgets.Input)
        text_input.value = text_val
        text_input.disabled = builtin
        self.query_one("#macro-enabled", textual.widgets.Switch).value = enabled
        toggle_switch = self.query_one("#macro-toggle", textual.widgets.Switch)
        toggle_switch.value = toggle
        toggle_switch.disabled = builtin
        toggle_text_input = self.query_one("#macro-toggle-text", textual.widgets.Input)
        toggle_text_input.value = toggle_text
        toggle_text_input.disabled = builtin
        text_label = self.query_one("#macro-text-label", textual.widgets.Label)
        text_label.update("On command" if toggle else "Text")
        self.query_one("#macro-toggle-text-row").display = toggle
        self.query_one("#macro-search", textual.widgets.Input).display = False
        self.query_one("#macro-table").display = False
        self.query_one("#macro-form").display = True
        self.set_action_buttons_disabled(True)
        if builtin:
            self.query_one("#macro-capture", textual.widgets.Button).focus()
        else:
            text_input.focus()

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
            text_label.update("On command" if on else "Text")
            self.query_one("#macro-toggle-text-row").display = on

    def submit_form(self) -> None:
        """Accept the current inline form values."""
        key_val = self.captured_key.strip()
        text_val = self.query_one("#macro-text", textual.widgets.Input).value
        enabled = self.query_one("#macro-enabled", textual.widgets.Switch).value
        toggle = self.query_one("#macro-toggle", textual.widgets.Switch).value
        toggle_text = self.query_one("#macro-toggle-text", textual.widgets.Input).value
        lu = self.macros[self.editing_idx][3] if self.editing_idx is not None else ""
        builtin = self.macros[self.editing_idx][6] if self.editing_idx is not None else False
        bname = self.macros[self.editing_idx][7] if self.editing_idx is not None else ""
        self.finalize_edit(
            (key_val, text_val, enabled, lu, toggle, toggle_text, builtin, bname),
            bool(key_val),
        )

    @staticmethod
    def blessed_display(blessed_name: str) -> str:
        r"""Format a blessed key name for display (strip KEY\\_ prefix)."""
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
            sys.stdout.write("\r\nPress a key to capture (Esc to cancel) ... ")
            sys.stdout.flush()
            with bt.raw():
                key = bt.inkey()
            sys.stdout.write("\r\n")
            sys.stdout.flush()
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
        self.screen.refresh()
        if result_key:
            self.captured_key = result_key
            label = self.query_one("#macro-key-label", textual.widgets.Static)
            label.update(result_display)
            label.remove_class("capturing")
            self.query_one("#macro-capture-status", textual.widgets.Static).update("")
        elif rejected:
            self.query_one("#macro-capture-status", textual.widgets.Static).update(rejected)

    def do_extra_button(self, suffix: str, btn: str) -> None:
        """Handle macro-specific buttons (travel, capture, etc.)."""
        if suffix == "fast-travel":
            self.pick_room_for_travel()
        elif suffix == "capture":
            self._blessed_capture()
        else:
            super()._on_extra_button(suffix, btn)

    def save_to_file(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        macro_list = [
            macros.Macro(
                key=k, text=t, enabled=ena, last_used=lu,
                toggle=tog, toggle_text=tt, builtin=bi, builtin_name=bn,
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


# ---------------------------------------------------------------------------
# Autoreply editor
# ---------------------------------------------------------------------------


class AutoreplyTuple(NamedTuple):
    """Lightweight tuple for autoreply rules in the TUI editor."""

    pattern: str
    reply: str
    always: bool = False
    enabled: bool = True
    when: dict[str, str] | None = None
    immediate: bool = False
    last_fired: str = ""
    case_sensitive: bool = False


class AutoreplyEditPane(client_tui_base.EditListPane):
    """Pane widget for autoreply rule editing."""

    growable_keys: list[str] = ["pattern", "reply"]

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
    #autoreply-form { padding: 0 0 0 4; }
    #autoreply-form .form-label { width: 12; }
    #autoreply-form .form-label-mid { width: 9; }
    #autoreply-form .insert-btn { margin: 0; padding: 0 1; }
    #autoreply-cond-vital { width: 14; }
    #autoreply-cond-op { width: 8; }
    #autoreply-cond-val { width: 9; border: tall grey; }
    #autoreply-cond-val:focus { border: tall $accent; }
    """
    )

    def __init__(self, path: str, session_key: str = "", select_pattern: str = "") -> None:
        """Initialize autoreply editor with file path and session key."""
        super().__init__()
        self.path = path
        self.session_key = session_key
        self.select_pattern = select_pattern
        self.rules: list[AutoreplyTuple] = []
        self.sort_mode: str = ""
        self.rooms_path = os.path.join(os.path.dirname(path), "rooms.json")

    @property
    def prefix(self) -> str:
        return "autoreply"

    @property
    def noun(self) -> str:
        return "Autoreply"

    @property
    def noun_plural(self) -> str:
        return "Autoreplies"

    @property
    def items(self) -> list[Any]:
        return self.rules

    @property
    def text_input_id(self) -> str:
        return "autoreply-reply"

    def compose(self) -> textual.app.ComposeResult:
        """Build the autoreply editor layout."""
        with textual.containers.Vertical(id="autoreply-panel", classes="edit-panel"):
            with textual.containers.Horizontal(id="autoreply-body", classes="edit-body"):
                with textual.containers.Vertical(id="autoreply-button-col", classes="edit-button-col"):
                    yield textual.widgets.Button("Add", variant="success", id="autoreply-add")
                    yield textual.widgets.Button("Edit", variant="warning", id="autoreply-edit")
                    yield textual.widgets.Button("Copy", id="autoreply-copy", classes="edit-copy")
                    yield textual.widgets.Button("Delete", variant="error", id="autoreply-delete")
                    yield textual.widgets.Button("Help", variant="success", id="autoreply-help")
                    yield textual.widgets.Button("Save", variant="primary", id="autoreply-save")
                    yield textual.widgets.Button("Cancel", id="autoreply-close")
                with textual.containers.Vertical(id="autoreply-right", classes="edit-right"):
                    yield textual.widgets.Input(
                        placeholder="Search autoreplies\u2026", id="autoreply-search", classes="edit-search"
                    )
                    yield textual.widgets.DataTable(id="autoreply-table", classes="edit-table")
                    with textual.containers.Vertical(id="autoreply-form", classes="edit-form"):
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Enabled:", classes="toggle-label")
                            yield textual.widgets.Switch(value=True, id="autoreply-enabled")
                            yield textual.widgets.Label("", classes="toggle-gap")
                            alw = textual.widgets.Switch(value=False, id="autoreply-always")
                            alw.tooltip = "Match even while another rule's chain is active"
                            yield textual.widgets.Label("Always:", classes="toggle-label")
                            yield alw
                            yield textual.widgets.Label("", classes="toggle-gap")
                            imm = textual.widgets.Switch(value=False, id="autoreply-immediate")
                            imm.tooltip = "Reply immediately without waiting for prompt"
                            yield textual.widgets.Label("Immediate:", classes="toggle-label")
                            yield imm
                            yield textual.widgets.Label("", classes="toggle-gap")
                            cs = textual.widgets.Switch(value=False, id="autoreply-case-sensitive")
                            cs.tooltip = "Case-sensitive pattern matching"
                            yield textual.widgets.Label("Case Sensitive:", classes="toggle-label")
                            yield cs
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Pattern", classes="form-label-short")
                            yield textual.widgets.Input(placeholder="regex pattern", id="autoreply-pattern")
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Reply", classes="form-label-short")
                            yield textual.widgets.Input(
                                placeholder=r"reply with \1 refs, ;/: seps", id="autoreply-reply"
                            )
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Condition", classes="form-label-short")
                            yield textual.widgets.Select(
                                [("(none)", ""), ("HP%", "HP%"), ("MP%", "MP%"), ("HP", "HP"), ("MP", "MP")],
                                value="",
                                allow_blank=False,
                                id="autoreply-cond-vital",
                            )
                            yield textual.widgets.Select(
                                [(">", ">"), ("<", "<"), (">=", ">="), ("<=", "<="), ("=", "=")],
                                value=">",
                                allow_blank=False,
                                id="autoreply-cond-op",
                            )
                            yield textual.widgets.Input(value="99", placeholder="99", id="autoreply-cond-val")
                            yield textual.widgets.Label("(as percent)", classes="form-label-pct")
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Button("When", id="autoreply-btn-when", classes="insert-btn")
                            yield textual.widgets.Button("Until", id="autoreply-btn-until", classes="insert-btn")
                            yield textual.widgets.Button("Delay", id="autoreply-btn-delay", classes="insert-btn")
                            yield textual.widgets.Button("Travel", id="autoreply-fast-travel", classes="insert-btn")
                            yield textual.widgets.Button("Return", id="autoreply-return", classes="insert-btn")
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Button(
                                "Autodiscover", id="autoreply-autodiscover", classes="insert-btn"
                            )
                            yield textual.widgets.Button(
                                "Random Walk", id="autoreply-btn-randomwalk", classes="insert-btn"
                            )
                        with textual.containers.Horizontal(id="autoreply-form-buttons", classes="edit-form-buttons"):
                            yield textual.widgets.Label(" ", classes="form-btn-spacer")
                            yield textual.widgets.Button("Cancel", variant="default", id="autoreply-cancel-form")
                            yield textual.widgets.Button("OK", variant="success", id="autoreply-ok")
                    yield textual.widgets.Static("", id="autoreply-count")

    def on_mount(self) -> None:
        """Load autoreplies from file and populate table."""
        table = self.query_one("#autoreply-table", textual.widgets.DataTable)
        table.cursor_type = "row"
        term_w = self.app.size.width
        col_w = max(15, 15 + (term_w - 80) // 2) if term_w > 80 else 15
        table.add_column("#", width=4, key="num")
        table.add_column("Pattern", width=col_w, key="pattern")
        table.add_column("Reply", width=col_w, key="reply")
        table.add_column("Flags", width=8, key="flags")
        table.add_column("Last", width=8, key="last")
        self.load_from_file()
        self.refresh_table()
        self.query_one("#autoreply-form").display = False
        if self.select_pattern:
            for i, (pattern, *rest) in enumerate(self.rules):
                if pattern == self.select_pattern:
                    table.move_cursor(row=i)
                    break

    def load_from_file(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            rules = autoreply.load_autoreplies(self.path, self.session_key)
            self.rules = [
                AutoreplyTuple(
                    r.pattern.pattern,
                    r.reply,
                    r.always,
                    r.enabled,
                    dict(r.when) or None,
                    r.immediate,
                    r.last_fired,
                    r.case_sensitive,
                )
                for r in rules
            ]
        except (ValueError, FileNotFoundError):
            pass

    def matches_search(self, idx: int, query: str) -> bool:
        """Match autoreply pattern or reply against search query."""
        rule = self.rules[idx]
        q = query.lower()
        return q in rule.pattern.lower() or q in rule.reply.lower()

    def refresh_table(self) -> None:
        table = self.query_one("#autoreply-table", textual.widgets.DataTable)
        table.clear()
        q = self.search_query
        self.filtered_indices = []
        order = list(range(len(self.rules)))
        if self.sort_mode == "last_fired":
            order.sort(key=lambda i: invert_ts(self.rules[i].last_fired))
        for i in order:
            rule = self.rules[i]
            if q and not self.matches_search(i, q):
                continue
            self.filtered_indices.append(i)
            flags = " ".join(
                filter(
                    None,
                    [
                        "X" if not rule.enabled else "",
                        "A" if rule.always else "",
                        "I" if rule.immediate else "",
                        "C" if rule.case_sensitive else "",
                        "W" if rule.when else "",
                    ],
                )
            )
            lf = client_tui_base.relative_time(rule.last_fired) if rule.last_fired else ""
            row_pos = len(self.filtered_indices) - 1
            table.add_row(str(i + 1), rule.pattern, rule.reply, flags.strip(), lf, key=str(row_pos))
        self.update_count_label()

    def action_sort_last(self) -> None:
        """Toggle sorting autoreplies by last fired time."""
        self.sort_mode = "last_fired" if self.sort_mode != "last_fired" else ""
        self.refresh_table()

    def show_form(
        self,
        pattern_val: str = "",
        reply_val: str = "",
        always: bool = False,
        enabled: bool = True,
        when: dict[str, str] | None = None,
        immediate: bool = False,
        last_fired: str = "",
        case_sensitive: bool = False,
    ) -> None:
        self.query_one("#autoreply-pattern", textual.widgets.Input).value = pattern_val
        self.query_one("#autoreply-reply", textual.widgets.Input).value = reply_val
        self.query_one("#autoreply-always", textual.widgets.Switch).value = always
        self.query_one("#autoreply-enabled", textual.widgets.Switch).value = enabled
        self.query_one("#autoreply-immediate", textual.widgets.Switch).value = immediate
        self.query_one("#autoreply-case-sensitive", textual.widgets.Switch).value = case_sensitive
        cond_vital, cond_op, cond_val = "", ">", "99"
        if when:
            vital = next(iter(when), "")
            expr = when.get(vital, ">99")
            if m := re.match(r"^(>=|<=|>|<|=)(\d+)$", expr):
                cond_vital, cond_op, cond_val = vital, m.group(1), m.group(2)
        self.query_one("#autoreply-cond-vital", textual.widgets.Select).value = cond_vital
        self.query_one("#autoreply-cond-op", textual.widgets.Select).value = cond_op
        self.query_one("#autoreply-cond-val", textual.widgets.Input).value = cond_val
        cond_none = not when
        self.query_one("#autoreply-cond-op", textual.widgets.Select).disabled = cond_none
        self.query_one("#autoreply-cond-val", textual.widgets.Input).disabled = cond_none
        self.query_one("#autoreply-search", textual.widgets.Input).display = False
        self.query_one("#autoreply-table").display = False
        self.query_one("#autoreply-form").display = True
        self.set_action_buttons_disabled(True)
        self.query_one("#autoreply-pattern", textual.widgets.Input).focus()

    def submit_form(self) -> None:
        """Accept the current inline form values."""
        pattern_val = self.query_one("#autoreply-pattern", textual.widgets.Input).value.strip()
        reply_val = self.query_one("#autoreply-reply", textual.widgets.Input).value
        always = self.query_one("#autoreply-always", textual.widgets.Switch).value
        enabled = self.query_one("#autoreply-enabled", textual.widgets.Switch).value
        immediate = self.query_one("#autoreply-immediate", textual.widgets.Switch).value
        case_sensitive = self.query_one("#autoreply-case-sensitive", textual.widgets.Switch).value
        cond_vital = self.query_one("#autoreply-cond-vital", textual.widgets.Select).value
        cond_op = self.query_one("#autoreply-cond-op", textual.widgets.Select).value
        cond_val = self.query_one("#autoreply-cond-val", textual.widgets.Input).value.strip()
        when: dict[str, str] | None = None
        if cond_vital and isinstance(cond_vital, str) and cond_vital in ("HP%", "MP%", "HP", "MP"):
            try:
                int(cond_val or "99")
            except ValueError:
                cond_val = "99"
            when = {cond_vital: f"{cond_op}{cond_val or '99'}"}
        if pattern_val:
            try:
                re.compile(pattern_val)
            except re.error as exc:
                self.notify(f"Invalid regex: {exc}", severity="error")
                return
        lf = self.rules[self.editing_idx].last_fired if self.editing_idx is not None else ""
        entry = AutoreplyTuple(pattern_val, reply_val, always, enabled, when, immediate, lf, case_sensitive)
        self.finalize_edit(entry, bool(pattern_val))

    def on_select_changed(self, event: textual.widgets.Select.Changed) -> None:
        """Disable operator/value fields when condition vital is '(none)'."""
        if event.select.id == "autoreply-cond-vital":
            disabled = not event.value or event.value is textual.widgets.Select.BLANK
            self.query_one("#autoreply-cond-op", textual.widgets.Select).disabled = disabled
            self.query_one("#autoreply-cond-val", textual.widgets.Input).disabled = disabled

    def do_extra_button(self, suffix: str, btn: str) -> None:
        """Handle autoreply-specific buttons (travel, etc.)."""
        if suffix == "fast-travel":
            self.pick_room_for_travel()
        else:
            super()._on_extra_button(suffix, btn)

    def save_to_file(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        rules = []
        for t in self.rules:
            flags = re.MULTILINE | re.DOTALL
            if not t.case_sensitive:
                flags |= re.IGNORECASE
            rules.append(
                autoreply.AutoreplyRule(
                    pattern=re.compile(t.pattern, flags),
                    reply=t.reply,
                    always=t.always,
                    enabled=t.enabled,
                    when=t.when or {},
                    immediate=t.immediate,
                    last_fired=t.last_fired,
                    case_sensitive=t.case_sensitive,
                )
            )
        autoreply.save_autoreplies(self.path, rules, self.session_key)


class AutoreplyEditScreen(client_tui_base.EditListScreen):
    """Thin screen wrapper for the autoreply editor."""

    def __init__(self, path: str, session_key: str = "", select_pattern: str = "") -> None:
        super().__init__()
        self.pane = AutoreplyEditPane(path=path, session_key=session_key, select_pattern=select_pattern)


# ---------------------------------------------------------------------------
# Highlight editor
# ---------------------------------------------------------------------------


class HighlightTuple(NamedTuple):
    """Lightweight tuple for highlight rules in the TUI editor."""

    pattern: str
    highlight: str
    enabled: bool = True
    stop_movement: bool = False
    builtin: bool = False
    case_sensitive: bool = False
    multiline: bool = False
    captured: bool = False
    capture_name: str = "captures"
    captures: tuple[dict[str, str], ...] = ()


class HighlightEditPane(client_tui_base.EditListPane):
    """Pane widget for highlight rule editing."""

    growable_keys: list[str] = ["pattern"]

    DEFAULT_CSS = (
        client_tui_base.EditListPane.DEFAULT_CSS
        + """
    #highlight-form { padding: 0 0 0 4; }
    #highlight-form .form-label { width: 12; }
    """
    )

    def __init__(self, path: str, session_key: str = "") -> None:
        """Initialize highlight editor for *path*."""
        super().__init__()
        self.path = path
        self.session_key = session_key
        self.rules: list[HighlightTuple] = []

    @property
    def prefix(self) -> str:
        return "highlight"

    @property
    def noun(self) -> str:
        return "Highlight"

    @property
    def items(self) -> list[Any]:
        return self.rules

    def item_label(self, idx: int) -> str:
        if idx < len(self.rules):
            return self.rules[idx].pattern
        return ""

    def compose(self) -> textual.app.ComposeResult:
        """Build the highlight editor widget tree."""
        with textual.containers.Vertical(id="highlight-panel", classes="edit-panel"):
            with textual.containers.Horizontal(id="highlight-body", classes="edit-body"):
                with textual.containers.Vertical(id="highlight-button-col", classes="edit-button-col"):
                    yield textual.widgets.Button("Add", variant="success", id="highlight-add")
                    yield textual.widgets.Button("Edit", variant="warning", id="highlight-edit")
                    yield textual.widgets.Button("Copy", id="highlight-copy", classes="edit-copy")
                    yield textual.widgets.Button("Delete", variant="error", id="highlight-delete")
                    yield textual.widgets.Button("Help", variant="success", id="highlight-help")
                    yield textual.widgets.Button("Save", variant="primary", id="highlight-save")
                    yield textual.widgets.Button("Cancel", id="highlight-close")
                with textual.containers.Vertical(id="highlight-right", classes="edit-right"):
                    yield textual.widgets.Input(
                        placeholder="Search highlights\u2026", id="highlight-search", classes="edit-search"
                    )
                    yield textual.widgets.DataTable(id="highlight-table", classes="edit-table")
                    with textual.containers.Vertical(id="highlight-form", classes="edit-form"):
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Enabled:", classes="toggle-label")
                            yield textual.widgets.Switch(value=True, id="highlight-enabled")
                            yield textual.widgets.Label("", classes="toggle-gap")
                            sm = textual.widgets.Switch(value=False, id="highlight-stop-movement")
                            sm.tooltip = "Cancel autodiscover/randomwalk when matched"
                            yield textual.widgets.Label("Stop:", classes="toggle-label")
                            yield sm
                            yield textual.widgets.Label("", classes="toggle-gap")
                            cs = textual.widgets.Switch(value=False, id="highlight-case-sensitive")
                            cs.tooltip = "Case-sensitive pattern matching"
                            yield textual.widgets.Label("Case Sensitive:", classes="toggle-label")
                            yield cs
                            yield textual.widgets.Label("", classes="toggle-gap")
                            ml = textual.widgets.Switch(value=False, id="highlight-multiline")
                            ml.tooltip = "Match pattern across multiple lines"
                            yield textual.widgets.Label("Multiline:", classes="toggle-label")
                            yield ml
                        with textual.containers.Horizontal(classes="field-row"):
                            cap_sw = textual.widgets.Switch(value=False, id="highlight-captured")
                            cap_sw.tooltip = "Capture regex groups into variables"
                            yield textual.widgets.Label("Captured:", classes="toggle-label")
                            yield cap_sw
                            yield textual.widgets.Label("", classes="toggle-gap")
                            yield textual.widgets.Label("Capture Name:", classes="toggle-label")
                            yield textual.widgets.Input(
                                value="captures", placeholder="channel name", id="highlight-capture-name"
                            )
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Pattern", classes="form-label-short")
                            yield textual.widgets.Input(placeholder="regex pattern", id="highlight-pattern")
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Highlight", classes="form-label-short")
                            yield textual.widgets.Input(placeholder="eg. blink_black_on_yellow", id="highlight-style")
                        with textual.containers.Vertical(id="highlight-capture-fields"):
                            with textual.containers.Vertical(id="highlight-captures-container"):
                                pass
                            yield textual.widgets.Button("Add Capture", variant="default", id="highlight-add-capture")
                        with textual.containers.Horizontal(id="highlight-form-buttons", classes="edit-form-buttons"):
                            yield textual.widgets.Label(" ", classes="form-btn-spacer")
                            yield textual.widgets.Button("Cancel", variant="default", id="highlight-cancel-form")
                            yield textual.widgets.Button("OK", variant="success", id="highlight-ok")
                    yield textual.widgets.Static("", id="highlight-count")

    def on_mount(self) -> None:
        """Configure highlight table columns and load rules from file."""
        table = self.query_one("#highlight-table", textual.widgets.DataTable)
        table.cursor_type = "row"
        term_w = shutil.get_terminal_size(fallback=(80, 24)).columns
        pat_w = max(15, 15 + (term_w - 80))
        table.add_column("#", width=4, key="num")
        table.add_column("Pattern", width=pat_w, key="pattern")
        table.add_column("Highlight", width=24, key="highlight")
        table.add_column("Flags", width=6, key="flags")
        self.load_from_file()
        self.refresh_table()
        self.query_one("#highlight-form").display = False
        self.query_one("#highlight-capture-fields").display = False

    AUTOREPLY_PATTERN = "<Autoreply pattern>"

    def ensure_builtin(self) -> None:
        """Ensure the builtin autoreply highlight rule exists."""
        if not any(r.builtin for r in self.rules):
            self.rules.insert(
                0,
                HighlightTuple(
                    self.AUTOREPLY_PATTERN,
                    highlighter.DEFAULT_AUTOREPLY_HIGHLIGHT,
                    enabled=True,
                    stop_movement=False,
                    builtin=True,
                ),
            )

    def load_from_file(self) -> None:
        if not os.path.exists(self.path):
            self.ensure_builtin()
            return
        try:
            rules = highlighter.load_highlights(self.path, self.session_key)
            self.rules = [
                HighlightTuple(
                    r.pattern.pattern,
                    r.highlight,
                    r.enabled,
                    r.stop_movement,
                    r.builtin,
                    r.case_sensitive,
                    r.multiline,
                    r.captured,
                    r.capture_name,
                    tuple(r.captures),
                )
                for r in rules
            ]
        except (ValueError, FileNotFoundError):
            pass
        self.ensure_builtin()

    def matches_search(self, idx: int, query: str) -> bool:
        rule = self.rules[idx]
        q = query.lower()
        return q in rule.pattern.lower() or q in rule.highlight.lower()

    def refresh_table(self) -> None:
        table = self.query_one("#highlight-table", textual.widgets.DataTable)
        table.clear()
        q = self.search_query
        self.filtered_indices = []
        for i, rule in enumerate(self.rules):
            if q and not self.matches_search(i, q):
                continue
            self.filtered_indices.append(i)
            flags = " ".join(
                filter(
                    None,
                    [
                        "X" if not rule.enabled else "",
                        "S" if rule.stop_movement else "",
                        "CS" if rule.case_sensitive else "",
                        "M" if rule.multiline else "",
                        "C" if rule.captured else "",
                    ],
                )
            )
            row_pos = len(self.filtered_indices) - 1
            table.add_row(str(i + 1), rule.pattern, rule.highlight, flags.strip(), key=str(row_pos))
        self.update_count_label()

    def show_form(
        self,
        pattern_val: str = "",
        highlight_val: str = "",
        enabled: bool = True,
        stop_movement: bool = False,
        builtin: bool = False,
        case_sensitive: bool = False,
        multiline: bool = False,
        captured: bool = False,
        capture_name: str = "captures",
        captures: tuple[dict[str, str], ...] = (),
    ) -> None:
        pat_input = self.query_one("#highlight-pattern", textual.widgets.Input)
        pat_input.value = pattern_val
        pat_input.disabled = builtin
        self.query_one("#highlight-style", textual.widgets.Input).value = highlight_val
        self.query_one("#highlight-enabled", textual.widgets.Switch).value = enabled
        stop_sw = self.query_one("#highlight-stop-movement", textual.widgets.Switch)
        stop_sw.value = stop_movement
        stop_sw.disabled = builtin
        cs_sw = self.query_one("#highlight-case-sensitive", textual.widgets.Switch)
        cs_sw.value = case_sensitive
        cs_sw.disabled = builtin
        ml_sw = self.query_one("#highlight-multiline", textual.widgets.Switch)
        ml_sw.value = multiline
        ml_sw.disabled = builtin
        cap_sw = self.query_one("#highlight-captured", textual.widgets.Switch)
        cap_sw.value = captured
        cap_sw.disabled = builtin
        self.query_one("#highlight-capture-name", textual.widgets.Input).value = capture_name
        container = self.query_one("#highlight-captures-container", textual.containers.Vertical)
        container.remove_children()
        for cap in captures:
            self.add_capture_row(container, cap.get("key", "KeyName"), cap.get("value", r"\1"))
        cap_fields = self.query_one("#highlight-capture-fields", textual.containers.Vertical)
        cap_fields.display = captured
        self.query_one("#highlight-search", textual.widgets.Input).display = False
        self.query_one("#highlight-table").display = False
        self.query_one("#highlight-form").display = True
        self.set_action_buttons_disabled(True)
        if builtin:
            self.query_one("#highlight-style", textual.widgets.Input).focus()
        else:
            pat_input.focus()

    def add_capture_row(self, container: textual.containers.Vertical, key: str = "KeyName", value: str = r"\1") -> None:
        """Append a key/value capture row to the captures container."""
        row_id = f"cap-row-{id(container)}-{len(container.children)}"
        btn = textual.widgets.Button("X", variant="error", classes="capture-remove")
        btn.styles.width = 5
        row = textual.containers.Horizontal(
            textual.widgets.Input(value=key, placeholder="Key", classes="capture-key"),
            textual.widgets.Input(value=value, placeholder="Value", classes="capture-value"),
            btn,
            id=row_id,
            classes="capture-row",
        )
        container.mount(row)

    def do_captured_toggle(self, value: bool) -> None:
        """Show or hide capture fields when Captured switch changes."""
        cap_fields = self.query_one("#highlight-capture-fields", textual.containers.Vertical)
        cap_fields.display = value

    def submit_form(self) -> None:
        pattern_val = self.query_one("#highlight-pattern", textual.widgets.Input).value.strip()
        highlight_val = self.query_one("#highlight-style", textual.widgets.Input).value.strip()
        enabled = self.query_one("#highlight-enabled", textual.widgets.Switch).value
        stop_movement = self.query_one("#highlight-stop-movement", textual.widgets.Switch).value
        case_sensitive = self.query_one("#highlight-case-sensitive", textual.widgets.Switch).value
        multiline = self.query_one("#highlight-multiline", textual.widgets.Switch).value
        if pattern_val:
            try:
                re.compile(pattern_val)
            except re.error as exc:
                self.notify(f"Invalid regex: {exc}", severity="error")
                return
            for i, existing in enumerate(self.rules):
                if i != self.editing_idx and not existing.builtin and existing.pattern == pattern_val:
                    self.notify(f"Duplicate pattern: {pattern_val!r}", severity="error")
                    return
        if highlight_val:
            try:
                term = client_repl.get_term()
            except ImportError:
                term = None
            if term is not None and not highlighter.validate_highlight(term, highlight_val):
                self.notify(f"Invalid highlight style: {highlight_val!r}", severity="error")
                return
        captured = self.query_one("#highlight-captured", textual.widgets.Switch).value
        capture_name = self.query_one("#highlight-capture-name", textual.widgets.Input).value.strip()
        if not capture_name:
            capture_name = "captures"
        captures_list: list[dict[str, str]] = []
        container = self.query_one("#highlight-captures-container", textual.containers.Vertical)
        for row in container.children:
            inputs = list(row.query(textual.widgets.Input))
            if len(inputs) >= 2:
                k = inputs[0].value.strip()
                v = inputs[1].value.strip()
                if k and v:
                    captures_list.append({"key": k, "value": v})
        builtin = False
        if self.editing_idx is not None:
            builtin = self.rules[self.editing_idx].builtin
        entry = HighlightTuple(
            pattern_val,
            highlight_val,
            enabled,
            stop_movement,
            builtin,
            case_sensitive,
            multiline,
            captured,
            capture_name,
            tuple(captures_list),
        )
        self.finalize_edit(entry, bool(pattern_val and highlight_val))

    def on_switch_changed(self, event: textual.widgets.Switch.Changed) -> None:
        """Toggle capture fields visibility when Captured switch changes."""
        if event.switch.id == "highlight-captured":
            self.do_captured_toggle(event.value)

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle button presses for add, edit, delete, save, and cancel."""
        btn = event.button.id or ""
        if btn == "highlight-delete":
            idx = self.selected_idx()
            if idx is not None and idx < len(self.rules) and self.rules[idx].builtin:
                self.notify("Cannot delete the builtin autoreply highlight rule.")
                return
        if btn == "highlight-add-capture":
            container = self.query_one("#highlight-captures-container", textual.containers.Vertical)
            n = len(container.children)
            self.add_capture_row(container, "KeyName", f"\\{n + 1}")
            return
        if "capture-remove" in (event.button.classes or set()):
            row = event.button.parent
            if row is not None:
                row.remove()
            return
        super().on_button_pressed(event)

    def save_to_file(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        rules = []
        for t in self.rules:
            flags = re.MULTILINE | re.DOTALL
            if not t.case_sensitive:
                flags |= re.IGNORECASE
            rules.append(
                highlighter.HighlightRule(
                    pattern=re.compile(t.pattern, flags),
                    highlight=t.highlight,
                    enabled=t.enabled,
                    stop_movement=t.stop_movement,
                    builtin=t.builtin,
                    case_sensitive=t.case_sensitive,
                    multiline=t.multiline,
                    captured=t.captured,
                    capture_name=t.capture_name,
                    captures=list(t.captures),
                )
            )
        highlighter.save_highlights(self.path, rules, self.session_key)


class HighlightEditScreen(client_tui_base.EditListScreen):
    """Thin screen wrapper for the highlight editor."""

    def __init__(self, path: str, session_key: str = "") -> None:
        super().__init__()
        self.pane = HighlightEditPane(path=path, session_key=session_key)


# ---------------------------------------------------------------------------
# Progress bar editor
# ---------------------------------------------------------------------------


class ProgressBarTuple(NamedTuple):
    """Lightweight tuple for progress bar configs in the TUI editor."""

    name: str = ""
    gmcp_package: str = ""
    value_field: str = ""
    max_field: str = ""
    enabled: bool = True
    color_mode: str = "theme"
    color_name_max: str = "success"
    color_name_min: str = "error"
    color_path: str = "shortest"
    text_color_fill: str = "auto"
    text_color_empty: str = "auto"
    display_order: int = 0
    side: str = "left"


class ProgressBarEditPane(client_tui_base.EditListPane):
    """Pane widget for GMCP progress bar configuration."""

    growable_keys: list[str] = ["name"]

    DEFAULT_CSS = (
        client_tui_base.EditListPane.DEFAULT_CSS
        + """
    #progressbar-form { padding: 0 0 0 4; }
    #progressbar-form .form-label { width: 12; }
    #progressbar-form Input { max-width: 50; }
    #progressbar-form Select { max-width: 50; }
    #pb-gmcp-select { max-width: 18; }
    #pb-value-select { max-width: 18; }
    #pb-max-select { max-width: 18; }
    #pb-color-swatch-max { width: 4; height: 1; padding-top: 1; }
    #pb-color-swatch-min { width: 4; height: 1; padding-top: 1; }
    #pb-color-row1 { height: auto; margin: 0; }
    #pb-color-row2 { height: auto; margin: 0; }
    #pb-color-row3 { height: auto; margin: 0; }
    #pb-color-mode { max-width: 20; }
    #pb-color-max { width: 28; }
    #pb-color-min { width: 28; }
    #pb-text-min { width: 28; }
    #pb-text-max { width: 28; }
    #pb-color-path { max-width: 20; }
    #pb-side { max-width: 20; }
    #pb-preview-bar { height: 1; margin: 0 0 0 12; }
    #pb-preview-gradient { height: 1; margin: 0 0 0 12; }
    """
    )

    def __init__(self, path: str, session_key: str = "", gmcp_snapshot_path: str = "") -> None:
        """Initialize progress bar editor."""
        super().__init__()
        self.path = path
        self.session_key = session_key
        self.gmcp_snapshot_path = gmcp_snapshot_path
        self.bars: list[ProgressBarTuple] = []
        self.gmcp_packages: list[str] = []
        self.gmcp_fields: dict[str, list[str]] = {}
        self.preview_timer: Any = None
        self.preview_phase: float = 0.0
        self.populating_form: bool = False
        self.form_source_pkg: str = ""
        self.form_color_mode: str = ""

    @property
    def prefix(self) -> str:
        return "progressbar"

    @property
    def noun(self) -> str:
        return "Progress Bar"

    @property
    def noun_plural(self) -> str:
        return "Progress Bars"

    @property
    def items(self) -> list[Any]:
        return self.bars

    def item_label(self, idx: int) -> str:
        if idx < len(self.bars):
            return self.bars[idx].name
        return ""

    def load_gmcp_packages(self) -> None:
        """Load GMCP package names and field names from the snapshot file."""
        if not self.gmcp_snapshot_path:
            return
        if not os.path.exists(self.gmcp_snapshot_path):
            return
        with open(self.gmcp_snapshot_path, encoding="utf-8") as fh:
            data = json.load(fh)
        packages = data.get("packages", {}) if isinstance(data, dict) else {}
        if packages:
            self.gmcp_packages = sorted(packages.keys())
            for pkg_name, pkg_info in packages.items():
                pkg_data = pkg_info.get("data", {}) if isinstance(pkg_info, dict) else {}
                if isinstance(pkg_data, dict):
                    self.gmcp_fields[pkg_name] = sorted(pkg_data.keys())

    def compose(self) -> textual.app.ComposeResult:
        """Build the progress bar editor widget tree."""
        pfx = "progressbar"
        with textual.containers.Vertical(id=f"{pfx}-panel", classes="edit-panel"):
            with textual.containers.Horizontal(id=f"{pfx}-body", classes="edit-body"):
                with textual.containers.Vertical(id=f"{pfx}-button-col", classes="edit-button-col"):
                    yield textual.widgets.Button("Add", variant="success", id=f"{pfx}-add")
                    yield textual.widgets.Button("Edit", variant="warning", id=f"{pfx}-edit")
                    yield textual.widgets.Button("Copy", id=f"{pfx}-copy", classes="edit-copy")
                    yield textual.widgets.Button("Detect", variant="primary", id=f"{pfx}-detect")
                    yield textual.widgets.Button("Help", variant="success", id=f"{pfx}-help")
                    yield textual.widgets.Button("Save", variant="primary", id=f"{pfx}-save")
                    yield textual.widgets.Button("Cancel", id=f"{pfx}-close")
                with textual.containers.Vertical(id=f"{pfx}-right", classes="edit-right"):
                    yield textual.widgets.Input(
                        placeholder="Search bars\u2026", id=f"{pfx}-search", classes="edit-search"
                    )
                    yield textual.widgets.DataTable(id=f"{pfx}-table", classes="edit-table")
                    with textual.containers.VerticalScroll(id=f"{pfx}-form", classes="edit-form"):
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Name", classes="form-label")
                            yield textual.widgets.Input(placeholder="bar name", id="pb-name")
                            yield textual.widgets.Label("", classes="toggle-gap")
                            yield textual.widgets.Label("Enabled:", classes="toggle-label")
                            yield textual.widgets.Switch(value=True, id="pb-enabled")
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Source", classes="form-label")
                            yield textual.widgets.Select[str](
                                [], id="pb-gmcp-select", allow_blank=True, prompt="textual.widgets.Select source\u2026"
                            )
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Val/Max", classes="form-label")
                            yield textual.widgets.Select[str](
                                [], id="pb-value-select", allow_blank=True, prompt="Value field\u2026"
                            )
                            yield textual.widgets.Label("", classes="form-gap")
                            yield textual.widgets.Select[str](
                                [], id="pb-max-select", allow_blank=True, prompt="Max field\u2026"
                            )
                        with textual.containers.Horizontal(id="pb-color-row1", classes="field-row"):
                            yield textual.widgets.Label("Color Mode", classes="form-label")
                            with textual.widgets.RadioSet(id="pb-color-mode"):
                                yield textual.widgets.RadioButton("Theme", id="pb-mode-theme", value=True)
                                yield textual.widgets.RadioButton("Custom", id="pb-mode-custom")
                            yield textual.widgets.Label("", id="pb-color-gap1", classes="form-gap")
                            yield textual.widgets.Label("Path", classes="form-label")
                            with textual.widgets.RadioSet(id="pb-color-path"):
                                yield textual.widgets.RadioButton("Shortest", id="pb-path-shortest", value=True)
                                yield textual.widgets.RadioButton("Longest", id="pb-path-longest")
                        with textual.containers.Horizontal(classes="field-row"):
                            yield textual.widgets.Label("Side", classes="form-label")
                            with textual.widgets.RadioSet(id="pb-side"):
                                yield textual.widgets.RadioButton("Left", id="pb-side-left", value=True)
                                yield textual.widgets.RadioButton("Right", id="pb-side-right")
                        with textual.containers.Horizontal(id="pb-color-row2", classes="field-row"):
                            yield textual.widgets.Label("Min:", classes="form-label")
                            yield textual.widgets.Select[str](
                                self.theme_color_options(), id="pb-color-min", value="error", allow_blank=False
                            )
                            yield textual.widgets.Static("", id="pb-color-swatch-min")
                            yield textual.widgets.Select[str](
                                self.text_color_options(False), id="pb-text-min", value="auto", allow_blank=False
                            )
                        with textual.containers.Horizontal(id="pb-color-row3", classes="field-row"):
                            yield textual.widgets.Label("Max:", classes="form-label")
                            yield textual.widgets.Select[str](
                                self.theme_color_options(), id="pb-color-max", value="success", allow_blank=False
                            )
                            yield textual.widgets.Static("", id="pb-color-swatch-max")
                            yield textual.widgets.Select[str](
                                self.text_color_options(False), id="pb-text-max", value="auto", allow_blank=False
                            )
                        yield textual.widgets.Static("", id="pb-preview-bar")
                        yield textual.widgets.Static("", id="pb-preview-gradient")
                        with textual.containers.Horizontal(id=f"{pfx}-form-buttons", classes="edit-form-buttons"):
                            yield textual.widgets.Label(" ", classes="form-btn-spacer")
                            yield textual.widgets.Button("Cancel", variant="default", id=f"{pfx}-cancel-form")
                            yield textual.widgets.Button("OK", variant="success", id=f"{pfx}-ok")
                    yield textual.widgets.Static("", id=f"{pfx}-count")

    @staticmethod
    def color_options() -> list[tuple[RichText, str]]:
        """Build textual.widgets.Select options from all Rich named colors with swatches."""
        options: list[tuple[rich.text.Text, str]] = []
        for c in progressbars.CURATED_COLORS:
            label = rich.text.Text()
            label.append("\u2588 ", style=c)
            label.append(c)
            options.append((label, c))
        return options

    @staticmethod
    def theme_color_options() -> list[tuple[RichText, str]]:
        """Build textual.widgets.Select options from Textual theme colors with swatches."""
        options: list[tuple[rich.text.Text, str]] = []
        for name, hex_val in progressbars.get_theme_colors().items():
            label = rich.text.Text()
            label.append("\u2588 ", style=hex_val[:7])
            label.append(name)
            options.append((label, name))
        return options

    @staticmethod
    def text_color_options(is_custom: bool) -> list[tuple[RichText, str]]:
        """Build textual.widgets.Select options for text color: ``auto`` plus bar color options."""
        auto_label = rich.text.Text()
        auto_label.append("auto")
        options: list[tuple[rich.text.Text, str]] = [(auto_label, "auto")]
        if is_custom:
            options.extend(ProgressBarEditPane.color_options())
        else:
            options.extend(ProgressBarEditPane.theme_color_options())
        return options

    def on_mount(self) -> None:
        """Configure table columns and load bars from file."""
        table = self.query_one("#progressbar-table", textual.widgets.DataTable)
        table.cursor_type = "row"
        term_w = shutil.get_terminal_size(fallback=(80, 24)).columns
        name_w = min(20, max(12, 12 + (term_w - 80)))
        table.add_column("#", width=4, key="num")
        table.add_column("Name", width=name_w, key="name")
        table.add_column("Source", width=20, key="source")
        table.add_column("Value", width=14, key="value")
        table.add_column("Max", width=14, key="max")
        table.add_column("Enabled", width=8, key="enabled")
        table.add_column("Color", width=10, key="color")
        self.load_gmcp_packages()
        self.load_from_file()
        self.do_detect(silent=True)
        self.refresh_table()
        self.query_one("#progressbar-form").display = False

    def load_from_file(self) -> None:
        if not os.path.exists(self.path):
            return
        bars = progressbars.load_progressbars(self.path, self.session_key)
        self.bars = [ProgressBarTuple(*b) for b in bars]

    def matches_search(self, idx: int, query: str) -> bool:
        bar = self.bars[idx]
        q = query.lower()
        return q in bar.name.lower() or q in bar.gmcp_package.lower()

    SWATCH_STEPS = 10

    @staticmethod
    def color_swatch(bar: ProgressBarTuple) -> RichText:
        """Build a solid-block gradient swatch for the Color column."""
        cfg = progressbars.BarConfig(
            bar.name,
            bar.gmcp_package,
            bar.value_field,
            bar.max_field,
            color_mode=bar.color_mode,
            color_name_max=bar.color_name_max,
            color_name_min=bar.color_name_min,
            color_path=bar.color_path,
        )
        steps = ProgressBarEditPane.SWATCH_STEPS
        swatch = rich.text.Text()
        for i in range(steps):
            f = i / max(1, steps - 1)
            c = progressbars.bar_color_at(f, cfg)
            swatch.append(" ", style=f"on {c}")
        return swatch

    def refresh_table(self) -> None:
        table = self.query_one("#progressbar-table", textual.widgets.DataTable)
        table.clear()
        q = self.search_query
        self.filtered_indices = []
        for i, bar in enumerate(self.bars):
            if q and not self.matches_search(i, q):
                continue
            self.filtered_indices.append(i)
            enabled = "Yes" if bar.enabled else "No"
            display_name = bar.name if len(bar.name) <= 19 else bar.name[:18] + "\u2026"
            val_f = bar.value_field if len(bar.value_field) <= 13 else bar.value_field[:12] + "\u2026"
            max_f = bar.max_field if len(bar.max_field) <= 13 else bar.max_field[:12] + "\u2026"
            swatch = self.color_swatch(bar)
            row_pos = len(self.filtered_indices) - 1
            table.add_row(str(i + 1), display_name, bar.gmcp_package, val_f, max_f, enabled, swatch, key=str(row_pos))
        self.update_count_label()

    def show_form(
        self,
        name: str = "",
        gmcp_package: str = "",
        value_field: str = "",
        max_field: str = "",
        enabled: bool = True,
        color_mode: str = "theme",
        color_name_max: str = "green",
        color_name_min: str = "red",
        color_path: str = "shortest",
        text_color_fill: str = "auto",
        text_color_empty: str = "auto",
        display_order: int = 0,
        side: str = "left",
    ) -> None:
        is_travel = name == progressbars.TRAVEL_BAR_NAME or (name and not gmcp_package)
        self.populating_form = True
        self.query_one("#pb-name", textual.widgets.Input).value = name
        self.query_one("#pb-enabled", textual.widgets.Switch).value = enabled
        # Populate source dropdown
        pkg_options = [(p, p) for p in self.gmcp_packages]
        src_sel = self.query_one("#pb-gmcp-select", textual.widgets.Select)
        src_sel.set_options(pkg_options)
        if gmcp_package and gmcp_package in self.gmcp_packages:
            src_sel.value = gmcp_package
        elif gmcp_package:
            src_sel.set_options(pkg_options + [(gmcp_package, gmcp_package)])
            src_sel.value = gmcp_package
        # Disable source fields for Travel bar
        src_sel.disabled = is_travel
        val_sel = self.query_one("#pb-value-select", textual.widgets.Select)
        max_sel = self.query_one("#pb-max-select", textual.widgets.Select)
        val_sel.disabled = is_travel
        max_sel.disabled = is_travel
        # Populate value/max field dropdowns
        self.form_source_pkg = gmcp_package
        self.populate_field_selects(gmcp_package, value_field, max_field)
        self.populating_form = False
        # Color mode -- set the desired button to on; textual.widgets.RadioSet will turn the
        # other off via its internal on_radio_button_changed handler.
        is_custom = color_mode == "custom"
        self.form_color_mode = color_mode
        target = "#pb-mode-custom" if is_custom else "#pb-mode-theme"
        self.query_one(target, textual.widgets.RadioButton).value = True
        # Color selects -- populate with theme or custom options
        self.swap_color_options(
            is_custom,
            preserve_max=color_name_max,
            preserve_min=color_name_min,
            preserve_text_fill=text_color_fill,
            preserve_text_empty=text_color_empty,
        )
        # Values already set by swap_color_options with proper fallbacks.
        # Color path
        is_longest = color_path == "longest"
        path_target = "#pb-path-longest" if is_longest else "#pb-path-shortest"
        self.query_one(path_target, textual.widgets.RadioButton).value = True
        # Side
        side_target = "#pb-side-right" if side == "right" else "#pb-side-left"
        self.query_one(side_target, textual.widgets.RadioButton).value = True
        # Show form + start preview animation
        self.query_one("#progressbar-search", textual.widgets.Input).display = False
        self.query_one("#progressbar-table").display = False
        self.query_one("#progressbar-form").display = True
        self.set_action_buttons_disabled(True)
        self.preview_phase = 0.0
        self.start_preview_timer()
        self.query_one("#pb-name", textual.widgets.Input).focus()

    def submit_form(self) -> None:
        name = self.query_one("#pb-name", textual.widgets.Input).value.strip()
        if not name:
            self.notify("Name is required.", severity="error")
            return
        enabled = self.query_one("#pb-enabled", textual.widgets.Switch).value
        gmcp_pkg = self.get_select_value("#pb-gmcp-select", "")
        value_field = self.get_select_value("#pb-value-select", "")
        max_field = self.get_select_value("#pb-max-select", "")
        color_mode = self.get_color_mode()
        color_name_max = self.get_select_value("#pb-color-max", "green")
        color_name_min = self.get_select_value("#pb-color-min", "red")
        color_path = "longest" if self.query_one("#pb-path-longest", textual.widgets.RadioButton).value else "shortest"
        text_color_fill = self.get_select_value("#pb-text-min", "auto")
        text_color_empty = self.get_select_value("#pb-text-max", "auto")
        side = "right" if self.query_one("#pb-side-right", textual.widgets.RadioButton).value else "left"
        order = 0
        if self.editing_idx is not None:
            order = self.bars[self.editing_idx].display_order
        entry = ProgressBarTuple(
            name=name,
            gmcp_package=gmcp_pkg,
            value_field=value_field,
            max_field=max_field,
            enabled=enabled,
            color_mode=color_mode,
            color_name_max=color_name_max,
            color_name_min=color_name_min,
            color_path=color_path,
            text_color_fill=text_color_fill,
            text_color_empty=text_color_empty,
            display_order=order,
            side=side,
        )
        self.finalize_edit(entry, True)

    def get_color_mode(self) -> str:
        """Return the selected color mode string."""
        if self.query_one("#pb-mode-custom", textual.widgets.RadioButton).value:
            return "custom"
        return "theme"

    def get_select_value(self, sel_id: str, default: str) -> str:
        """Return a textual.widgets.Select widget's value as str."""
        sel = self.query_one(sel_id, textual.widgets.Select)
        val = sel.value
        if isinstance(val, str):
            return val
        return default

    def update_swatch(self, widget_id: str, color_name: str) -> None:
        """Update a color swatch textual.widgets.Static with a colored block."""
        try:
            swatch = self.query_one(widget_id, textual.widgets.Static)
            swatch.update(f"[on {color_name}]    [/]")
        except textual.css.query.NoMatches:
            pass

    def hide_form(self) -> None:
        self.stop_preview_timer()
        self.form_source_pkg = ""
        self.form_color_mode = ""
        super().hide_form()

    def start_preview_timer(self) -> None:
        """Start the animated preview at ~15 fps."""
        self.stop_preview_timer()
        self.preview_phase = 0.0
        self.preview_timer = self.set_interval(1.0 / 15.0, self.tick_preview)

    def stop_preview_timer(self) -> None:
        """Stop the animated preview."""
        if self.preview_timer is not None:
            self.preview_timer.stop()
            self.preview_timer = None

    def tick_preview(self) -> None:
        """Advance the preview animation phase and redraw."""
        self.preview_phase += (1.0 / 15.0) / 13.5
        if self.preview_phase >= 1.0:
            self.preview_phase -= 1.0
        self.update_preview()

    @staticmethod
    def ease_in_out(t: float) -> float:
        """Smooth ease-in-out: very slow at ends, faster in middle."""
        # Quintic smootherstep for pronounced dwell at 0% and 100%
        return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)

    def update_preview(self) -> None:
        """Render animated preview bars from current form settings."""
        # Triangular wave 0->1->0 with ease-in-out
        t = self.preview_phase * 2.0
        if t > 1.0:
            t = 2.0 - t
        fraction = self.ease_in_out(t)

        try:
            w = self.query_one("#pb-preview-bar", textual.widgets.Static).size.width
            bar_w = w - 2 if w > 12 else 24
        except textual.css.query.NoMatches:
            bar_w = 24
        cur = int(fraction * 1000)
        mx = 1000
        name = self.query_one("#pb-name", textual.widgets.Input).value.strip() or "bar"

        # Build BarConfig from form
        color_mode = self.get_color_mode()
        max_c = self.get_select_value("#pb-color-max", "success")
        min_c = self.get_select_value("#pb-color-min", "error")
        path = "longest" if self.query_one("#pb-path-longest", textual.widgets.RadioButton).value else "shortest"
        cfg = progressbars.BarConfig(
            name, "", "", "", color_mode=color_mode, color_name_max=max_c, color_name_min=min_c, color_path=path
        )

        text_fill_val = self.get_select_value("#pb-text-min", "auto")
        text_empty_val = self.get_select_value("#pb-text-max", "auto")
        text_fill = progressbars.resolve_text_color_hex(text_fill_val)
        text_empty = progressbars.resolve_text_color_hex(text_empty_val)

        # 1) Toolbar-style bar via vital_bar (SGR -> ANSI -> Rich Text)
        kind = name.lower()
        color = progressbars.bar_color_at(fraction, cfg)
        fragments = client_repl_render.vital_bar(
            cur, mx, bar_w, kind, color_override=color, text_fill_color=text_fill, text_empty_color=text_empty
        )
        ansi_str = "".join(sgr + text for sgr, text in fragments) + "\x1b[0m"
        try:
            bar_text = rich.text.Text.from_ansi(ansi_str)
            self.query_one("#pb-preview-bar", textual.widgets.Static).update(bar_text)
        except textual.css.query.NoMatches:
            pass

        # 2) textual.widgets.Static color gradient showing the full min->max spectrum
        steps = bar_w + 2
        gradient_parts: list[str] = []
        for i in range(steps):
            f = i / max(1, steps - 1)
            c = progressbars.bar_color_at(f, cfg)
            gradient_parts.append(f"[on {c}] [/]")
        try:
            self.query_one("#pb-preview-gradient", textual.widgets.Static).update("".join(gradient_parts))
        except textual.css.query.NoMatches:
            pass

    def swap_color_options(
        self,
        is_custom: bool,
        preserve_max: str = "",
        preserve_min: str = "",
        preserve_text_fill: str = "auto",
        preserve_text_empty: str = "auto",
    ) -> None:
        """Swap color dropdown options for theme vs custom mode."""
        options = self.color_options() if is_custom else self.theme_color_options()
        max_sel = self.query_one("#pb-color-max", textual.widgets.Select)
        min_sel = self.query_one("#pb-color-min", textual.widgets.Select)
        max_sel.set_options(options)
        min_sel.set_options(options)
        if preserve_max:
            vals = {v for _, v in options}
            if preserve_max in vals:
                max_sel.value = preserve_max
            else:
                max_sel.value = "success" if not is_custom else "green"
        if preserve_min:
            vals = {v for _, v in options}
            if preserve_min in vals:
                min_sel.value = preserve_min
            else:
                min_sel.value = "error" if not is_custom else "red"
        text_options = self.text_color_options(is_custom)
        text_min_sel = self.query_one("#pb-text-min", textual.widgets.Select)
        text_max_sel = self.query_one("#pb-text-max", textual.widgets.Select)
        text_min_sel.set_options(text_options)
        text_max_sel.set_options(text_options)
        text_vals = {v for _, v in text_options}
        text_min_sel.value = preserve_text_fill if preserve_text_fill in text_vals else "auto"
        text_max_sel.value = preserve_text_empty if preserve_text_empty in text_vals else "auto"

    def on_radio_set_changed(self, event: textual.widgets.RadioSet.Changed) -> None:
        """Toggle custom color fields and swap color options when mode changes."""
        if event.radio_set.id == "pb-color-mode":
            is_custom = self.query_one("#pb-mode-custom", textual.widgets.RadioButton).value
            new_mode = "custom" if is_custom else "theme"
            if new_mode == self.form_color_mode:
                return
            self.form_color_mode = new_mode
            self.swap_color_options(
                is_custom,
                preserve_max=self.get_select_value("#pb-color-max", ""),
                preserve_min=self.get_select_value("#pb-color-min", ""),
                preserve_text_fill=self.get_select_value("#pb-text-min", "auto"),
                preserve_text_empty=self.get_select_value("#pb-text-max", "auto"),
            )

    def populate_field_selects(self, pkg_name: str, value_field: str = "", max_field: str = "") -> None:
        """Populate value/max field textual.widgets.Select widgets from GMCP snapshot fields."""
        fields = self.gmcp_fields.get(pkg_name, [])
        field_opts = [(f, f) for f in fields]
        val_sel = self.query_one("#pb-value-select", textual.widgets.Select)
        max_sel = self.query_one("#pb-max-select", textual.widgets.Select)
        # Add current values if not already in the list
        val_opts = list(field_opts)
        max_opts = list(field_opts)
        if value_field and value_field not in fields:
            val_opts.append((value_field, value_field))
        if max_field and max_field not in fields:
            max_opts.append((max_field, max_field))
        val_sel.set_options(val_opts)
        max_sel.set_options(max_opts)
        if value_field:
            val_sel.value = value_field
        if max_field:
            max_sel.value = max_field

    def on_select_changed(self, event: textual.widgets.Select.Changed) -> None:
        """Update swatch when a color changes, update fields when source changes."""
        sel_id = event.select.id
        if sel_id == "pb-color-max":
            val = event.value
            if isinstance(val, str):
                self.update_swatch("#pb-color-swatch-max", val)
        elif sel_id == "pb-color-min":
            val = event.value
            if isinstance(val, str):
                self.update_swatch("#pb-color-swatch-min", val)
        elif sel_id == "pb-gmcp-select":
            if self.populating_form:
                return
            val = event.value
            if isinstance(val, str) and val != self.form_source_pkg:
                self.form_source_pkg = val
                self.populate_field_selects(val)

    def on_input_changed(self, event: textual.widgets.Input.Changed) -> None:
        """Handle search input changes."""
        if event.input.id == f"{self.prefix}-search":
            self.search_query = event.value
            self.refresh_table()

    def do_extra_button(self, suffix: str, btn: str) -> None:
        """Handle the Detect button."""
        if suffix == "detect":
            self.do_detect()

    def do_detect(self, silent: bool = False) -> None:
        """Auto-detect progress bars from GMCP snapshot."""
        if not self.gmcp_snapshot_path:
            if not silent:
                self.notify("No GMCP snapshot available.", severity="warning")
            return
        if not os.path.exists(self.gmcp_snapshot_path):
            if not silent:
                self.notify("GMCP snapshot file not found.", severity="warning")
            return
        with open(self.gmcp_snapshot_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        packages = raw.get("packages", {}) if isinstance(raw, dict) else {}
        gmcp_data = {pkg: info.get("data", info) for pkg, info in packages.items() if isinstance(info, dict)}
        detected = progressbars.detect_progressbars(gmcp_data)
        existing = {(b.gmcp_package, b.value_field, b.max_field) for b in self.bars}
        existing_names = {b.name for b in self.bars}
        added = 0
        for bar in detected:
            if not bar.gmcp_package and bar.name in existing_names:
                continue
            key = (bar.gmcp_package, bar.value_field, bar.max_field)
            if key not in existing:
                self.bars.append(ProgressBarTuple(*bar))
                existing.add(key)
                added += 1
        if added:
            self.refresh_table()
            if not silent:
                self.notify(f"Detected {added} new bar(s).")
        elif not silent:
            self.notify("No new bars detected.")

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Route button presses to base class."""
        super().on_button_pressed(event)

    def save_to_file(self) -> None:
        bars = [progressbars.BarConfig(*t) for t in self.bars]
        progressbars.save_progressbars(self.path, self.session_key, bars)


class ProgressBarEditScreen(client_tui_base.EditListScreen):
    """Thin screen wrapper for the progress bar editor."""

    def __init__(self, path: str, session_key: str = "", gmcp_snapshot_path: str = "") -> None:
        super().__init__()
        self.pane = ProgressBarEditPane(path=path, session_key=session_key, gmcp_snapshot_path=gmcp_snapshot_path)


# ---------------------------------------------------------------------------
# Standalone entry points
# ---------------------------------------------------------------------------


def edit_macros_main(
    path: str, session_key: str = "", rooms_file: str = "", current_room_file: str = "", logfile: str = ""
) -> None:
    """Launch standalone macro editor TUI."""
    client_tui_base.launch_editor(
        MacroEditScreen(path=path, session_key=session_key, rooms_file=rooms_file, current_room_file=current_room_file),
        session_key=session_key,
        logfile=logfile,
    )


def edit_autoreplies_main(path: str, session_key: str = "", select_pattern: str = "", logfile: str = "") -> None:
    """Launch standalone autoreply editor TUI."""
    client_tui_base.launch_editor(
        AutoreplyEditScreen(path=path, session_key=session_key, select_pattern=select_pattern),
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


# ---------------------------------------------------------------------------
# Theme editor
# ---------------------------------------------------------------------------

SWATCH_KEYS = ("primary", "secondary", "accent", "success", "error", "warning", "surface", "background", "foreground")


class ThemeEditPane(textual.containers.Vertical):
    """Pane widget for selecting Textual built-in themes."""

    DEFAULT_CSS = """
    ThemeEditPane {
        width: 100%; height: 100%;
    }
    #theme-panel {
        width: 100%; height: 100%;
        border: round $surface-lighten-2; background: $surface; padding: 1 1;
    }
    #theme-table { height: 1fr; }
    #theme-preview { height: 3; margin-top: 1; }
    """

    def __init__(self, session_key: str = "", **kwargs: Any) -> None:
        """Initialize theme editor."""
        super().__init__(**kwargs)
        self.session_key = session_key

    def compose(self) -> textual.app.ComposeResult:
        """Build the theme selection layout."""
        with textual.containers.Vertical(id="theme-panel"):
            yield textual.widgets.Label("textual.widgets.Select a theme:")
            yield textual.widgets.DataTable(id="theme-table")
            yield textual.widgets.Static("", id="theme-preview")

    def on_mount(self) -> None:
        """Populate the theme list on mount."""
        table = self.query_one("#theme-table", textual.widgets.DataTable)
        table.cursor_type = "row"
        table.add_columns("Theme", "Preview")

        current = self.app.theme or ""
        for name in sorted(textual.theme.BUILTIN_THEMES):
            marker = "\u2713" if name == current else ""
            theme = textual.theme.BUILTIN_THEMES[name]
            cs = theme.to_color_system()
            colors = cs.generate()
            swatches = " ".join(f"[{colors.get(k, '#888')}]\u2588\u2588[/]" for k in SWATCH_KEYS if k in colors)
            table.add_row(f"{marker} {name}" if marker else f"  {name}", swatches, key=name)

        if current and current in textual.theme.BUILTIN_THEMES:
            idx = sorted(textual.theme.BUILTIN_THEMES).index(current)
            table.move_cursor(row=idx)

        self.update_preview()

    def on_data_table_row_selected(self, event: textual.widgets.DataTable.RowSelected) -> None:
        """Apply the selected theme."""
        name = str(event.row_key.value) if event.row_key.value else ""
        if name:
            self.refresh_markers(name)
            self.app.theme = name
            self.update_preview()

    def refresh_markers(self, active: str) -> None:
        """Update the check-mark column to reflect the active theme."""
        table = self.query_one("#theme-table", textual.widgets.DataTable)
        for name in sorted(textual.theme.BUILTIN_THEMES):
            marker = "\u2713" if name == active else ""
            label = f"{marker} {name}" if marker else f"  {name}"
            try:
                table.update_cell(name, "Theme", label)
            except textual.widgets.data_table.CellDoesNotExist:
                pass

    def update_preview(self) -> None:
        """Show a color swatch preview of the current theme."""
        current = self.app.theme or ""
        theme = textual.theme.BUILTIN_THEMES.get(current)
        if theme is None:
            return
        cs = theme.to_color_system()
        colors = cs.generate()
        lines = []
        for k in SWATCH_KEYS:
            val = colors.get(k, "")
            if val:
                lines.append(f"[{val}]\u2588\u2588[/] {k}: {val}")
        preview = self.query_one("#theme-preview", textual.widgets.Static)
        preview.update(" | ".join(lines))
