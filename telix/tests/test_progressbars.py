"""Tests for telix.progressbars and telix.gmcp_snapshot modules."""

from __future__ import annotations

# std imports
import os
import json

# 3rd party
import pytest

# local
from telix.progressbars import (
    TRAVEL_BAR_NAME,
    BarConfig,
    bar_color_at,
    load_progressbars,
    save_progressbars,
    detect_progressbars,
    resolve_text_color_hex,
)
from telix.gmcp_snapshot import load_gmcp_snapshot, save_gmcp_snapshot

# -- detect_progressbars --


@pytest.mark.parametrize("hp_key,maxhp_key", [("hp", "maxhp"), ("hp", "maxHP"), ("HP", "maxHP")])
def test_detect_hp_aliases(hp_key, maxhp_key):
    gmcp = {"Char.Vitals": {hp_key: 100, maxhp_key: 200}}
    bars = detect_progressbars(gmcp)
    hp_bars = [b for b in bars if b.name == "HP"]
    assert len(hp_bars) == 1
    assert hp_bars[0].enabled is True
    assert hp_bars[0].value_field == hp_key
    assert hp_bars[0].max_field == maxhp_key


@pytest.mark.parametrize("mp_key,maxmp_key", [("mp", "maxmp"), ("mana", "maxmana"), ("sp", "maxsp")])
def test_detect_mp_aliases(mp_key, maxmp_key):
    gmcp = {"Char.Vitals": {mp_key: 50, maxmp_key: 100}}
    bars = detect_progressbars(gmcp)
    mp_bars = [b for b in bars if b.name == "MP"]
    assert len(mp_bars) == 1
    assert mp_bars[0].enabled is True


def test_detect_xp_from_status():
    gmcp = {"Char.Status": {"xp": 500, "maxxp": 1000}}
    bars = detect_progressbars(gmcp)
    xp_bars = [b for b in bars if b.name == "XP"]
    assert len(xp_bars) == 1
    assert xp_bars[0].gmcp_package == "Char.Status"


def test_detect_max_prefix_pattern():
    gmcp = {"Char.Guild.Stats": {"Adrenaline": 442, "MaxAdrenaline": 442, "Water": 100, "MaxWater": 200}}
    bars = detect_progressbars(gmcp)
    gmcp_bars = [b for b in bars if b.gmcp_package]
    names = {b.name for b in gmcp_bars}
    assert "Adrenaline" in names
    assert "Water" in names
    for b in gmcp_bars:
        assert b.enabled is False


def test_detect_suffix_max_pattern():
    gmcp = {"Custom.Pkg": {"stamina": 80, "staminamax": 100}}
    bars = [b for b in detect_progressbars(gmcp) if b.gmcp_package]
    assert len(bars) == 1
    assert bars[0].name == "stamina"
    assert bars[0].max_field == "staminamax"


def test_detect_case_insensitive():
    gmcp = {"Char.Guild": {"energy": 50, "MAXENERGY": 100}}
    bars = [b for b in detect_progressbars(gmcp) if b.gmcp_package]
    assert len(bars) == 1
    assert bars[0].value_field == "energy"


def test_detect_empty_gmcp():
    assert detect_progressbars({}) == []


def test_detect_no_pairs():
    gmcp = {"Char.Vitals": {"name": "player", "level": 5}}
    bars = [b for b in detect_progressbars(gmcp) if b.gmcp_package]
    assert bars == []


def test_detect_skips_non_numeric_values():
    gmcp = {"Char.StatusVars": {"xp": "Experience", "maxxp": "Max Experience"}}
    bars = [b for b in detect_progressbars(gmcp) if b.gmcp_package]
    assert bars == []


def test_detect_skips_non_numeric_pair():
    gmcp = {"Custom.Pkg": {"Mode": "Rage", "MaxMode": "something"}}
    bars = [b for b in detect_progressbars(gmcp) if b.gmcp_package]
    assert bars == []


def test_detect_combined_hp_mp_and_guild():
    gmcp = {
        "Char.Vitals": {"hp": 100, "maxhp": 200, "mp": 50, "maxmp": 100},
        "Char.Guild.Stats": {"Adrenaline": 10, "MaxAdrenaline": 50},
    }
    bars = detect_progressbars(gmcp)
    names = [b.name for b in bars]
    assert "HP" in names
    assert "MP" in names
    assert "Adrenaline" in names
    assert bars[0].display_order == 0
    assert bars[-1].display_order == len(bars) - 1


def test_detect_no_duplicate_standard_and_pair():
    gmcp = {"Char.Vitals": {"hp": 100, "maxhp": 200}}
    bars = detect_progressbars(gmcp)
    hp_bars = [b for b in bars if b.value_field == "hp"]
    assert len(hp_bars) == 1


# -- load_progressbars / save_progressbars --


def test_round_trip(tmp_path):
    path = str(tmp_path / "pb.json")
    bars = [
        BarConfig("HP", "Char.Vitals", "hp", "maxhp", True, "theme", "green", "red", "shortest", display_order=0),
        BarConfig("MP", "Char.Vitals", "mp", "maxmp", True, "custom", "blue", "gold1", "longest", display_order=1),
    ]
    save_progressbars(path, "mud:1234", bars)
    loaded = load_progressbars(path, "mud:1234")
    assert len(loaded) == 2
    assert loaded[0].name == "HP"
    assert loaded[0].color_mode == "theme"
    assert loaded[1].name == "MP"
    assert loaded[1].color_mode == "custom"
    assert loaded[1].color_path == "longest"


def test_load_missing_file(tmp_path):
    path = str(tmp_path / "nonexistent.json")
    assert load_progressbars(path, "x:1") == []


def test_load_empty_session(tmp_path):
    path = str(tmp_path / "pb.json")
    save_progressbars(path, "a:1", [BarConfig("HP", "V", "hp", "mhp")])
    assert load_progressbars(path, "other:2") == []


def test_save_preserves_other_sessions(tmp_path):
    path = str(tmp_path / "pb.json")
    save_progressbars(path, "a:1", [BarConfig("HP", "V", "hp", "mhp")])
    save_progressbars(path, "b:2", [BarConfig("MP", "V", "mp", "mmp")])
    assert len(load_progressbars(path, "a:1")) == 1
    assert len(load_progressbars(path, "b:2")) == 1


# -- bar_color_at --


def test_bar_color_theme_mode():
    bar = BarConfig("HP", "V", "hp", "mhp", color_mode="theme")
    color = bar_color_at(1.0, bar, theme_accent=(0, 200, 0))
    assert color.startswith("#")
    assert len(color) == 7


def test_bar_color_custom_mode():
    bar = BarConfig("HP", "V", "hp", "mhp", color_mode="custom", color_name_max="green", color_name_min="red")
    c0 = bar_color_at(0.0, bar)
    c1 = bar_color_at(1.0, bar)
    assert c0 != c1


def test_bar_color_edge_fractions():
    bar = BarConfig("X", "P", "v", "m", color_mode="custom", color_name_max="blue", color_name_min="red")
    c0 = bar_color_at(0.0, bar)
    c1 = bar_color_at(1.0, bar)
    assert c0.startswith("#")
    assert c1.startswith("#")


def test_bar_color_clamps():
    bar = BarConfig("X", "P", "v", "m", color_mode="custom", color_name_max="green", color_name_min="red")
    assert bar_color_at(-0.5, bar) == bar_color_at(0.0, bar)
    assert bar_color_at(1.5, bar) == bar_color_at(1.0, bar)


def test_bar_color_longest_path():
    bar_short = BarConfig(
        "X", "P", "v", "m", color_mode="custom", color_name_max="green", color_name_min="red", color_path="shortest"
    )
    bar_long = BarConfig(
        "X", "P", "v", "m", color_mode="custom", color_name_max="green", color_name_min="red", color_path="longest"
    )
    mid_short = bar_color_at(0.5, bar_short)
    mid_long = bar_color_at(0.5, bar_long)
    assert mid_short != mid_long


def test_bar_color_theme_none_accent():
    bar = BarConfig("HP", "V", "hp", "mhp", color_mode="theme")
    color = bar_color_at(0.5, bar, theme_accent=None)
    assert color.startswith("#")


# -- gmcp_snapshot --


def test_save_and_load_snapshot(tmp_path):
    path = str(tmp_path / "snap.json")
    gmcp = {"Char.Vitals": {"hp": 100, "maxhp": 200}}
    save_gmcp_snapshot(path, "mud:1234", gmcp)
    pkgs = load_gmcp_snapshot(path)
    assert "Char.Vitals" in pkgs
    assert pkgs["Char.Vitals"]["data"]["hp"] == 100
    assert "last_updated" in pkgs["Char.Vitals"]


def test_snapshot_merge(tmp_path):
    path = str(tmp_path / "snap.json")
    save_gmcp_snapshot(path, "mud:1234", {"Char.Vitals": {"hp": 100}})
    save_gmcp_snapshot(path, "mud:1234", {"Char.Guild": {"xp": 50}})
    pkgs = load_gmcp_snapshot(path)
    assert "Char.Vitals" in pkgs
    assert "Char.Guild" in pkgs


def test_snapshot_overwrites_package(tmp_path):
    path = str(tmp_path / "snap.json")
    save_gmcp_snapshot(path, "m:1", {"Char.Vitals": {"hp": 100}})
    save_gmcp_snapshot(path, "m:1", {"Char.Vitals": {"hp": 200}})
    pkgs = load_gmcp_snapshot(path)
    assert pkgs["Char.Vitals"]["data"]["hp"] == 200


def test_load_missing_snapshot(tmp_path):
    path = str(tmp_path / "missing.json")
    assert load_gmcp_snapshot(path) == {}


def test_save_empty_gmcp_noop(tmp_path):
    path = str(tmp_path / "snap.json")
    save_gmcp_snapshot(path, "m:1", {})
    assert not os.path.exists(path)


def test_snapshot_has_session_key(tmp_path):
    path = str(tmp_path / "snap.json")
    save_gmcp_snapshot(path, "mud:4000", {"X": {"a": 1}})
    with open(path) as f:
        raw = json.load(f)
    assert raw["session_key"] == "mud:4000"
    assert "last_updated" in raw


# -- text color fields --


def test_text_color_default_auto():
    bar = BarConfig("HP", "V", "hp", "mhp")
    assert bar.text_color_fill == "auto"
    assert bar.text_color_empty == "auto"


def test_text_color_json_roundtrip(tmp_path):
    path = str(tmp_path / "pb.json")
    bars = [BarConfig("HP", "V", "hp", "mhp", text_color_fill="red", text_color_empty="blue")]
    save_progressbars(path, "m:1", bars)
    loaded = load_progressbars(path, "m:1")
    assert loaded[0].text_color_fill == "red"
    assert loaded[0].text_color_empty == "blue"


def test_auto_omitted_from_json(tmp_path):
    path = str(tmp_path / "pb.json")
    bars = [BarConfig("HP", "V", "hp", "mhp")]
    save_progressbars(path, "m:1", bars)
    with open(path) as f:
        raw = json.load(f)
    entry = raw["m:1"]["bars"][0]
    assert "text_color_fill" not in entry
    assert "text_color_empty" not in entry


def test_resolve_text_color_hex_auto():
    assert resolve_text_color_hex("auto") is None


def test_resolve_text_color_hex_named():
    result = resolve_text_color_hex("red")
    assert result is not None
    assert result.startswith("#")
    assert len(result) == 7


# -- side field --


def test_side_field_default():
    bar = BarConfig("HP", "V", "hp", "mhp")
    assert bar.side == "left"


def test_side_field_round_trip(tmp_path):
    path = str(tmp_path / "pb.json")
    bars = [BarConfig("HP", "V", "hp", "mhp", side="right")]
    save_progressbars(path, "m:1", bars)
    loaded = load_progressbars(path, "m:1")
    assert loaded[0].side == "right"


def test_side_left_omitted_from_json(tmp_path):
    path = str(tmp_path / "pb.json")
    bars = [BarConfig("HP", "V", "hp", "mhp")]
    save_progressbars(path, "m:1", bars)
    with open(path) as f:
        raw = json.load(f)
    entry = raw["m:1"]["bars"][0]
    assert "side" not in entry


def test_load_json_without_side_defaults_left(tmp_path):
    path = str(tmp_path / "pb.json")
    data = {"m:1": {"bars": [{"name": "HP", "gmcp_package": "V", "value_field": "hp", "max_field": "mhp"}]}}
    with open(path, "w") as f:
        json.dump(data, f)
    loaded = load_progressbars(path, "m:1")
    assert loaded[0].side == "left"


# -- Travel bar in detect --


def test_detect_includes_travel_bar():
    gmcp = {"Char.Vitals": {"hp": 100, "maxhp": 200}}
    bars = detect_progressbars(gmcp)
    travel = [b for b in bars if b.name == TRAVEL_BAR_NAME]
    assert len(travel) == 1
    assert travel[0].gmcp_package == ""
    assert travel[0].side == "right"
    assert travel[0].enabled is True
    assert travel[0].name == "<Travel>"


def test_detect_travel_bar_first():
    gmcp = {"Char.Vitals": {"hp": 100, "maxhp": 200}}
    bars = detect_progressbars(gmcp)
    assert bars[0].name == TRAVEL_BAR_NAME
    assert bars[0].display_order == 0
