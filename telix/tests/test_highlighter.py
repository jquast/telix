"""Tests for the highlighter module."""

from __future__ import annotations

# std imports
import os
import re
import json
import tempfile
from unittest.mock import MagicMock, patch

# 3rd party
import pytest

# local
from telix.highlighter import (
    RE_FLAGS,
    HighlightRule,
    CompiledRuleSet,
    HighlightEngine,
    load_highlights,
    save_highlights,
    validate_highlight,
)


def make_rule(
    pattern: str,
    highlight: str = "bold_red",
    enabled: bool = True,
    stop_movement: bool = False,
    builtin: bool = False,
) -> HighlightRule:
    return HighlightRule(
        pattern=re.compile(pattern, RE_FLAGS),
        highlight=highlight,
        enabled=enabled,
        stop_movement=stop_movement,
        builtin=builtin,
    )


class FormattingString(str):
    """Mimics blessed.FormattingString -- a str subclass that is also callable."""

    def __call__(self, text: str = "") -> str:
        return f"{self}{text}\x1b[0m"


class MockTerminal:
    """Minimal blessed.Terminal stand-in for highlight tests."""

    STYLES = {
        "bold_red": FormattingString("\x1b[1;31m"),
        "blink_black_on_yellow": FormattingString("\x1b[5;30;43m"),
        "black_on_beige": FormattingString("\x1b[30;43m"),
        "cyan": FormattingString("\x1b[36m"),
        "normal": FormattingString("\x1b[0m"),
    }

    def __getattr__(self, name: str) -> FormattingString:
        if name in self.STYLES:
            return self.STYLES[name]
        raise AttributeError(name)


def mock_term():
    return MockTerminal()


class TestHighlightRuleLoadSave:
    """Load/save roundtrip for highlight rules."""

    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "highlights.json")
        rules = [
            make_rule("dynamite", "blink_black_on_yellow"),
            make_rule("danger", "bold_red", stop_movement=True),
        ]
        save_highlights(path, rules, "test:23")
        loaded = load_highlights(path, "test:23")
        assert len(loaded) == 2
        assert loaded[0].pattern.pattern == "dynamite"
        assert loaded[0].highlight == "blink_black_on_yellow"
        assert loaded[0].stop_movement is False
        assert loaded[1].stop_movement is True

    def test_empty_session(self, tmp_path):
        path = str(tmp_path / "highlights.json")
        save_highlights(path, [], "test:23")
        loaded = load_highlights(path, "other:99")
        assert loaded == []

    def test_invalid_regex(self, tmp_path):
        path = str(tmp_path / "highlights.json")
        data = {"test:23": {"highlights": [{"pattern": "[invalid", "highlight": "bold_red"}]}}
        with open(path, "w") as fh:
            json.dump(data, fh)
        with pytest.raises(ValueError, match="Invalid highlight pattern"):
            load_highlights(path, "test:23")

    def test_preserves_other_sessions(self, tmp_path):
        path = str(tmp_path / "highlights.json")
        data = {"other:99": {"highlights": [{"pattern": "foo", "highlight": "bold_red"}]}}
        with open(path, "w") as fh:
            json.dump(data, fh)
        save_highlights(path, [make_rule("bar")], "test:23")
        with open(path) as fh:
            saved = json.load(fh)
        assert "other:99" in saved
        assert "test:23" in saved

    def test_builtin_flag_roundtrip(self, tmp_path):
        path = str(tmp_path / "highlights.json")
        rules = [make_rule("autoreply", "black_on_beige", builtin=True)]
        save_highlights(path, rules, "test:23")
        loaded = load_highlights(path, "test:23")
        assert loaded[0].builtin is True


class TestValidateHighlight:

    def test_valid_compoundable(self):
        term = mock_term()
        assert validate_highlight(term, "bold_red") is True

    def test_invalid_compoundable(self):
        term = mock_term()
        assert validate_highlight(term, "nonexistent_style") is False


class TestCompiledRuleSet:

    def test_combines_enabled_autoreply_rules(self):
        from telix.autoreply import AutoreplyRule

        ar_rules = [
            AutoreplyRule(pattern=re.compile("foo", RE_FLAGS), reply="bar", enabled=True),
            AutoreplyRule(pattern=re.compile("baz", RE_FLAGS), reply="qux", enabled=False),
            AutoreplyRule(pattern=re.compile("quux", RE_FLAGS), reply="x", enabled=True),
        ]
        rs = CompiledRuleSet([], ar_rules, "black_on_beige", True)
        spans = rs.finditer("foo and quux but not baz")
        highlights = [(s, e, hl) for s, e, hl, *_ in spans]
        assert ("foo", "black_on_beige") in [
            ("foo and quux but not baz"[s:e], hl) for s, e, hl in highlights
        ]
        assert ("quux", "black_on_beige") in [
            ("foo and quux but not baz"[s:e], hl) for s, e, hl in highlights
        ]
        matched_texts = {"foo and quux but not baz"[s:e] for s, e, hl in highlights}
        assert "baz" not in matched_texts

    def test_empty_rules(self):
        rs = CompiledRuleSet([], [], "black_on_beige", True)
        assert rs.finditer("anything") == []

    def test_all_disabled(self):
        from telix.autoreply import AutoreplyRule

        ar_rules = [AutoreplyRule(pattern=re.compile("foo", RE_FLAGS), reply="bar", enabled=False)]
        rs = CompiledRuleSet([], ar_rules, "black_on_beige", True)
        assert rs.finditer("foo") == []

    def test_combines_highlight_and_autoreply(self):
        from telix.autoreply import AutoreplyRule

        ar_rules = [
            AutoreplyRule(pattern=re.compile("monster", RE_FLAGS), reply="flee", enabled=True)
        ]
        hl_rules = [make_rule("dynamite", "bold_red")]
        rs = CompiledRuleSet(hl_rules, ar_rules, "black_on_beige", True)
        spans = rs.finditer("a monster has dynamite")
        assert len(spans) == 2
        assert spans[0][2] == "black_on_beige"
        assert spans[1][2] == "bold_red"

    def test_overlap_first_wins(self):
        hl_rules = [make_rule("abc", "bold_red"), make_rule("bc", "black_on_beige")]
        rs = CompiledRuleSet(hl_rules, [], "black_on_beige", True)
        spans = rs.finditer("xabcx")
        assert len(spans) == 1
        assert spans[0][2] == "bold_red"


class TestHighlightEngineProcessLine:

    def test_no_match_passthrough(self):
        engine = HighlightEngine([make_rule("dynamite")], [], mock_term())
        line = "nothing interesting here"
        result, matched = engine.process_line(line)
        assert result == line
        assert matched is False

    def test_simple_match(self):
        engine = HighlightEngine([make_rule("danger", "bold_red")], [], mock_term())
        line = "there is danger ahead"
        result, matched = engine.process_line(line)
        assert matched is True
        assert "danger" in result
        assert "\x1b[1;31m" in result
        assert "\x1b[0m" in result

    def test_case_insensitive(self):
        engine = HighlightEngine([make_rule("DANGER", "bold_red")], [], mock_term())
        line = "there is danger ahead"
        result, matched = engine.process_line(line)
        assert matched is True
        assert "\x1b[1;31m" in result

    def test_preserves_existing_sgr(self):
        engine = HighlightEngine([make_rule("def", "bold_red")], [], mock_term())
        line = "\x1b[36mabc def ghi\x1b[0m"
        result, matched = engine.process_line(line)
        assert matched is True
        assert "\x1b[36m" in result
        assert "\x1b[1;31m" in result

    def test_multiple_matches(self):
        engine = HighlightEngine([make_rule("cat", "bold_red")], [], mock_term())
        line = "the cat sat on the cat"
        result, matched = engine.process_line(line)
        assert matched is True
        assert result.count("\x1b[1;31m") == 2

    def test_disabled_engine(self):
        engine = HighlightEngine([make_rule("danger")], [], mock_term())
        engine.enabled = False
        line = "there is danger ahead"
        result, matched = engine.process_line(line)
        assert result == line
        assert matched is False

    def test_disabled_rule(self):
        engine = HighlightEngine([make_rule("danger", enabled=False)], [], mock_term())
        line = "there is danger ahead"
        result, matched = engine.process_line(line)
        assert result == line
        assert matched is False

    def test_empty_line(self):
        engine = HighlightEngine([make_rule("danger")], [], mock_term())
        result, matched = engine.process_line("")
        assert result == ""
        assert matched is False

    def test_sequence_only_line(self):
        engine = HighlightEngine([make_rule("danger")], [], mock_term())
        result, matched = engine.process_line("\x1b[0m")
        assert matched is False


class TestHighlightEngineStopMovement:

    def test_cancels_discover(self):
        ctx = MagicMock()
        ctx.discover_active = True
        ctx.discover_task = MagicMock()
        ctx.randomwalk_active = False
        engine = HighlightEngine(
            [make_rule("danger", stop_movement=True)], [], mock_term(), ctx=ctx
        )
        result, _ = engine.process_line("there is danger ahead")
        ctx.discover_task.cancel.assert_called_once()
        assert ctx.discover_active is False
        assert "[stop: discover cancelled]" in result

    def test_cancels_randomwalk(self):
        ctx = MagicMock()
        ctx.discover_active = False
        ctx.randomwalk_active = True
        ctx.randomwalk_task = MagicMock()
        engine = HighlightEngine(
            [make_rule("danger", stop_movement=True)], [], mock_term(), ctx=ctx
        )
        result, _ = engine.process_line("there is danger ahead")
        ctx.randomwalk_task.cancel.assert_called_once()
        assert ctx.randomwalk_active is False
        assert "[stop: random walk cancelled]" in result

    def test_no_stop_without_flag(self):
        ctx = MagicMock()
        ctx.discover_active = True
        ctx.discover_task = MagicMock()
        engine = HighlightEngine(
            [make_rule("danger", stop_movement=False)], [], mock_term(), ctx=ctx
        )
        engine.process_line("there is danger ahead")
        ctx.discover_task.cancel.assert_not_called()


class TestHighlightEngineAutoreplyBuiltin:

    def test_builtin_autoreply_highlight(self):
        from telix.autoreply import AutoreplyRule

        ar_rules = [
            AutoreplyRule(pattern=re.compile("monster", RE_FLAGS), reply="flee", enabled=True)
        ]
        engine = HighlightEngine([], ar_rules, mock_term(), autoreply_highlight="black_on_beige")
        line = "A monster appears!"
        result, matched = engine.process_line(line)
        assert matched is True
        assert "\x1b[30;43m" in result

    def test_builtin_disabled(self):
        from telix.autoreply import AutoreplyRule

        ar_rules = [
            AutoreplyRule(pattern=re.compile("monster", RE_FLAGS), reply="flee", enabled=True)
        ]
        engine = HighlightEngine([], ar_rules, mock_term(), autoreply_enabled=False)
        line = "A monster appears!"
        result, matched = engine.process_line(line)
        assert matched is False


class TestAutoreplyCaseInsensitive:

    def test_case_insensitive_matching(self):
        from telix.autoreply import parse_entries

        entries = [{"pattern": "DANGER", "reply": "flee"}]
        rules = parse_entries(entries)
        assert rules[0].pattern.search("there is danger ahead") is not None
        assert rules[0].pattern.search("DANGER") is not None
        assert rules[0].pattern.search("Danger Zone") is not None


def make_capture_rule(
    pattern: str,
    highlight: str = "bold_red",
    captured: bool = True,
    capture_name: str = "captures",
    captures: list[dict[str, str]] | None = None,
) -> HighlightRule:
    return HighlightRule(
        pattern=re.compile(pattern, RE_FLAGS),
        highlight=highlight,
        captured=captured,
        capture_name=capture_name,
        captures=captures or [],
    )


class TestMultilineHighlight:

    def test_multiline_default_false(self):
        rule = make_rule("foo")
        assert rule.multiline is False

    def test_process_block_no_ml_rules(self):
        engine = HighlightEngine([make_rule("foo")], [], mock_term())
        text = "line one\nline two\n"
        result, matched = engine.process_block(text)
        assert result == text
        assert matched is False

    def test_process_block_matches(self):
        rule = HighlightRule(
            pattern=re.compile(r"echoes:\n.*hijacked", RE_FLAGS),
            highlight="bold_red",
            multiline=True,
        )
        engine = HighlightEngine([rule], [], mock_term())
        text = "The hearer echoes:\nLytol hijacked the spire\n"
        result, matched = engine.process_block(text)
        assert matched is True
        assert "\x1b[1;31m" in result

    def test_process_block_cr_normalization(self):
        rule = HighlightRule(
            pattern=re.compile(r"echoes:\n.*hijacked", RE_FLAGS),
            highlight="bold_red",
            multiline=True,
        )
        engine = HighlightEngine([rule], [], mock_term())
        text = "The hearer echoes:\r\nLytol hijacked the spire\n"
        result, matched = engine.process_block(text)
        assert matched is True
        assert "\x1b[1;31m" in result

    def test_singleline_unaffected(self):
        sl_rule = make_rule("danger", "bold_red")
        ml_rule = HighlightRule(
            pattern=re.compile(r"echoes:\n.*hijacked", RE_FLAGS),
            highlight="bold_red",
            multiline=True,
        )
        engine = HighlightEngine([sl_rule, ml_rule], [], mock_term())
        result, matched = engine.process_line("there is danger ahead")
        assert matched is True
        assert "\x1b[1;31m" in result

    def test_ml_not_in_process_line(self):
        rule = HighlightRule(
            pattern=re.compile(r"echoes:\n.*hijacked", RE_FLAGS),
            highlight="bold_red",
            multiline=True,
        )
        engine = HighlightEngine([rule], [], mock_term())
        result, matched = engine.process_line("The hearer echoes:")
        assert matched is False

    def test_json_roundtrip(self, tmp_path):
        path = str(tmp_path / "highlights.json")
        rules = [
            HighlightRule(
                pattern=re.compile(r"echoes:\n.*hijacked", RE_FLAGS),
                highlight="bold_red",
                multiline=True,
            )
        ]
        save_highlights(path, rules, "test:23")
        loaded = load_highlights(path, "test:23")
        assert len(loaded) == 1
        assert loaded[0].multiline is True

    def test_false_omitted_from_json(self, tmp_path):
        path = str(tmp_path / "highlights.json")
        rules = [make_rule("danger")]
        save_highlights(path, rules, "test:23")
        with open(path) as fh:
            data = json.load(fh)
        entry = data["test:23"]["highlights"][0]
        assert "multiline" not in entry

    def test_multiline_capture(self):
        rule = HighlightRule(
            pattern=re.compile(r"echoes:\n(\w+) hijacked", RE_FLAGS),
            highlight="bold_red",
            multiline=True,
            captured=True,
            capture_name="captures",
        )
        ctx = MagicMock()
        ctx.captures = {}
        ctx.capture_log = {}
        ctx.discover_active = False
        ctx.randomwalk_active = False
        engine = HighlightEngine([rule], [], mock_term(), ctx=ctx)
        text = "The hearer echoes:\nLytol hijacked the spire\n"
        result, matched = engine.process_block(text)
        assert matched is True
        assert "captures" in ctx.capture_log
        assert len(ctx.capture_log["captures"]) == 1


class TestHighlightCaptures:

    def test_capture_basic(self):
        rule = make_capture_rule(
            r"Adrenaline: (\d+)/(\d+)",
            captures=[
                {"key": "Adrenaline", "value": r"\1"},
                {"key": "MaxAdrenaline", "value": r"\2"},
            ],
        )
        ctx = MagicMock()
        ctx.captures = {}
        ctx.capture_log = {}
        ctx.discover_active = False
        ctx.randomwalk_active = False
        engine = HighlightEngine([rule], [], mock_term(), ctx=ctx)
        engine.process_line("Adrenaline: 442/500")
        assert ctx.captures["Adrenaline"] == 442
        assert ctx.captures["MaxAdrenaline"] == 500

    def test_capture_disabled(self):
        rule = make_capture_rule(
            r"Adrenaline: (\d+)/(\d+)",
            captured=False,
            captures=[{"key": "Adrenaline", "value": r"\1"}],
        )
        ctx = MagicMock()
        ctx.captures = {}
        ctx.capture_log = {}
        ctx.discover_active = False
        ctx.randomwalk_active = False
        engine = HighlightEngine([rule], [], mock_term(), ctx=ctx)
        engine.process_line("Adrenaline: 442/500")
        assert ctx.captures == {}

    def test_capture_non_integer_skipped(self):
        rule = make_capture_rule(r"Name: (\w+)", captures=[{"key": "Name", "value": r"\1"}])
        ctx = MagicMock()
        ctx.captures = {}
        ctx.capture_log = {}
        ctx.discover_active = False
        ctx.randomwalk_active = False
        engine = HighlightEngine([rule], [], mock_term(), ctx=ctx)
        engine.process_line("Name: Bob")
        assert "Name" not in ctx.captures

    def test_capture_line_logged(self):
        rule = make_capture_rule(r"tells you", capture_name="tells")
        ctx = MagicMock()
        ctx.captures = {}
        ctx.capture_log = {}
        ctx.discover_active = False
        ctx.randomwalk_active = False
        engine = HighlightEngine([rule], [], mock_term(), ctx=ctx)
        engine.process_line("Bob tells you: hello")
        assert "tells" in ctx.capture_log
        assert len(ctx.capture_log["tells"]) == 1
        assert ctx.capture_log["tells"][0]["line"] == "Bob tells you: hello"

    def test_capture_json_roundtrip(self, tmp_path):
        path = str(tmp_path / "highlights.json")
        rules = [
            make_capture_rule(
                r"HP: (\d+)/(\d+)",
                captured=True,
                capture_name="vitals",
                captures=[{"key": "HP", "value": r"\1"}, {"key": "MaxHP", "value": r"\2"}],
            )
        ]
        save_highlights(path, rules, "test:23")
        loaded = load_highlights(path, "test:23")
        assert len(loaded) == 1
        assert loaded[0].captured is True
        assert loaded[0].capture_name == "vitals"
        assert loaded[0].captures == [
            {"key": "HP", "value": r"\1"},
            {"key": "MaxHP", "value": r"\2"},
        ]

    def test_capture_custom_channel(self):
        rule = make_capture_rule(r"(\w+) tells you: (.+)", capture_name="tells")
        ctx = MagicMock()
        ctx.captures = {}
        ctx.capture_log = {}
        ctx.discover_active = False
        ctx.randomwalk_active = False
        engine = HighlightEngine([rule], [], mock_term(), ctx=ctx)
        engine.process_line("Bob tells you: hello there")
        engine.process_line("Alice tells you: hi")
        assert len(ctx.capture_log["tells"]) == 2
        assert "captures" not in ctx.capture_log

    def test_capture_dynamic_channel_name(self):
        rule = make_capture_rule(r"(\w+) replies: (.*)$", capture_name=r"\1")
        ctx = MagicMock()
        ctx.captures = {}
        ctx.capture_log = {}
        ctx.discover_active = False
        ctx.randomwalk_active = False
        engine = HighlightEngine([rule], [], mock_term(), ctx=ctx)
        engine.process_line("Bob replies: hello there")
        engine.process_line("Alice replies: hi")
        assert "Bob" in ctx.capture_log
        assert "Alice" in ctx.capture_log
        assert len(ctx.capture_log["Bob"]) == 1
        assert len(ctx.capture_log["Alice"]) == 1
