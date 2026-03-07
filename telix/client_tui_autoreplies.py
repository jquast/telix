"""Autoreply editor pane and screen for the telix TUI."""

# std imports
import os
import re
import json
from typing import Any, ClassVar, NamedTuple

# 3rd party
import textual.app
import textual.binding
import textual.widgets
import textual.containers

# local
from . import autoreply, client_tui_base


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

    growable_keys: list[str] = ["pattern"]

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
    #autoreply-cond-source { width: 25; }
    #autoreply-cond-vital { width: 18; }
    #autoreply-cond-op { width: 8; }
    #autoreply-cond-val { width: 9; border: tall grey; }
    #autoreply-cond-val:focus { border: tall $accent; }
    .command-text-area { height: 1fr; min-height: 3; }
    """
    )

    def __init__(
        self, path: str, session_key: str = "", select_pattern: str = "", gmcp_snapshot_path: str = ""
    ) -> None:
        """Initialize autoreply editor with file path and session key."""
        super().__init__()
        self.path = path
        self.session_key = session_key
        self.select_pattern = select_pattern
        self.gmcp_snapshot_path = gmcp_snapshot_path
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
                            yield textual.widgets.Label("Condition", classes="form-label-short")
                            yield textual.widgets.Select(
                                [("(none)", "")], value="", allow_blank=False, id="autoreply-cond-source"
                            )
                            yield textual.widgets.Select(
                                [("(none)", "")], value="", allow_blank=False, id="autoreply-cond-vital", disabled=True
                            )
                            yield textual.widgets.Select(
                                [(">", ">"), ("<", "<"), (">=", ">="), ("<=", "<="), ("=", "=")],
                                value=">",
                                allow_blank=False,
                                id="autoreply-cond-op",
                            )
                            yield textual.widgets.Input(value="99", placeholder="99", id="autoreply-cond-val")
                            yield textual.widgets.Label(
                                "(as percent)", id="autoreply-cond-pct-label", classes="form-label-pct"
                            )
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
                        yield textual.widgets.Label("Command Text", classes="form-label-short")
                        yield textual.widgets.TextArea(id="autoreply-reply", classes="command-text-area")
                        with textual.containers.Horizontal(id="autoreply-form-buttons", classes="edit-form-buttons"):
                            yield textual.widgets.Label(" ", classes="form-btn-spacer")
                            yield textual.widgets.Button("Cancel", variant="default", id="autoreply-cancel-form")
                            yield textual.widgets.Button("OK", variant="success", id="autoreply-ok")
                    yield textual.widgets.Static("", id="autoreply-count")

    def gmcp_source_choices(self) -> list[tuple[str, str]]:
        """Build Select choices for the GMCP source dropdown."""
        options: list[tuple[str, str]] = [("(none)", "")]
        if not self.gmcp_snapshot_path or not os.path.exists(self.gmcp_snapshot_path):
            return options
        with open(self.gmcp_snapshot_path, encoding="utf-8") as fh:
            data = json.load(fh)
        packages = data.get("packages", {}) if isinstance(data, dict) else {}
        for pkg_name, pkg_info in sorted(packages.items()):
            pkg_data = pkg_info.get("data", {}) if isinstance(pkg_info, dict) else {}
            if not isinstance(pkg_data, dict):
                continue
            if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pkg_data.values()):
                options.append((pkg_name, pkg_name))
        return options

    def gmcp_field_choices(self, source: str) -> list[tuple[str, str]]:
        """Build Select choices for the condition field given a source."""
        if not source or not self.gmcp_snapshot_path or not os.path.exists(self.gmcp_snapshot_path):
            return [("(none)", "")]
        with open(self.gmcp_snapshot_path, encoding="utf-8") as fh:
            data = json.load(fh)
        packages = data.get("packages", {}) if isinstance(data, dict) else {}
        pkg_info = packages.get(source, {})
        pkg_data = pkg_info.get("data", {}) if isinstance(pkg_info, dict) else {}
        if not isinstance(pkg_data, dict):
            return [("(none)", "")]
        fields = sorted(k for k, v in pkg_data.items() if isinstance(v, (int, float)) and not isinstance(v, bool))
        if not fields:
            return [("(none)", "")]
        field_lower_set = {k.lower() for k in fields}
        paired = [k for k in fields if f"max{k.lower()}" in field_lower_set]
        options: list[tuple[str, str]] = [("(none)", "")]
        for k in paired:
            options.append((f"{k}%", f"{k}%"))
        for k in fields:
            options.append((k, k))
        return options

    def find_condition_source(self, vital_key: str) -> str:
        """Return the GMCP source package for *vital_key*, or ``''`` if not found."""
        if not self.gmcp_snapshot_path or not os.path.exists(self.gmcp_snapshot_path):
            return ""
        base_key = vital_key[:-1] if vital_key.endswith("%") else vital_key
        with open(self.gmcp_snapshot_path, encoding="utf-8") as fh:
            data = json.load(fh)
        packages = data.get("packages", {}) if isinstance(data, dict) else {}
        for pkg_name, pkg_info in packages.items():
            pkg_data = pkg_info.get("data", {}) if isinstance(pkg_info, dict) else {}
            if not isinstance(pkg_data, dict):
                continue
            if base_key in pkg_data:
                return pkg_name
        return ""

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
        self.query_one("#autoreply-cond-source", textual.widgets.Select).set_options(self.gmcp_source_choices())
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
        from . import client_tui_editors

        table = self.query_one("#autoreply-table", textual.widgets.DataTable)
        table.clear()
        q = self.search_query
        self.filtered_indices = []
        order = list(range(len(self.rules)))
        if self.sort_mode == "last_fired":
            order.sort(key=lambda i: client_tui_editors.invert_ts(self.rules[i].last_fired))
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
        self.query_one("#autoreply-reply", textual.widgets.TextArea).text = reply_val
        self.query_one("#autoreply-always", textual.widgets.Switch).value = always
        self.query_one("#autoreply-enabled", textual.widgets.Switch).value = enabled
        self.query_one("#autoreply-immediate", textual.widgets.Switch).value = immediate
        self.query_one("#autoreply-case-sensitive", textual.widgets.Switch).value = case_sensitive
        cond_source, cond_vital, cond_op, cond_val = "", "", ">", "99"
        if when:
            vital = next(iter(when), "")
            expr = when.get(vital, ">99")
            if m := re.match(r"^(>=|<=|>|<|=)(\d+)$", expr):
                cond_vital, cond_op, cond_val = vital, m.group(1), m.group(2)
            cond_source = self.find_condition_source(cond_vital) if cond_vital else ""
        else:
            sources = [v for _, v in self.gmcp_source_choices() if v]
            if "Char.Vitals" in sources:
                cond_source = "Char.Vitals"
                fields = [v for _, v in self.gmcp_field_choices(cond_source) if v]
                if "hp%" in fields:
                    cond_vital = "hp%"
        source_sel = self.query_one("#autoreply-cond-source", textual.widgets.Select)
        source_sel.value = cond_source
        field_sel = self.query_one("#autoreply-cond-vital", textual.widgets.Select)
        field_sel.set_options(self.gmcp_field_choices(cond_source))
        field_sel.value = cond_vital
        field_sel.disabled = not cond_source
        self.query_one("#autoreply-cond-op", textual.widgets.Select).value = cond_op
        self.query_one("#autoreply-cond-val", textual.widgets.Input).value = cond_val
        cond_none = not cond_vital
        self.query_one("#autoreply-cond-op", textual.widgets.Select).disabled = cond_none
        self.query_one("#autoreply-cond-val", textual.widgets.Input).disabled = cond_none
        is_pct = cond_vital.endswith("%")
        self.query_one("#autoreply-cond-pct-label", textual.widgets.Label).display = is_pct
        self.query_one("#autoreply-search", textual.widgets.Input).display = False
        self.query_one("#autoreply-table").display = False
        self.query_one("#autoreply-form").display = True
        self.set_action_buttons_disabled(True)
        self.query_one("#autoreply-pattern", textual.widgets.Input).focus()

    def submit_form(self) -> None:
        """Accept the current inline form values."""
        pattern_val = self.query_one("#autoreply-pattern", textual.widgets.Input).value.strip()
        reply_val = self.query_one("#autoreply-reply", textual.widgets.TextArea).text
        always = self.query_one("#autoreply-always", textual.widgets.Switch).value
        enabled = self.query_one("#autoreply-enabled", textual.widgets.Switch).value
        immediate = self.query_one("#autoreply-immediate", textual.widgets.Switch).value
        case_sensitive = self.query_one("#autoreply-case-sensitive", textual.widgets.Switch).value
        cond_vital = self.query_one("#autoreply-cond-vital", textual.widgets.Select).value
        cond_op = self.query_one("#autoreply-cond-op", textual.widgets.Select).value
        cond_val = self.query_one("#autoreply-cond-val", textual.widgets.Input).value.strip()
        when: dict[str, str] | None = None
        if cond_vital and isinstance(cond_vital, str):
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
        """Update field/operator/value dropdowns when source or field changes."""
        if event.select.id == "autoreply-cond-source":
            source = event.value if isinstance(event.value, str) else ""
            field_sel = self.query_one("#autoreply-cond-vital", textual.widgets.Select)
            field_sel.set_options(self.gmcp_field_choices(source))
            field_sel.value = ""
            field_sel.disabled = not source
            self.query_one("#autoreply-cond-op", textual.widgets.Select).disabled = True
            self.query_one("#autoreply-cond-val", textual.widgets.Input).disabled = True
            self.query_one("#autoreply-cond-pct-label", textual.widgets.Label).display = False
        elif event.select.id == "autoreply-cond-vital":
            disabled = not event.value or event.value is textual.widgets.Select.BLANK
            self.query_one("#autoreply-cond-op", textual.widgets.Select).disabled = disabled
            self.query_one("#autoreply-cond-val", textual.widgets.Input).disabled = disabled
            is_pct = isinstance(event.value, str) and event.value.endswith("%")
            self.query_one("#autoreply-cond-pct-label", textual.widgets.Label).display = is_pct

    def do_extra_button(self, suffix: str, btn: str) -> None:
        """Handle autoreply-specific buttons (travel, etc.)."""
        if suffix == "fast-travel":
            self.pick_room_for_travel()
        else:
            super().do_extra_button(suffix, btn)

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

    def __init__(
        self, path: str, session_key: str = "", select_pattern: str = "", gmcp_snapshot_path: str = ""
    ) -> None:
        super().__init__()
        self.pane = AutoreplyEditPane(
            path=path, session_key=session_key, select_pattern=select_pattern, gmcp_snapshot_path=gmcp_snapshot_path
        )
