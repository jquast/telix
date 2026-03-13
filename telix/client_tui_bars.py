"""Progress bar and theme editor panes and screens for the telix TUI."""

# std imports
import os
import json
import shutil
import typing
from typing import TYPE_CHECKING

# 3rd party
import rich.text
import textual.app
import textual.theme
import textual.events
import textual.binding
import textual.widgets
import textual.css.query
import textual.containers
import textual.widgets.data_table

# local
from . import progressbars, client_tui_base, client_repl_render

if TYPE_CHECKING:
    RichText = rich.text.Text


class ProgressBarTuple(typing.NamedTuple):
    """Lightweight tuple for progress bar configs in the TUI editor."""

    name: str = ""
    gmcp_package: str = ""
    value_field: str = ""
    max_field: str = ""
    max_gmcp_package: str = ""
    enabled: bool = True
    color_mode: str = "theme"
    color_name_max: str = "success"
    color_name_min: str = "error"
    color_path: str = "shortest"
    text_color_fill: str = "auto"
    text_color_empty: str = "auto"
    display_order: int = 0
    side: str = "left"
    bar_type: str = "bar"
    label_format: str = "{value}"


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
    #pb-bar-type { max-width: 20; }
    #pb-label-format { max-width: 50; }
    #pb-label-format-row { height: auto; margin: 0; }
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
        self.preview_timer: typing.Any = None
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
    def items(self) -> list[typing.Any]:
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
                        with textual.containers.Horizontal(id="pb-type-row", classes="field-row"):
                            yield textual.widgets.Label("Type", classes="form-label")
                            with textual.widgets.RadioSet(id="pb-bar-type"):
                                yield textual.widgets.RadioButton("Progress Bar", id="pb-type-bar", value=True)
                                yield textual.widgets.RadioButton("Label", id="pb-type-label")
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
                        with textual.containers.Horizontal(id="pb-label-format-row", classes="field-row"):
                            yield textual.widgets.Label("Format", classes="form-label")
                            yield textual.widgets.Input(placeholder="{value}", id="pb-label-format")
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
    def color_options() -> list[tuple["RichText", str]]:
        """Build textual.widgets.Select options from all Rich named colors with swatches."""
        options: list[tuple[rich.text.Text, str]] = []
        for c in progressbars.CURATED_COLORS:
            label = rich.text.Text()
            label.append("\u2588 ", style=c)
            label.append(c)
            options.append((label, c))
        return options

    @staticmethod
    def theme_color_options() -> list[tuple["RichText", str]]:
        """Build textual.widgets.Select options from Textual theme colors with swatches."""
        options: list[tuple[rich.text.Text, str]] = []
        for name, hex_val in progressbars.get_theme_colors().items():
            label = rich.text.Text()
            label.append("\u2588 ", style=hex_val[:7])
            label.append(name)
            options.append((label, name))
        return options

    @staticmethod
    def text_color_options(is_custom: bool) -> list[tuple["RichText", str]]:
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
    def color_swatch(bar: ProgressBarTuple) -> "RichText":
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
            if bar.bar_type == "label":
                max_f = "(label)"
            else:
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
        max_gmcp_package: str = "",
        enabled: bool = True,
        color_mode: str = "theme",
        color_name_max: str = "green",
        color_name_min: str = "red",
        color_path: str = "shortest",
        text_color_fill: str = "auto",
        text_color_empty: str = "auto",
        display_order: int = 0,
        side: str = "left",
        bar_type: str = "bar",
        label_format: str = "{value}",
    ) -> None:
        is_travel = bool(name == progressbars.TRAVEL_BAR_NAME or (name and not gmcp_package))
        self.populating_form = True
        self.query_one("#pb-name", textual.widgets.Input).value = name
        self.query_one("#pb-enabled", textual.widgets.Switch).value = enabled
        pkg_options = [(p, p) for p in self.gmcp_packages]
        src_sel = self.query_one("#pb-gmcp-select", textual.widgets.Select)
        src_sel.set_options(pkg_options)
        if gmcp_package and gmcp_package in self.gmcp_packages:
            src_sel.value = gmcp_package
        elif gmcp_package:
            src_sel.set_options(pkg_options + [(gmcp_package, gmcp_package)])
            src_sel.value = gmcp_package
        src_sel.disabled = is_travel
        val_sel = self.query_one("#pb-value-select", textual.widgets.Select)
        max_sel = self.query_one("#pb-max-select", textual.widgets.Select)
        val_sel.disabled = is_travel
        max_sel.disabled = is_travel
        self.form_source_pkg = gmcp_package
        self.populate_field_selects(gmcp_package, value_field, max_field)
        self.populating_form = False
        is_custom = color_mode == "custom"
        self.form_color_mode = color_mode
        target = "#pb-mode-custom" if is_custom else "#pb-mode-theme"
        self.query_one(target, textual.widgets.RadioButton).value = True
        self.swap_color_options(
            is_custom,
            preserve_max=color_name_max,
            preserve_min=color_name_min,
            preserve_text_fill=text_color_fill,
            preserve_text_empty=text_color_empty,
        )
        is_longest = color_path == "longest"
        path_target = "#pb-path-longest" if is_longest else "#pb-path-shortest"
        self.query_one(path_target, textual.widgets.RadioButton).value = True
        side_target = "#pb-side-right" if side == "right" else "#pb-side-left"
        self.query_one(side_target, textual.widgets.RadioButton).value = True
        self.query_one("#progressbar-search", textual.widgets.Input).display = False
        self.query_one("#progressbar-table").display = False
        self.query_one("#progressbar-form").display = True
        self.set_action_buttons_disabled(True)
        is_label = bar_type == "label"
        type_target = "#pb-type-label" if is_label else "#pb-type-bar"
        self.query_one(type_target, textual.widgets.RadioButton).value = True
        self.query_one("#pb-label-format", textual.widgets.Input).value = label_format
        self.toggle_label_mode(is_label)
        self.preview_phase = 0.0
        if not is_label:
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
        bar_type = "label" if self.query_one("#pb-type-label", textual.widgets.RadioButton).value else "bar"
        label_format = self.query_one("#pb-label-format", textual.widgets.Input).value.strip() or "{value}"
        order = 0
        max_gmcp_pkg = ""
        if self.editing_idx is not None:
            order = self.bars[self.editing_idx].display_order
            max_gmcp_pkg = self.bars[self.editing_idx].max_gmcp_package
        entry = ProgressBarTuple(
            name=name,
            gmcp_package=gmcp_pkg,
            value_field=value_field,
            max_field=max_field,
            max_gmcp_package=max_gmcp_pkg,
            enabled=enabled,
            color_mode=color_mode,
            color_name_max=color_name_max,
            color_name_min=color_name_min,
            color_path=color_path,
            text_color_fill=text_color_fill,
            text_color_empty=text_color_empty,
            display_order=order,
            side=side,
            bar_type=bar_type,
            label_format=label_format,
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

    def toggle_label_mode(self, is_label: bool) -> None:
        """Show/hide form fields appropriate for label vs bar type."""
        self.query_one("#pb-label-format-row").display = is_label
        self.query_one("#pb-max-select", textual.widgets.Select).disabled = is_label
        self.query_one("#pb-color-row1").display = not is_label
        self.query_one("#pb-color-row2").display = not is_label
        self.query_one("#pb-color-row3").display = not is_label
        self.query_one("#pb-preview-bar").display = not is_label
        self.query_one("#pb-preview-gradient").display = not is_label
        if is_label:
            self.stop_preview_timer()
        else:
            self.start_preview_timer()

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
        return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)

    def update_preview(self) -> None:
        """Render animated preview bars from current form settings."""
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
        """Toggle custom color fields, bar type, and swap color options when mode changes."""
        if event.radio_set.id == "pb-bar-type":
            is_label = self.query_one("#pb-type-label", textual.widgets.RadioButton).value
            self.toggle_label_mode(is_label)
            return
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


SWATCH_KEYS = ("primary", "secondary", "accent", "success", "error", "warning", "surface", "background", "foreground")


class ThemeEditPane(textual.containers.Vertical):
    """Pane widget for selecting Textual built-in themes."""

    class Saved(textual.events.Event):
        """Posted when the user confirms a theme selection in modal mode."""

    class Cancelled(textual.events.Event):
        """Posted when the user cancels theme selection in modal mode."""

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
    #theme-modal-buttons { height: auto; margin-top: 1; }
    #theme-modal-buttons Button { margin-right: 1; min-width: 0; }
    """

    def __init__(self, session_key: str = "", modal: bool = False, **kwargs: typing.Any) -> None:
        """Initialize theme editor."""
        super().__init__(**kwargs)
        self.session_key = session_key
        self.modal = modal
        self.original_theme: str = ""

    def compose(self) -> textual.app.ComposeResult:
        """Build the theme selection layout."""
        with textual.containers.Vertical(id="theme-panel"):
            yield textual.widgets.Label("Select a theme:")
            yield textual.widgets.Static(" ")
            yield textual.widgets.DataTable(id="theme-table")
            yield textual.widgets.Static("", id="theme-preview")
            if self.modal:
                with textual.containers.Horizontal(id="theme-modal-buttons"):
                    yield textual.widgets.Button("Save & Close", variant="success", id="theme-save-btn")
                    yield textual.widgets.Button("Cancel", variant="default", id="theme-cancel-btn")

    def on_mount(self) -> None:
        """Populate the theme list on mount."""
        self.original_theme = self.app.theme or ""
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

    def focus_default(self) -> None:
        """Focus the theme table."""
        self.query_one("#theme-table", textual.widgets.DataTable).focus()

    def apply_theme(self, name: str) -> None:
        """Set the app theme and update markers and preview."""
        self.app.theme = name
        self.refresh_markers(name)
        self.update_preview()

    def on_data_table_row_selected(self, event: textual.widgets.DataTable.RowSelected) -> None:
        """Apply the selected theme."""
        name = str(event.row_key.value) if event.row_key.value else ""
        if name:
            self.apply_theme(name)

    def on_data_table_row_highlighted(self, event: textual.widgets.DataTable.RowHighlighted) -> None:
        """Preview theme on cursor movement."""
        name = str(event.row_key.value) if event.row_key.value else ""
        if name:
            self.apply_theme(name)

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle Save & Close / Cancel in modal mode."""
        btn_id = event.button.id or ""
        if btn_id == "theme-save-btn":
            self.post_message(self.Saved())
        elif btn_id == "theme-cancel-btn":
            self.app.theme = self.original_theme
            self.post_message(self.Cancelled())

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
        table.refresh()

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
