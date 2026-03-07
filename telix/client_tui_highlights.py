"""Highlight editor pane and screen for the telix TUI."""

# std imports
import os
import re
import shutil
from typing import Any, NamedTuple

# 3rd party
import textual.app
import textual.widget
import textual.widgets
import textual.containers

# local
from . import client_repl, highlighter, client_tui_base


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
            if isinstance(row, textual.widget.Widget):
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
