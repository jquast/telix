"""Tests for :mod:`telix.repl_theme`."""

from __future__ import annotations

# 3rd party
import pytest

# local
from telix.repl_theme import FALLBACK, TOKEN_MAP, hex_to_rgb, get_repl_palette, invalidate_cache


@pytest.fixture(autouse=True)
def clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


class TestGetReplPalette:
    def test_returns_all_semantic_keys(self, monkeypatch):
        monkeypatch.setattr("telix.repl_theme.saved_theme_name", lambda sk: "textual-dark")
        palette = get_repl_palette()
        for key in TOKEN_MAP:
            assert key in palette

    def test_values_are_hex_strings(self, monkeypatch):
        monkeypatch.setattr("telix.repl_theme.saved_theme_name", lambda sk: "textual-dark")
        palette = get_repl_palette()
        for key, val in palette.items():
            assert val.startswith("#"), f"{key}: {val}"
            assert len(val) == 7, f"{key}: {val}"

    def test_cache_hit(self, monkeypatch):
        monkeypatch.setattr("telix.repl_theme.saved_theme_name", lambda sk: "textual-dark")
        p1 = get_repl_palette()
        p2 = get_repl_palette()
        assert p1 is p2

    def test_invalidate_cache_clears(self, monkeypatch):
        monkeypatch.setattr("telix.repl_theme.saved_theme_name", lambda sk: "textual-dark")
        p1 = get_repl_palette()
        invalidate_cache()
        p2 = get_repl_palette()
        assert p1 is not p2

    def test_fallback_on_unknown_theme(self, monkeypatch):
        monkeypatch.setattr("telix.repl_theme.saved_theme_name", lambda sk: "")
        palette = get_repl_palette()
        assert "foreground" in palette
        assert "background" in palette

    def test_fallback_values_used_when_resolution_fails(self, monkeypatch):
        monkeypatch.setattr("telix.repl_theme.saved_theme_name", lambda sk: "nonexistent")
        monkeypatch.setattr("telix.repl_theme.resolve_theme", lambda name: {})
        palette = get_repl_palette()
        computed_keys = {"active_cmd", "pending_cmd", "input_ar_suggestion"}
        for key, value in FALLBACK.items():
            if key not in computed_keys:
                assert palette[key] == value
        assert "active_cmd" in palette
        assert "pending_cmd" in palette
        assert "input_ar_suggestion" in palette

    def test_session_key_forwarded(self, monkeypatch):
        captured = []
        monkeypatch.setattr("telix.repl_theme.saved_theme_name", lambda sk: (captured.append(sk), "textual-dark")[1])
        get_repl_palette("myhost:4000")
        assert captured == ["myhost:4000"]


class TestHexToRgb:
    @pytest.mark.parametrize(
        "hex_val,expected",
        [("#000000", (0, 0, 0)), ("#ffffff", (255, 255, 255)), ("#1a0000", (26, 0, 0)), ("#ff00ff", (255, 0, 255))],
    )
    def test_conversion(self, hex_val, expected):
        assert hex_to_rgb(hex_val) == expected
