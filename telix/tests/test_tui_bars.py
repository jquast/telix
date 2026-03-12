"""Tests for telix.client_tui_bars data logic."""

from __future__ import annotations

import json
import os

import pytest
import rich.text

from telix import client_tui_bars, progressbars


class TestProgressBarTupleDefaults:
    """ProgressBarTuple default field values."""

    def test_defaults(self):
        t = client_tui_bars.ProgressBarTuple()
        assert t.name == ""
        assert t.gmcp_package == ""
        assert t.value_field == ""
        assert t.max_field == ""
        assert t.enabled is True
        assert t.color_mode == "theme"
        assert t.color_name_max == "success"
        assert t.color_name_min == "error"
        assert t.color_path == "shortest"
        assert t.text_color_fill == "auto"
        assert t.text_color_empty == "auto"
        assert t.display_order == 0
        assert t.side == "left"
        assert t.bar_type == "bar"
        assert t.label_format == "{value}"


class TestProgressBarTupleAccess:
    """ProgressBarTuple field access with custom values."""

    def test_field_access(self):
        t = client_tui_bars.ProgressBarTuple(
            name="HP",
            gmcp_package="Char.Vitals",
            value_field="hp",
            max_field="maxhp",
            enabled=False,
            color_mode="custom",
            color_name_max="green",
            color_name_min="red",
            color_path="longest",
            text_color_fill="white",
            text_color_empty="black",
            display_order=3,
            side="right",
            bar_type="label",
            label_format="{value}/{max}",
        )
        assert t.name == "HP"
        assert t.gmcp_package == "Char.Vitals"
        assert t.value_field == "hp"
        assert t.max_field == "maxhp"
        assert t.enabled is False
        assert t.color_mode == "custom"
        assert t.color_name_max == "green"
        assert t.color_name_min == "red"
        assert t.color_path == "longest"
        assert t.text_color_fill == "white"
        assert t.text_color_empty == "black"
        assert t.display_order == 3
        assert t.side == "right"
        assert t.bar_type == "label"
        assert t.label_format == "{value}/{max}"


class TestLoadGmcpPackages:
    """ProgressBarEditPane.load_gmcp_packages from snapshot file."""

    def test_load_from_file(self, tmp_path):
        snapshot = {
            "packages": {
                "Char.Vitals": {"data": {"hp": 100, "maxhp": 200}},
                "Char.Status": {"data": {"level": 5}},
            }
        }
        path = str(tmp_path / "gmcp.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
        pane = client_tui_bars.ProgressBarEditPane(path="unused", gmcp_snapshot_path=path)
        pane.load_gmcp_packages()
        assert pane.gmcp_packages == ["Char.Status", "Char.Vitals"]
        assert pane.gmcp_fields == {"Char.Vitals": ["hp", "maxhp"], "Char.Status": ["level"]}

    def test_empty_path(self):
        pane = client_tui_bars.ProgressBarEditPane(path="unused", gmcp_snapshot_path="")
        pane.load_gmcp_packages()
        assert pane.gmcp_packages == []
        assert pane.gmcp_fields == {}

    def test_missing_file(self, tmp_path):
        pane = client_tui_bars.ProgressBarEditPane(
            path="unused", gmcp_snapshot_path=str(tmp_path / "nonexistent.json")
        )
        pane.load_gmcp_packages()
        assert pane.gmcp_packages == []


class TestMatchesSearch:
    """ProgressBarEditPane.matches_search filtering."""

    def test_match_name(self):
        pane = client_tui_bars.ProgressBarEditPane(path="unused")
        pane.bars = [client_tui_bars.ProgressBarTuple(name="Health Bar", gmcp_package="Char.Vitals")]
        assert pane.matches_search(0, "health")

    def test_match_gmcp_package(self):
        pane = client_tui_bars.ProgressBarEditPane(path="unused")
        pane.bars = [client_tui_bars.ProgressBarTuple(name="HP", gmcp_package="Char.Vitals")]
        assert pane.matches_search(0, "vitals")

    def test_no_match(self):
        pane = client_tui_bars.ProgressBarEditPane(path="unused")
        pane.bars = [client_tui_bars.ProgressBarTuple(name="HP", gmcp_package="Char.Vitals")]
        assert not pane.matches_search(0, "mana")


class TestColorOptions:
    """ProgressBarEditPane.color_options static method."""

    def test_returns_list_of_tuples(self):
        opts = client_tui_bars.ProgressBarEditPane.color_options()
        assert isinstance(opts, list)
        assert len(opts) > 0
        for label, value in opts:
            assert isinstance(label, rich.text.Text)
            assert isinstance(value, str)


class TestThemeColorOptions:
    """ProgressBarEditPane.theme_color_options static method."""

    def test_returns_nonempty(self):
        opts = client_tui_bars.ProgressBarEditPane.theme_color_options()
        assert len(opts) > 0
        for label, value in opts:
            assert isinstance(label, rich.text.Text)
            assert isinstance(value, str)


class TestTextColorOptions:
    """ProgressBarEditPane.text_color_options static method."""

    @pytest.mark.parametrize("is_custom", [True, False])
    def test_first_item_is_auto(self, is_custom):
        opts = client_tui_bars.ProgressBarEditPane.text_color_options(is_custom)
        assert len(opts) > 0
        label, value = opts[0]
        assert value == "auto"


class TestEaseInOut:
    """ProgressBarEditPane.ease_in_out static method."""

    @pytest.mark.parametrize("t,expected", [
        (0.0, 0.0),
        (1.0, 1.0),
        (0.5, 0.5),
    ])
    def test_known_values(self, t, expected):
        assert client_tui_bars.ProgressBarEditPane.ease_in_out(t) == pytest.approx(expected)

    def test_quarter(self):
        result = client_tui_bars.ProgressBarEditPane.ease_in_out(0.25)
        assert 0.0 < result < 0.5


class TestColorSwatch:
    """ProgressBarEditPane.color_swatch static method."""

    def test_returns_rich_text(self):
        bar = client_tui_bars.ProgressBarTuple(name="HP", gmcp_package="Char.Vitals")
        swatch = client_tui_bars.ProgressBarEditPane.color_swatch(bar)
        assert isinstance(swatch, rich.text.Text)

    def test_has_swatch_steps_segments(self):
        bar = client_tui_bars.ProgressBarTuple(name="HP", gmcp_package="Char.Vitals")
        swatch = client_tui_bars.ProgressBarEditPane.color_swatch(bar)
        assert len(swatch._spans) == client_tui_bars.ProgressBarEditPane.SWATCH_STEPS
