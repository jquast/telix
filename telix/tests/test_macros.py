"""Tests for telix.macros module."""

from __future__ import annotations

# std imports
import json
import logging

# 3rd party
import pytest

# local
from telix.macros import Macro, load_macros, save_macros, build_macro_dispatch

SK = "test.host:23"


def test_load_macros_valid(tmp_path):
    fp = tmp_path / "macros.json"
    fp.write_text(
        json.dumps(
            {
                SK: {
                    "macros": [
                        {"key": "KEY_F5", "text": "look;"},
                        {"key": "KEY_ALT_N", "text": "north;"},
                    ]
                }
            }
        )
    )
    macros = load_macros(str(fp), SK)
    assert len(macros) == 2
    assert macros[0].key == "KEY_F5"
    assert macros[0].text == "look;"
    assert macros[1].key == "KEY_ALT_N"


def test_load_macros_missing_file():
    with pytest.raises(FileNotFoundError):
        load_macros("/nonexistent/path.json", SK)


def test_load_macros_empty_key_skipped(tmp_path):
    fp = tmp_path / "macros.json"
    fp.write_text(
        json.dumps(
            {SK: {"macros": [{"key": "", "text": "skip"}, {"key": "KEY_F6", "text": "keep;"}]}}
        )
    )
    macros = load_macros(str(fp), SK)
    assert len(macros) == 1
    assert macros[0].key == "KEY_F6"


def test_load_macros_empty_list(tmp_path):
    fp = tmp_path / "macros.json"
    fp.write_text(json.dumps({SK: {"macros": []}}))
    assert not load_macros(str(fp), SK)


def test_load_macros_no_session(tmp_path):
    fp = tmp_path / "macros.json"
    fp.write_text(json.dumps({"other.host:23": {"macros": [{"key": "KEY_F5", "text": "x"}]}}))
    assert not load_macros(str(fp), SK)


def test_save_macros_roundtrip(tmp_path):
    fp = tmp_path / "macros.json"
    original = [Macro(key="KEY_F5", text="look;"), Macro(key="KEY_ALT_N", text="north;")]
    save_macros(str(fp), original, SK)
    loaded = load_macros(str(fp), SK)
    assert len(loaded) == len(original)
    for orig, restored in zip(original, loaded, strict=False):
        assert orig.key == restored.key
        assert orig.text == restored.text


def test_save_macros_preserves_other_sessions(tmp_path):
    fp = tmp_path / "macros.json"
    save_macros(str(fp), [Macro(key="KEY_F1", text="a;")], "host1:23")
    save_macros(str(fp), [Macro(key="KEY_F2", text="b;")], "host2:23")
    assert len(load_macros(str(fp), "host1:23")) == 1
    assert len(load_macros(str(fp), "host2:23")) == 1


def test_save_macros_empty(tmp_path):
    fp = tmp_path / "macros.json"
    save_macros(str(fp), [], SK)
    assert not load_macros(str(fp), SK)


def test_save_macros_unicode(tmp_path):
    fp = tmp_path / "macros.json"
    macros = [Macro(key="KEY_F1", text="say héllo;")]
    save_macros(str(fp), macros, SK)
    loaded = load_macros(str(fp), SK)
    assert loaded[0].text == "say héllo;"


def test_build_dispatch_skips_editor_keymap_conflicts(caplog):
    pytest.importorskip("blessed")
    import types

    from telix.macros import build_macro_dispatch

    writer = types.SimpleNamespace(log=logging.getLogger("test"))
    macros = [
        Macro(key="KEY_LEFT", text="should be skipped"),
        Macro(key="KEY_ALT_E", text="should be kept"),
    ]
    with caplog.at_level(logging.WARNING):
        result = build_macro_dispatch(macros, writer, writer.log)
    assert "KEY_LEFT" not in result
    assert "KEY_ALT_E" in result
    assert "conflicts with editor keymap" in caplog.text


def test_toggle_macro_roundtrip(tmp_path):
    fp = tmp_path / "macros.json"
    original = [Macro(key="KEY_F5", text="survey on", toggle=True, toggle_text="survey off")]
    save_macros(str(fp), original, SK)
    loaded = load_macros(str(fp), SK)
    assert loaded[0].toggle is True
    assert loaded[0].toggle_text == "survey off"
    assert loaded[0].text == "survey on"


def test_toggle_default_state_false(tmp_path):
    fp = tmp_path / "macros.json"
    fp.write_text(
        json.dumps(
            {
                SK: {
                    "macros": [
                        {"key": "KEY_F5", "text": "on", "toggle": True, "toggle_text": "off"}
                    ]
                }
            }
        )
    )
    loaded = load_macros(str(fp), SK)
    assert loaded[0].toggle_state is False


def test_toggle_dispatch_alternates():
    pytest.importorskip("blessed")
    import types
    import asyncio
    from unittest.mock import patch

    sent: list[str] = []

    async def fake_exec(text, ctx, log):
        sent.append(text)

    ctx = types.SimpleNamespace()
    macro = Macro(key="KEY_F9", text="survey on", toggle=True, toggle_text="survey off")
    log = logging.getLogger("test")

    with patch("telix.client_repl.execute_macro_commands", fake_exec):
        dispatch = build_macro_dispatch([macro], ctx, log)
        handler = dispatch["KEY_F9"]
        loop = asyncio.new_event_loop()
        loop.run_until_complete(handler())
        loop.run_until_complete(handler())
        loop.run_until_complete(handler())
        loop.close()

    assert sent == ["survey on", "survey off", "survey on"]


def test_non_toggle_macro_unchanged(tmp_path):
    fp = tmp_path / "macros.json"
    original = [Macro(key="KEY_F5", text="look;")]
    save_macros(str(fp), original, SK)
    loaded = load_macros(str(fp), SK)
    assert loaded[0].toggle is False
    assert loaded[0].toggle_text == ""
    raw = json.loads(fp.read_text())
    assert "toggle" not in raw[SK]["macros"][0]


def test_expand_commands():
    from telix.client_repl import expand_commands

    cmds = expand_commands("look;inventory;")
    assert cmds == ["look", "inventory"]


def test_expand_commands_no_semicolon():
    from telix.client_repl import expand_commands

    cmds = expand_commands("partial text")
    assert cmds == ["partial text"]
