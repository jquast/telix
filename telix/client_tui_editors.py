"""
Concrete editor panes for the Textual TUI.

Contains MacroEditPane, AutoreplyEditPane, HighlightEditPane,
ProgressBarEditPane, their screen wrappers, and standalone entry points.
Imports from ``client_tui_base`` only (no circular deps).
"""

from __future__ import annotations

# std imports
import os
import json
import shutil
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

if TYPE_CHECKING:
    from rich.text import Text as RichText

# 3rd party
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import (
    Input,
    Label,
    Button,
    Select,
    Static,
    Switch,
    RadioSet,
    DataTable,
    RadioButton,
)
from textual.css.query import NoMatches
from textual.containers import Vertical, Horizontal, VerticalScroll

# local
from .client_tui_base import (
    EditListPane,
    EditListScreen,
    CommandHelpScreen,
    launch_editor,
    relative_time,
)


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


class MacroEditPane(EditListPane):
    """Pane widget for macro key binding editing."""

    growable_keys: list[str] = ["text", "toggle-text"]

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_or_close", "Cancel", priority=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("plus", "reorder_hint", "Change Priority", key_display="+/=/-", show=True),
        Binding("enter", "save_hint", "Save", show=True),
        Binding("l", "sort_last", "Recent", show=True),
    ]

    DEFAULT_CSS = EditListPane.DEFAULT_CSS + """
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

    def __init__(
        self, path: str, session_key: str = "", rooms_file: str = "", current_room_file: str = ""
    ) -> None:
        """Initialize macro editor with file path and session key."""
        super().__init__()
        self.path = path
        self.session_key = session_key
        self.rooms_path = rooms_file
        self.current_room_path = current_room_file
        self.macros: list[tuple[str, str, bool, str, bool, str]] = []
        self.capturing: bool = False
        self.sort_mode: str = ""
        self.capture_escape_pending: bool = False
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

    def compose(self) -> ComposeResult:
        """Build the macro editor layout."""
        with Vertical(id="macro-panel", classes="edit-panel"):
            with Horizontal(id="macro-body", classes="edit-body"):
                with Vertical(id="macro-button-col", classes="edit-button-col"):
                    yield Button("Add", variant="success", id="macro-add")
                    yield Button("Edit", variant="warning", id="macro-edit")
                    yield Button("Copy", id="macro-copy", classes="edit-copy")
                    yield Button("Delete", variant="error", id="macro-delete")
                    yield Button("Help", variant="success", id="macro-help")
                    yield Button("Save", variant="primary", id="macro-save")
                    yield Button("Cancel", id="macro-close")
                with Vertical(id="macro-right", classes="edit-right"):
                    yield Input(
                        placeholder="Search macros\u2026", id="macro-search", classes="edit-search"
                    )
                    yield DataTable(id="macro-table", classes="edit-table")
                    with Vertical(id="macro-form", classes="edit-form"):
                        with Horizontal(classes="field-row"):
                            yield Label("Enabled:", classes="toggle-label")
                            yield Switch(value=True, id="macro-enabled")
                            yield Label("", classes="form-gap")
                            yield Label("Key", classes="form-label-mid")
                            yield Button("Capture", id="macro-capture")
                            yield Static("(none)", id="macro-key-label")
                            yield Static("", id="macro-capture-status")
                        with Horizontal(id="macro-text-row", classes="field-row"):
                            yield Label("Text", classes="form-label", id="macro-text-label")
                            yield Input(placeholder="text with ; separators", id="macro-text")
                        with Horizontal(id="macro-toggle-row", classes="field-row"):
                            yield Label("Toggle:", classes="toggle-label")
                            yield Switch(value=False, id="macro-toggle")
                        with Horizontal(id="macro-toggle-text-row", classes="field-row"):
                            yield Label("Off command", classes="form-label")
                            yield Input(
                                placeholder="off command with ; separators", id="macro-toggle-text"
                            )
                        with Horizontal(classes="field-row"):
                            yield Button("Travel", id="macro-fast-travel", classes="insert-btn")
                            yield Button("Return", id="macro-return", classes="insert-btn")
                        with Horizontal(classes="field-row"):
                            yield Button(
                                "Autodiscover", id="macro-autodiscover", classes="insert-btn"
                            )
                            yield Button(
                                "Random Walk", id="macro-btn-randomwalk", classes="insert-btn"
                            )
                            yield Button("Delay", id="macro-delay", classes="insert-btn")
                            yield Button("When", id="macro-btn-when", classes="insert-btn")
                            yield Button("Until", id="macro-btn-until", classes="insert-btn")
                        with Horizontal(id="macro-form-buttons", classes="edit-form-buttons"):
                            yield Label(" ", classes="form-btn-spacer")
                            yield Button("Cancel", variant="default", id="macro-cancel-form")
                            yield Button("OK", variant="success", id="macro-ok")
                    yield Static("", id="macro-count")

    def on_mount(self) -> None:
        """Load macros from file and populate table."""
        table = self.query_one("#macro-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Key", width=14, key="key")
        table.add_column("Command Text", key="text")
        table.add_column("Last", width=8, key="last")
        self.load_from_file()
        self.refresh_table()
        self.query_one("#macro-form").display = False

    def load_from_file(self) -> None:
        if not os.path.exists(self.path):
            return
        from .macros import load_macros

        try:
            macros = load_macros(self.path, self.session_key)
            self.macros = [
                (m.key, m.text, m.enabled, m.last_used, m.toggle, m.toggle_text) for m in macros
            ]
        except (ValueError, FileNotFoundError):
            pass

    def matches_search(self, idx: int, query: str) -> bool:
        """Match macro key, text, or toggle_text against search query."""
        key, text, _enabled, lu, toggle, toggle_text = self.macros[idx]
        q = query.lower()
        return q in key.lower() or q in text.lower() or q in toggle_text.lower()

    def refresh_table(self) -> None:
        table = self.query_one("#macro-table", DataTable)
        table.clear()
        q = self.search_query
        self.filtered_indices = []
        order = list(range(len(self.macros)))
        if self.sort_mode == "last_used":
            order.sort(key=lambda i: invert_ts(self.macros[i][3]))
        for i in order:
            key, text, enabled, last_used, toggle, toggle_text = self.macros[i]
            if q and not self.matches_search(i, q):
                continue
            status = "" if enabled else " (off)"
            if toggle:
                status += " (toggle)"
            lu = relative_time(last_used) if last_used else ""
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
    ) -> None:
        self.captured_key = key_val
        self.capturing = False
        self.capture_escape_pending = False
        label = self.query_one("#macro-key-label", Static)
        display = self.blessed_display(key_val) if key_val else "(none)"
        label.update(display)
        label.remove_class("capturing")
        self.query_one("#macro-capture-status", Static).update("")
        self.query_one("#macro-text", Input).value = text_val
        self.query_one("#macro-enabled", Switch).value = enabled
        self.query_one("#macro-toggle", Switch).value = toggle
        self.query_one("#macro-toggle-text", Input).value = toggle_text
        text_label = self.query_one("#macro-text-label", Label)
        text_label.update("On command" if toggle else "Text")
        self.query_one("#macro-toggle-text-row").display = toggle
        self.query_one("#macro-search", Input).display = False
        self.query_one("#macro-table").display = False
        self.query_one("#macro-form").display = True
        self.set_action_buttons_disabled(True)
        self.query_one("#macro-text", Input).focus()

    def hide_form(self) -> None:
        self.capturing = False
        self.capture_escape_pending = False
        super()._hide_form()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Show/hide toggle text row when the toggle switch changes."""
        if event.switch.id == "macro-toggle":
            on = event.value
            text_label = self.query_one("#macro-text-label", Label)
            text_label.update("On command" if on else "Text")
            self.query_one("#macro-toggle-text-row").display = on

    def submit_form(self) -> None:
        """Accept the current inline form values."""
        key_val = self.captured_key.strip()
        text_val = self.query_one("#macro-text", Input).value
        enabled = self.query_one("#macro-enabled", Switch).value
        toggle = self.query_one("#macro-toggle", Switch).value
        toggle_text = self.query_one("#macro-toggle-text", Input).value
        lu = self.macros[self.editing_idx][3] if self.editing_idx is not None else ""
        self.finalize_edit((key_val, text_val, enabled, lu, toggle, toggle_text), bool(key_val))

    REPL_RESERVED_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "KEY_F1",
            "KEY_F3",
            "KEY_F4",
            "KEY_F5",
            "KEY_F6",
            "KEY_F7",
            "KEY_F8",
            "KEY_F9",
            "KEY_F10",
            "KEY_F11",
            "KEY_F18",
            "KEY_F21",
        }
    )

    @staticmethod
    def blessed_display(blessed_name: str) -> str:
        r"""Format a blessed key name for display (strip KEY\\_ prefix)."""
        if blessed_name.startswith("KEY_"):
            return blessed_name[4:]
        return blessed_name

    def finish_capture(self, blessed_key: str, display: str) -> None:
        """Accept a captured key and update the form."""
        from blessed.line_editor import DEFAULT_KEYMAP

        if blessed_key in DEFAULT_KEYMAP:
            self.reject_capture(f"Rejected: {display} -- reserved by line editor")
            return
        if blessed_key in self.REPL_RESERVED_KEYS:
            self.reject_capture(f"Rejected: {display} -- reserved by REPL")
            return
        self.capturing = False
        self.capture_escape_pending = False
        self.captured_key = blessed_key
        label = self.query_one("#macro-key-label", Static)
        label.update(display)
        label.remove_class("capturing")
        self.query_one("#macro-capture-status", Static).update("")

    def reject_capture(self, reason: str) -> None:
        """Show a rejection message and stay in capture mode."""
        self.query_one("#macro-capture-status", Static).update(reason)

    def on_key(self, event: events.Key) -> None:
        """Handle key capture mode, then delegate to base navigation."""
        if self.capturing:
            event.stop()
            event.prevent_default()
            key = event.key

            if self.capture_escape_pending:
                self.capture_escape_pending = False
                if key == "escape":
                    blessed_key = "KEY_ESCAPE"
                    self.finish_capture(blessed_key, "ESCAPE")
                elif len(key) == 1 and key.isalpha():
                    blessed_key = "KEY_ALT_" + key.upper()
                    self.finish_capture(blessed_key, "ALT_" + key.upper())
                else:
                    self.reject_capture(f"Rejected: escape+{key} -- use Esc then a letter")
                return

            if key == "escape":
                self.capture_escape_pending = True
                self.query_one("#macro-capture-status", Static).update(
                    "Esc pressed -- now press a letter for Alt combo, "
                    "or Esc again for plain Escape"
                )
                return

            if key.startswith("f") and key[1:].isdigit():
                blessed_key = "KEY_" + key.upper()
                self.finish_capture(blessed_key, key.upper())
                return

            if key.startswith("ctrl+"):
                letter = key[5:]
                if len(letter) == 1 and letter.isalpha():
                    blessed_key = "KEY_CTRL_" + letter.upper()
                    self.finish_capture(blessed_key, "CTRL_" + letter.upper())
                    return

            if key.startswith("alt+"):
                letter = key[4:]
                if len(letter) == 1 and letter.isalpha():
                    blessed_key = "KEY_ALT_" + letter.upper()
                    self.finish_capture(blessed_key, "ALT_" + letter.upper())
                    return

            self.reject_capture(f"Rejected: {key} -- use F-keys, Ctrl+key, or Alt+key")
            return

        super().on_key(event)

    def action_cancel_or_close(self) -> None:
        """Cancel key capture or close the screen."""
        if self.capturing:
            return
        super().action_cancel_or_close()

    def do_extra_button(self, suffix: str, btn: str) -> None:
        """Handle macro-specific buttons (travel, capture, etc.)."""
        if suffix == "fast-travel":
            self.pick_room_for_travel()
        elif suffix == "capture":
            self.capturing = True
            self.capture_escape_pending = False
            label = self.query_one("#macro-key-label", Static)
            label.update("press keystroke to capture ...")
            label.add_class("capturing")
            self.query_one("#macro-capture-status", Static).update("")
        else:
            super()._on_extra_button(suffix, btn)

    def save_to_file(self) -> None:
        from .macros import Macro, save_macros

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        macros = [
            Macro(key=k, text=t, enabled=ena, last_used=lu, toggle=tog, toggle_text=tt)
            for k, t, ena, lu, tog, tt in self.macros
        ]
        save_macros(self.path, macros, self.session_key)


class MacroEditScreen(EditListScreen):
    """Thin screen wrapper for the macro editor."""

    def __init__(
        self, path: str, session_key: str = "", rooms_file: str = "", current_room_file: str = ""
    ) -> None:
        super().__init__()
        self.pane = MacroEditPane(
            path=path,
            session_key=session_key,
            rooms_file=rooms_file,
            current_room_file=current_room_file,
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


class AutoreplyEditPane(EditListPane):
    """Pane widget for autoreply rule editing."""

    growable_keys: list[str] = ["pattern", "reply"]

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_or_close", "Cancel", priority=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("plus", "reorder_hint", "Change Priority", key_display="+/=/-", show=True),
        Binding("enter", "save_hint", "Save", show=True),
        Binding("l", "sort_last", "Recent", show=True),
    ]

    DEFAULT_CSS = EditListPane.DEFAULT_CSS + """
    #autoreply-form { padding: 0 0 0 4; }
    #autoreply-form .form-label { width: 12; }
    #autoreply-form .form-label-mid { width: 9; }
    #autoreply-form .insert-btn { margin: 0; padding: 0 1; }
    #autoreply-cond-vital { width: 14; }
    #autoreply-cond-op { width: 8; }
    #autoreply-cond-val { width: 9; border: tall grey; }
    #autoreply-cond-val:focus { border: tall $accent; }
    """

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

    def compose(self) -> ComposeResult:
        """Build the autoreply editor layout."""
        with Vertical(id="autoreply-panel", classes="edit-panel"):
            with Horizontal(id="autoreply-body", classes="edit-body"):
                with Vertical(id="autoreply-button-col", classes="edit-button-col"):
                    yield Button("Add", variant="success", id="autoreply-add")
                    yield Button("Edit", variant="warning", id="autoreply-edit")
                    yield Button("Copy", id="autoreply-copy", classes="edit-copy")
                    yield Button("Delete", variant="error", id="autoreply-delete")
                    yield Button("Help", variant="success", id="autoreply-help")
                    yield Button("Save", variant="primary", id="autoreply-save")
                    yield Button("Cancel", id="autoreply-close")
                with Vertical(id="autoreply-right", classes="edit-right"):
                    yield Input(
                        placeholder="Search autoreplies\u2026",
                        id="autoreply-search",
                        classes="edit-search",
                    )
                    yield DataTable(id="autoreply-table", classes="edit-table")
                    with Vertical(id="autoreply-form", classes="edit-form"):
                        with Horizontal(classes="field-row"):
                            yield Label("Enabled:", classes="toggle-label")
                            yield Switch(value=True, id="autoreply-enabled")
                            yield Label("", classes="toggle-gap")
                            alw = Switch(value=False, id="autoreply-always")
                            alw.tooltip = "Match even while another rule's chain is active"
                            yield Label("Always:", classes="toggle-label")
                            yield alw
                            yield Label("", classes="toggle-gap")
                            imm = Switch(value=False, id="autoreply-immediate")
                            imm.tooltip = "Reply immediately without waiting for prompt"
                            yield Label("Immediate:", classes="toggle-label")
                            yield imm
                            yield Label("", classes="toggle-gap")
                            cs = Switch(value=False, id="autoreply-case-sensitive")
                            cs.tooltip = "Case-sensitive pattern matching"
                            yield Label("Case Sensitive:", classes="toggle-label")
                            yield cs
                        with Horizontal(classes="field-row"):
                            yield Label("Pattern", classes="form-label-short")
                            yield Input(placeholder="regex pattern", id="autoreply-pattern")
                        with Horizontal(classes="field-row"):
                            yield Label("Reply", classes="form-label-short")
                            yield Input(
                                placeholder=r"reply with \1 refs, ;/: seps", id="autoreply-reply"
                            )
                        with Horizontal(classes="field-row"):
                            yield Label("Condition", classes="form-label-short")
                            yield Select(
                                [
                                    ("(none)", ""),
                                    ("HP%", "HP%"),
                                    ("MP%", "MP%"),
                                    ("HP", "HP"),
                                    ("MP", "MP"),
                                ],
                                value="",
                                allow_blank=False,
                                id="autoreply-cond-vital",
                            )
                            yield Select(
                                [(">", ">"), ("<", "<"), (">=", ">="), ("<=", "<="), ("=", "=")],
                                value=">",
                                allow_blank=False,
                                id="autoreply-cond-op",
                            )
                            yield Input(value="99", placeholder="99", id="autoreply-cond-val")
                            yield Label("(as percent)", classes="form-label-pct")
                        with Horizontal(classes="field-row"):
                            yield Button("When", id="autoreply-btn-when", classes="insert-btn")
                            yield Button("Until", id="autoreply-btn-until", classes="insert-btn")
                            yield Button("Delay", id="autoreply-btn-delay", classes="insert-btn")
                            yield Button("Travel", id="autoreply-fast-travel", classes="insert-btn")
                            yield Button("Return", id="autoreply-return", classes="insert-btn")
                        with Horizontal(classes="field-row"):
                            yield Button(
                                "Autodiscover", id="autoreply-autodiscover", classes="insert-btn"
                            )
                            yield Button(
                                "Random Walk", id="autoreply-btn-randomwalk", classes="insert-btn"
                            )
                        with Horizontal(id="autoreply-form-buttons", classes="edit-form-buttons"):
                            yield Label(" ", classes="form-btn-spacer")
                            yield Button("Cancel", variant="default", id="autoreply-cancel-form")
                            yield Button("OK", variant="success", id="autoreply-ok")
                    yield Static("", id="autoreply-count")

    def on_mount(self) -> None:
        """Load autoreplies from file and populate table."""
        table = self.query_one("#autoreply-table", DataTable)
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
        from .autoreply import load_autoreplies

        try:
            rules = load_autoreplies(self.path, self.session_key)
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
        table = self.query_one("#autoreply-table", DataTable)
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
            lf = relative_time(rule.last_fired) if rule.last_fired else ""
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
        self.query_one("#autoreply-pattern", Input).value = pattern_val
        self.query_one("#autoreply-reply", Input).value = reply_val
        self.query_one("#autoreply-always", Switch).value = always
        self.query_one("#autoreply-enabled", Switch).value = enabled
        self.query_one("#autoreply-immediate", Switch).value = immediate
        self.query_one("#autoreply-case-sensitive", Switch).value = case_sensitive
        cond_vital, cond_op, cond_val = "", ">", "99"
        if when:
            vital = next(iter(when), "")
            expr = when.get(vital, ">99")
            import re as re

            if m := re.match(r"^(>=|<=|>|<|=)(\d+)$", expr):
                cond_vital, cond_op, cond_val = vital, m.group(1), m.group(2)
        self.query_one("#autoreply-cond-vital", Select).value = cond_vital
        self.query_one("#autoreply-cond-op", Select).value = cond_op
        self.query_one("#autoreply-cond-val", Input).value = cond_val
        cond_none = not when
        self.query_one("#autoreply-cond-op", Select).disabled = cond_none
        self.query_one("#autoreply-cond-val", Input).disabled = cond_none
        self.query_one("#autoreply-search", Input).display = False
        self.query_one("#autoreply-table").display = False
        self.query_one("#autoreply-form").display = True
        self.set_action_buttons_disabled(True)
        self.query_one("#autoreply-pattern", Input).focus()

    def submit_form(self) -> None:
        """Accept the current inline form values."""
        pattern_val = self.query_one("#autoreply-pattern", Input).value.strip()
        reply_val = self.query_one("#autoreply-reply", Input).value
        always = self.query_one("#autoreply-always", Switch).value
        enabled = self.query_one("#autoreply-enabled", Switch).value
        immediate = self.query_one("#autoreply-immediate", Switch).value
        case_sensitive = self.query_one("#autoreply-case-sensitive", Switch).value
        cond_vital = self.query_one("#autoreply-cond-vital", Select).value
        cond_op = self.query_one("#autoreply-cond-op", Select).value
        cond_val = self.query_one("#autoreply-cond-val", Input).value.strip()
        when: dict[str, str] | None = None
        if cond_vital and isinstance(cond_vital, str) and cond_vital in ("HP%", "MP%", "HP", "MP"):
            try:
                int(cond_val or "99")
            except ValueError:
                cond_val = "99"
            when = {cond_vital: f"{cond_op}{cond_val or '99'}"}
        if pattern_val:
            import re

            try:
                re.compile(pattern_val)
            except re.error as exc:
                self.notify(f"Invalid regex: {exc}", severity="error")
                return
        lf = self.rules[self.editing_idx].last_fired if self.editing_idx is not None else ""
        entry = AutoreplyTuple(
            pattern_val, reply_val, always, enabled, when, immediate, lf, case_sensitive
        )
        self.finalize_edit(entry, bool(pattern_val))

    def on_select_changed(self, event: Select.Changed) -> None:
        """Disable operator/value fields when condition vital is '(none)'."""
        if event.select.id == "autoreply-cond-vital":
            disabled = not event.value or event.value is Select.BLANK
            self.query_one("#autoreply-cond-op", Select).disabled = disabled
            self.query_one("#autoreply-cond-val", Input).disabled = disabled

    def do_extra_button(self, suffix: str, btn: str) -> None:
        """Handle autoreply-specific buttons (travel, etc.)."""
        if suffix == "fast-travel":
            self.pick_room_for_travel()
        else:
            super()._on_extra_button(suffix, btn)

    def save_to_file(self) -> None:
        import re

        from .autoreply import AutoreplyRule, save_autoreplies

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        rules = []
        for t in self.rules:
            flags = re.MULTILINE | re.DOTALL
            if not t.case_sensitive:
                flags |= re.IGNORECASE
            rules.append(
                AutoreplyRule(
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
        save_autoreplies(self.path, rules, self.session_key)


class AutoreplyEditScreen(EditListScreen):
    """Thin screen wrapper for the autoreply editor."""

    def __init__(self, path: str, session_key: str = "", select_pattern: str = "") -> None:
        super().__init__()
        self.pane = AutoreplyEditPane(
            path=path, session_key=session_key, select_pattern=select_pattern
        )


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


class HighlightEditPane(EditListPane):
    """Pane widget for highlight rule editing."""

    growable_keys: list[str] = ["pattern"]

    DEFAULT_CSS = EditListPane.DEFAULT_CSS + """
    #highlight-form { padding: 0 0 0 4; }
    #highlight-form .form-label { width: 12; }
    """

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

    def compose(self) -> ComposeResult:
        """Build the highlight editor widget tree."""
        with Vertical(id="highlight-panel", classes="edit-panel"):
            with Horizontal(id="highlight-body", classes="edit-body"):
                with Vertical(id="highlight-button-col", classes="edit-button-col"):
                    yield Button("Add", variant="success", id="highlight-add")
                    yield Button("Edit", variant="warning", id="highlight-edit")
                    yield Button("Copy", id="highlight-copy", classes="edit-copy")
                    yield Button("Delete", variant="error", id="highlight-delete")
                    yield Button("Help", variant="success", id="highlight-help")
                    yield Button("Save", variant="primary", id="highlight-save")
                    yield Button("Cancel", id="highlight-close")
                with Vertical(id="highlight-right", classes="edit-right"):
                    yield Input(
                        placeholder="Search highlights\u2026",
                        id="highlight-search",
                        classes="edit-search",
                    )
                    yield DataTable(id="highlight-table", classes="edit-table")
                    with Vertical(id="highlight-form", classes="edit-form"):
                        with Horizontal(classes="field-row"):
                            yield Label("Enabled:", classes="toggle-label")
                            yield Switch(value=True, id="highlight-enabled")
                            yield Label("", classes="toggle-gap")
                            sm = Switch(value=False, id="highlight-stop-movement")
                            sm.tooltip = "Cancel autodiscover/randomwalk when matched"
                            yield Label("Stop:", classes="toggle-label")
                            yield sm
                            yield Label("", classes="toggle-gap")
                            cs = Switch(value=False, id="highlight-case-sensitive")
                            cs.tooltip = "Case-sensitive pattern matching"
                            yield Label("Case Sensitive:", classes="toggle-label")
                            yield cs
                            yield Label("", classes="toggle-gap")
                            ml = Switch(value=False, id="highlight-multiline")
                            ml.tooltip = "Match pattern across multiple lines"
                            yield Label("Multiline:", classes="toggle-label")
                            yield ml
                        with Horizontal(classes="field-row"):
                            cap_sw = Switch(value=False, id="highlight-captured")
                            cap_sw.tooltip = "Capture regex groups into variables"
                            yield Label("Captured:", classes="toggle-label")
                            yield cap_sw
                            yield Label("", classes="toggle-gap")
                            yield Label("Capture Name:", classes="toggle-label")
                            yield Input(
                                value="captures",
                                placeholder="channel name",
                                id="highlight-capture-name",
                            )
                        with Horizontal(classes="field-row"):
                            yield Label("Pattern", classes="form-label-short")
                            yield Input(placeholder="regex pattern", id="highlight-pattern")
                        with Horizontal(classes="field-row"):
                            yield Label("Highlight", classes="form-label-short")
                            yield Input(
                                placeholder="eg. blink_black_on_yellow", id="highlight-style"
                            )
                        with Vertical(id="highlight-capture-fields"):
                            with Vertical(id="highlight-captures-container"):
                                pass
                            yield Button(
                                "Add Capture", variant="default", id="highlight-add-capture"
                            )
                        with Horizontal(id="highlight-form-buttons", classes="edit-form-buttons"):
                            yield Label(" ", classes="form-btn-spacer")
                            yield Button("Cancel", variant="default", id="highlight-cancel-form")
                            yield Button("OK", variant="success", id="highlight-ok")
                    yield Static("", id="highlight-count")

    def on_mount(self) -> None:
        """Configure highlight table columns and load rules from file."""
        table = self.query_one("#highlight-table", DataTable)
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
        from .highlighter import DEFAULT_AUTOREPLY_HIGHLIGHT

        if not any(r.builtin for r in self.rules):
            self.rules.insert(
                0,
                HighlightTuple(
                    self.AUTOREPLY_PATTERN,
                    DEFAULT_AUTOREPLY_HIGHLIGHT,
                    enabled=True,
                    stop_movement=False,
                    builtin=True,
                ),
            )

    def load_from_file(self) -> None:
        if not os.path.exists(self.path):
            self.ensure_builtin()
            return
        from .highlighter import load_highlights

        try:
            rules = load_highlights(self.path, self.session_key)
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
        table = self.query_one("#highlight-table", DataTable)
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
        pat_input = self.query_one("#highlight-pattern", Input)
        pat_input.value = pattern_val
        pat_input.disabled = builtin
        self.query_one("#highlight-style", Input).value = highlight_val
        self.query_one("#highlight-enabled", Switch).value = enabled
        stop_sw = self.query_one("#highlight-stop-movement", Switch)
        stop_sw.value = stop_movement
        stop_sw.disabled = builtin
        cs_sw = self.query_one("#highlight-case-sensitive", Switch)
        cs_sw.value = case_sensitive
        cs_sw.disabled = builtin
        ml_sw = self.query_one("#highlight-multiline", Switch)
        ml_sw.value = multiline
        ml_sw.disabled = builtin
        cap_sw = self.query_one("#highlight-captured", Switch)
        cap_sw.value = captured
        cap_sw.disabled = builtin
        self.query_one("#highlight-capture-name", Input).value = capture_name
        container = self.query_one("#highlight-captures-container", Vertical)
        container.remove_children()
        for cap in captures:
            self.add_capture_row(container, cap.get("key", "KeyName"), cap.get("value", r"\1"))
        cap_fields = self.query_one("#highlight-capture-fields", Vertical)
        cap_fields.display = captured
        self.query_one("#highlight-search", Input).display = False
        self.query_one("#highlight-table").display = False
        self.query_one("#highlight-form").display = True
        self.set_action_buttons_disabled(True)
        if builtin:
            self.query_one("#highlight-style", Input).focus()
        else:
            pat_input.focus()

    def add_capture_row(
        self, container: Vertical, key: str = "KeyName", value: str = r"\1"
    ) -> None:
        """Append a key/value capture row to the captures container."""
        row_id = f"cap-row-{id(container)}-{len(container.children)}"
        btn = Button("X", variant="error", classes="capture-remove")
        btn.styles.width = 5
        row = Horizontal(
            Input(value=key, placeholder="Key", classes="capture-key"),
            Input(value=value, placeholder="Value", classes="capture-value"),
            btn,
            id=row_id,
            classes="capture-row",
        )
        container.mount(row)

    def do_captured_toggle(self, value: bool) -> None:
        """Show or hide capture fields when Captured switch changes."""
        cap_fields = self.query_one("#highlight-capture-fields", Vertical)
        cap_fields.display = value

    def submit_form(self) -> None:
        import re as re

        pattern_val = self.query_one("#highlight-pattern", Input).value.strip()
        highlight_val = self.query_one("#highlight-style", Input).value.strip()
        enabled = self.query_one("#highlight-enabled", Switch).value
        stop_movement = self.query_one("#highlight-stop-movement", Switch).value
        case_sensitive = self.query_one("#highlight-case-sensitive", Switch).value
        multiline = self.query_one("#highlight-multiline", Switch).value
        if pattern_val:
            try:
                re.compile(pattern_val)
            except re.error as exc:
                self.notify(f"Invalid regex: {exc}", severity="error")
                return
            for i, existing in enumerate(self.rules):
                if (
                    i != self.editing_idx
                    and not existing.builtin
                    and existing.pattern == pattern_val
                ):
                    self.notify(f"Duplicate pattern: {pattern_val!r}", severity="error")
                    return
        if highlight_val:
            from .client_repl import get_term
            from .highlighter import validate_highlight

            try:
                term = get_term()
            except ImportError:
                term = None
            if term is not None and not validate_highlight(term, highlight_val):
                self.notify(f"Invalid highlight style: {highlight_val!r}", severity="error")
                return
        captured = self.query_one("#highlight-captured", Switch).value
        capture_name = self.query_one("#highlight-capture-name", Input).value.strip()
        if not capture_name:
            capture_name = "captures"
        captures_list: list[dict[str, str]] = []
        container = self.query_one("#highlight-captures-container", Vertical)
        for row in container.children:
            inputs = list(row.query(Input))
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

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Toggle capture fields visibility when Captured switch changes."""
        if event.switch.id == "highlight-captured":
            self.do_captured_toggle(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses for add, edit, delete, save, and cancel."""
        btn = event.button.id or ""
        if btn == "highlight-delete":
            idx = self.selected_idx()
            if idx is not None and idx < len(self.rules) and self.rules[idx].builtin:
                self.notify("Cannot delete the builtin autoreply highlight rule.")
                return
        if btn == "highlight-add-capture":
            container = self.query_one("#highlight-captures-container", Vertical)
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
        import re as re

        from .highlighter import HighlightRule, save_highlights

        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        rules = []
        for t in self.rules:
            flags = re.MULTILINE | re.DOTALL
            if not t.case_sensitive:
                flags |= re.IGNORECASE
            rules.append(
                HighlightRule(
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
        save_highlights(self.path, rules, self.session_key)


class HighlightEditScreen(EditListScreen):
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


class ProgressBarEditPane(EditListPane):
    """Pane widget for GMCP progress bar configuration."""

    growable_keys: list[str] = ["name"]

    DEFAULT_CSS = EditListPane.DEFAULT_CSS + """
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

    def compose(self) -> ComposeResult:
        """Build the progress bar editor widget tree."""
        pfx = "progressbar"
        with Vertical(id=f"{pfx}-panel", classes="edit-panel"):
            with Horizontal(id=f"{pfx}-body", classes="edit-body"):
                with Vertical(id=f"{pfx}-button-col", classes="edit-button-col"):
                    yield Button("Add", variant="success", id=f"{pfx}-add")
                    yield Button("Edit", variant="warning", id=f"{pfx}-edit")
                    yield Button("Copy", id=f"{pfx}-copy", classes="edit-copy")
                    yield Button("Detect", variant="primary", id=f"{pfx}-detect")
                    yield Button("Help", variant="success", id=f"{pfx}-help")
                    yield Button("Save", variant="primary", id=f"{pfx}-save")
                    yield Button("Cancel", id=f"{pfx}-close")
                with Vertical(id=f"{pfx}-right", classes="edit-right"):
                    yield Input(
                        placeholder="Search bars\u2026", id=f"{pfx}-search", classes="edit-search"
                    )
                    yield DataTable(id=f"{pfx}-table", classes="edit-table")
                    with VerticalScroll(id=f"{pfx}-form", classes="edit-form"):
                        with Horizontal(classes="field-row"):
                            yield Label("Name", classes="form-label")
                            yield Input(placeholder="bar name", id="pb-name")
                            yield Label("", classes="toggle-gap")
                            yield Label("Enabled:", classes="toggle-label")
                            yield Switch(value=True, id="pb-enabled")
                        with Horizontal(classes="field-row"):
                            yield Label("Source", classes="form-label")
                            yield Select[str](
                                [],
                                id="pb-gmcp-select",
                                allow_blank=True,
                                prompt="Select source\u2026",
                            )
                        with Horizontal(classes="field-row"):
                            yield Label("Val/Max", classes="form-label")
                            yield Select[str](
                                [],
                                id="pb-value-select",
                                allow_blank=True,
                                prompt="Value field\u2026",
                            )
                            yield Label("", classes="form-gap")
                            yield Select[str](
                                [], id="pb-max-select", allow_blank=True, prompt="Max field\u2026"
                            )
                        with Horizontal(id="pb-color-row1", classes="field-row"):
                            yield Label("Color Mode", classes="form-label")
                            with RadioSet(id="pb-color-mode"):
                                yield RadioButton("Theme", id="pb-mode-theme", value=True)
                                yield RadioButton("Custom", id="pb-mode-custom")
                            yield Label("", id="pb-color-gap1", classes="form-gap")
                            yield Label("Path", classes="form-label")
                            with RadioSet(id="pb-color-path"):
                                yield RadioButton("Shortest", id="pb-path-shortest", value=True)
                                yield RadioButton("Longest", id="pb-path-longest")
                        with Horizontal(classes="field-row"):
                            yield Label("Side", classes="form-label")
                            with RadioSet(id="pb-side"):
                                yield RadioButton("Left", id="pb-side-left", value=True)
                                yield RadioButton("Right", id="pb-side-right")
                        with Horizontal(id="pb-color-row2", classes="field-row"):
                            yield Label("Min:", classes="form-label")
                            yield Select[str](
                                self.theme_color_options(),
                                id="pb-color-min",
                                value="error",
                                allow_blank=False,
                            )
                            yield Static("", id="pb-color-swatch-min")
                            yield Select[str](
                                self.text_color_options(False),
                                id="pb-text-min",
                                value="auto",
                                allow_blank=False,
                            )
                        with Horizontal(id="pb-color-row3", classes="field-row"):
                            yield Label("Max:", classes="form-label")
                            yield Select[str](
                                self.theme_color_options(),
                                id="pb-color-max",
                                value="success",
                                allow_blank=False,
                            )
                            yield Static("", id="pb-color-swatch-max")
                            yield Select[str](
                                self.text_color_options(False),
                                id="pb-text-max",
                                value="auto",
                                allow_blank=False,
                            )
                        yield Static("", id="pb-preview-bar")
                        yield Static("", id="pb-preview-gradient")
                        with Horizontal(id=f"{pfx}-form-buttons", classes="edit-form-buttons"):
                            yield Label(" ", classes="form-btn-spacer")
                            yield Button("Cancel", variant="default", id=f"{pfx}-cancel-form")
                            yield Button("OK", variant="success", id=f"{pfx}-ok")
                    yield Static("", id=f"{pfx}-count")

    @staticmethod
    def color_options() -> list[tuple[RichText, str]]:
        """Build Select options from all Rich named colors with swatches."""
        from rich.text import Text

        from .progressbars import CURATED_COLORS

        options: list[tuple[Text, str]] = []
        for c in CURATED_COLORS:
            label = Text()
            label.append("\u2588 ", style=c)
            label.append(c)
            options.append((label, c))
        return options

    @staticmethod
    def theme_color_options() -> list[tuple[RichText, str]]:
        """Build Select options from Textual theme colors with swatches."""
        from rich.text import Text

        from .progressbars import get_theme_colors

        options: list[tuple[Text, str]] = []
        for name, hex_val in get_theme_colors().items():
            label = Text()
            label.append("\u2588 ", style=hex_val[:7])
            label.append(name)
            options.append((label, name))
        return options

    @staticmethod
    def text_color_options(is_custom: bool) -> list[tuple[RichText, str]]:
        """Build Select options for text color: ``auto`` plus bar color options."""
        from rich.text import Text

        auto_label = Text()
        auto_label.append("auto")
        options: list[tuple[Text, str]] = [(auto_label, "auto")]
        if is_custom:
            options.extend(ProgressBarEditPane.color_options())
        else:
            options.extend(ProgressBarEditPane.theme_color_options())
        return options

    def on_mount(self) -> None:
        """Configure table columns and load bars from file."""
        table = self.query_one("#progressbar-table", DataTable)
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
        from .progressbars import load_progressbars

        bars = load_progressbars(self.path, self.session_key)
        self.bars = [ProgressBarTuple(*b) for b in bars]

    def matches_search(self, idx: int, query: str) -> bool:
        bar = self.bars[idx]
        q = query.lower()
        return q in bar.name.lower() or q in bar.gmcp_package.lower()

    SWATCH_STEPS = 10

    @staticmethod
    def color_swatch(bar: ProgressBarTuple) -> RichText:
        """Build a solid-block gradient swatch for the Color column."""
        from rich.text import Text

        from .progressbars import BarConfig, bar_color_at

        cfg = BarConfig(
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
        swatch = Text()
        for i in range(steps):
            f = i / max(1, steps - 1)
            c = bar_color_at(f, cfg)
            swatch.append(" ", style=f"on {c}")
        return swatch

    def refresh_table(self) -> None:
        table = self.query_one("#progressbar-table", DataTable)
        table.clear()
        q = self.search_query
        self.filtered_indices = []
        for i, bar in enumerate(self.bars):
            if q and not self.matches_search(i, q):
                continue
            self.filtered_indices.append(i)
            enabled = "Yes" if bar.enabled else "No"
            display_name = bar.name if len(bar.name) <= 19 else bar.name[:18] + "\u2026"
            val_f = (
                bar.value_field if len(bar.value_field) <= 13 else bar.value_field[:12] + "\u2026"
            )
            max_f = bar.max_field if len(bar.max_field) <= 13 else bar.max_field[:12] + "\u2026"
            swatch = self.color_swatch(bar)
            row_pos = len(self.filtered_indices) - 1
            table.add_row(
                str(i + 1),
                display_name,
                bar.gmcp_package,
                val_f,
                max_f,
                enabled,
                swatch,
                key=str(row_pos),
            )
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
        from .progressbars import TRAVEL_BAR_NAME

        is_travel = name == TRAVEL_BAR_NAME or (name and not gmcp_package)
        self.populating_form = True
        self.query_one("#pb-name", Input).value = name
        self.query_one("#pb-enabled", Switch).value = enabled
        # Populate source dropdown
        pkg_options = [(p, p) for p in self.gmcp_packages]
        src_sel = self.query_one("#pb-gmcp-select", Select)
        src_sel.set_options(pkg_options)
        if gmcp_package and gmcp_package in self.gmcp_packages:
            src_sel.value = gmcp_package
        elif gmcp_package:
            src_sel.set_options(pkg_options + [(gmcp_package, gmcp_package)])
            src_sel.value = gmcp_package
        # Disable source fields for Travel bar
        src_sel.disabled = is_travel
        val_sel = self.query_one("#pb-value-select", Select)
        max_sel = self.query_one("#pb-max-select", Select)
        val_sel.disabled = is_travel
        max_sel.disabled = is_travel
        # Populate value/max field dropdowns
        self.form_source_pkg = gmcp_package
        self.populate_field_selects(gmcp_package, value_field, max_field)
        self.populating_form = False
        # Color mode -- set the desired button to on; RadioSet will turn the
        # other off via its internal on_radio_button_changed handler.
        is_custom = color_mode == "custom"
        self.form_color_mode = color_mode
        target = "#pb-mode-custom" if is_custom else "#pb-mode-theme"
        self.query_one(target, RadioButton).value = True
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
        self.query_one(path_target, RadioButton).value = True
        # Side
        side_target = "#pb-side-right" if side == "right" else "#pb-side-left"
        self.query_one(side_target, RadioButton).value = True
        # Show form + start preview animation
        self.query_one("#progressbar-search", Input).display = False
        self.query_one("#progressbar-table").display = False
        self.query_one("#progressbar-form").display = True
        self.set_action_buttons_disabled(True)
        self.preview_phase = 0.0
        self.start_preview_timer()
        self.query_one("#pb-name", Input).focus()

    def submit_form(self) -> None:
        name = self.query_one("#pb-name", Input).value.strip()
        if not name:
            self.notify("Name is required.", severity="error")
            return
        enabled = self.query_one("#pb-enabled", Switch).value
        gmcp_pkg = self.get_select_value("#pb-gmcp-select", "")
        value_field = self.get_select_value("#pb-value-select", "")
        max_field = self.get_select_value("#pb-max-select", "")
        color_mode = self.get_color_mode()
        color_name_max = self.get_select_value("#pb-color-max", "green")
        color_name_min = self.get_select_value("#pb-color-min", "red")
        color_path = (
            "longest" if self.query_one("#pb-path-longest", RadioButton).value else "shortest"
        )
        text_color_fill = self.get_select_value("#pb-text-min", "auto")
        text_color_empty = self.get_select_value("#pb-text-max", "auto")
        side = "right" if self.query_one("#pb-side-right", RadioButton).value else "left"
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
        if self.query_one("#pb-mode-custom", RadioButton).value:
            return "custom"
        return "theme"

    def get_select_value(self, sel_id: str, default: str) -> str:
        """Return a Select widget's value as str."""
        sel = self.query_one(sel_id, Select)
        val = sel.value
        if isinstance(val, str):
            return val
        return default

    def update_swatch(self, widget_id: str, color_name: str) -> None:
        """Update a color swatch Static with a colored block."""
        try:
            swatch = self.query_one(widget_id, Static)
            swatch.update(f"[on {color_name}]    [/]")
        except NoMatches:
            pass

    def hide_form(self) -> None:
        self.stop_preview_timer()
        self.form_source_pkg = ""
        self.form_color_mode = ""
        super()._hide_form()

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
        from rich.text import Text

        from .progressbars import BarConfig, bar_color_at, resolve_text_color_hex
        from .client_repl_render import vital_bar

        # Triangular wave 0->1->0 with ease-in-out
        t = self.preview_phase * 2.0
        if t > 1.0:
            t = 2.0 - t
        fraction = self.ease_in_out(t)

        try:
            w = self.query_one("#pb-preview-bar", Static).size.width
            bar_w = w - 2 if w > 12 else 24
        except NoMatches:
            bar_w = 24
        cur = int(fraction * 1000)
        mx = 1000
        name = self.query_one("#pb-name", Input).value.strip() or "bar"

        # Build BarConfig from form
        color_mode = self.get_color_mode()
        max_c = self.get_select_value("#pb-color-max", "success")
        min_c = self.get_select_value("#pb-color-min", "error")
        path = "longest" if self.query_one("#pb-path-longest", RadioButton).value else "shortest"
        cfg = BarConfig(
            name,
            "",
            "",
            "",
            color_mode=color_mode,
            color_name_max=max_c,
            color_name_min=min_c,
            color_path=path,
        )

        text_fill_val = self.get_select_value("#pb-text-min", "auto")
        text_empty_val = self.get_select_value("#pb-text-max", "auto")
        text_fill = resolve_text_color_hex(text_fill_val)
        text_empty = resolve_text_color_hex(text_empty_val)

        # 1) Toolbar-style bar via vital_bar (SGR -> ANSI -> Rich Text)
        kind = name.lower()
        color = bar_color_at(fraction, cfg)
        fragments = vital_bar(
            cur,
            mx,
            bar_w,
            kind,
            color_override=color,
            text_fill_color=text_fill,
            text_empty_color=text_empty,
        )
        ansi_str = "".join(sgr + text for sgr, text in fragments) + "\x1b[0m"
        try:
            bar_text = Text.from_ansi(ansi_str)
            self.query_one("#pb-preview-bar", Static).update(bar_text)
        except NoMatches:
            pass

        # 2) Static color gradient showing the full min->max spectrum
        steps = bar_w + 2
        gradient_parts: list[str] = []
        for i in range(steps):
            f = i / max(1, steps - 1)
            c = bar_color_at(f, cfg)
            gradient_parts.append(f"[on {c}] [/]")
        try:
            self.query_one("#pb-preview-gradient", Static).update("".join(gradient_parts))
        except NoMatches:
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
        max_sel = self.query_one("#pb-color-max", Select)
        min_sel = self.query_one("#pb-color-min", Select)
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
        text_min_sel = self.query_one("#pb-text-min", Select)
        text_max_sel = self.query_one("#pb-text-max", Select)
        text_min_sel.set_options(text_options)
        text_max_sel.set_options(text_options)
        text_vals = {v for _, v in text_options}
        text_min_sel.value = preserve_text_fill if preserve_text_fill in text_vals else "auto"
        text_max_sel.value = preserve_text_empty if preserve_text_empty in text_vals else "auto"

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Toggle custom color fields and swap color options when mode changes."""
        if event.radio_set.id == "pb-color-mode":
            is_custom = self.query_one("#pb-mode-custom", RadioButton).value
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

    def populate_field_selects(
        self, pkg_name: str, value_field: str = "", max_field: str = ""
    ) -> None:
        """Populate value/max field Select widgets from GMCP snapshot fields."""
        fields = self.gmcp_fields.get(pkg_name, [])
        field_opts = [(f, f) for f in fields]
        val_sel = self.query_one("#pb-value-select", Select)
        max_sel = self.query_one("#pb-max-select", Select)
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

    def on_select_changed(self, event: Select.Changed) -> None:
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

    def on_input_changed(self, event: Input.Changed) -> None:
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
        gmcp_data = {
            pkg: info.get("data", info) for pkg, info in packages.items() if isinstance(info, dict)
        }
        from .progressbars import detect_progressbars

        detected = detect_progressbars(gmcp_data)
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to base class."""
        super().on_button_pressed(event)

    def save_to_file(self) -> None:
        from .progressbars import BarConfig, save_progressbars

        bars = [BarConfig(*t) for t in self.bars]
        save_progressbars(self.path, self.session_key, bars)


class ProgressBarEditScreen(EditListScreen):
    """Thin screen wrapper for the progress bar editor."""

    def __init__(self, path: str, session_key: str = "", gmcp_snapshot_path: str = "") -> None:
        super().__init__()
        self.pane = ProgressBarEditPane(
            path=path, session_key=session_key, gmcp_snapshot_path=gmcp_snapshot_path
        )


# ---------------------------------------------------------------------------
# Standalone entry points
# ---------------------------------------------------------------------------


def edit_macros_main(
    path: str,
    session_key: str = "",
    rooms_file: str = "",
    current_room_file: str = "",
    logfile: str = "",
) -> None:
    """Launch standalone macro editor TUI."""
    launch_editor(
        MacroEditScreen(
            path=path,
            session_key=session_key,
            rooms_file=rooms_file,
            current_room_file=current_room_file,
        ),
        session_key=session_key,
        logfile=logfile,
    )


def edit_autoreplies_main(
    path: str, session_key: str = "", select_pattern: str = "", logfile: str = ""
) -> None:
    """Launch standalone autoreply editor TUI."""
    launch_editor(
        AutoreplyEditScreen(path=path, session_key=session_key, select_pattern=select_pattern),
        session_key=session_key,
        logfile=logfile,
    )


def edit_highlights_main(path: str, session_key: str = "", logfile: str = "") -> None:
    """Launch standalone highlight editor TUI."""
    launch_editor(
        HighlightEditScreen(path=path, session_key=session_key),
        session_key=session_key,
        logfile=logfile,
    )


def edit_progressbars_main(
    path: str, session_key: str = "", gmcp_snapshot_path: str = "", logfile: str = ""
) -> None:
    """Launch standalone progress bar editor TUI."""
    launch_editor(
        ProgressBarEditScreen(
            path=path, session_key=session_key, gmcp_snapshot_path=gmcp_snapshot_path
        ),
        session_key=session_key,
        logfile=logfile,
    )


def show_help_main(topic: str = "keybindings", logfile: str = "") -> None:
    """Launch standalone help viewer TUI."""
    launch_editor(CommandHelpScreen(topic=topic), logfile=logfile)


# ---------------------------------------------------------------------------
# Theme editor
# ---------------------------------------------------------------------------

SWATCH_KEYS = (
    "primary",
    "secondary",
    "accent",
    "success",
    "error",
    "warning",
    "surface",
    "background",
    "foreground",
)


class ThemeEditPane(Vertical):
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

    def compose(self) -> ComposeResult:
        """Build the theme selection layout."""
        with Vertical(id="theme-panel"):
            yield Label("Select a theme:")
            yield DataTable(id="theme-table")
            yield Static("", id="theme-preview")

    def on_mount(self) -> None:
        """Populate the theme list on mount."""
        from textual.theme import BUILTIN_THEMES

        table = self.query_one("#theme-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Theme", "Preview")

        current = self.app.theme or ""
        for name in sorted(BUILTIN_THEMES):
            marker = "\u2713" if name == current else ""
            theme = BUILTIN_THEMES[name]
            cs = theme.to_color_system()
            colors = cs.generate()
            swatches = " ".join(
                f"[{colors.get(k, '#888')}]\u2588\u2588[/]" for k in SWATCH_KEYS if k in colors
            )
            table.add_row(f"{marker} {name}" if marker else f"  {name}", swatches, key=name)

        if current and current in BUILTIN_THEMES:
            idx = sorted(BUILTIN_THEMES).index(current)
            table.move_cursor(row=idx)

        self.update_preview()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Apply the selected theme."""
        name = str(event.row_key.value) if event.row_key.value else ""
        if name:
            self.refresh_markers(name)
            self.app.theme = name
            self.update_preview()

    def refresh_markers(self, active: str) -> None:
        """Update the check-mark column to reflect the active theme."""
        from textual.theme import BUILTIN_THEMES
        from textual.widgets.data_table import CellDoesNotExist

        table = self.query_one("#theme-table", DataTable)
        for name in sorted(BUILTIN_THEMES):
            marker = "\u2713" if name == active else ""
            label = f"{marker} {name}" if marker else f"  {name}"
            try:
                table.update_cell(name, "Theme", label)
            except CellDoesNotExist:
                pass

    def update_preview(self) -> None:
        """Show a color swatch preview of the current theme."""
        from textual.theme import BUILTIN_THEMES

        current = self.app.theme or ""
        theme = BUILTIN_THEMES.get(current)
        if theme is None:
            return
        cs = theme.to_color_system()
        colors = cs.generate()
        lines = []
        for k in SWATCH_KEYS:
            val = colors.get(k, "")
            if val:
                lines.append(f"[{val}]\u2588\u2588[/] {k}: {val}")
        preview = self.query_one("#theme-preview", Static)
        preview.update(" | ".join(lines))
