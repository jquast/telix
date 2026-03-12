"""Tests for telix.macros module."""

from __future__ import annotations

# std imports
import json
import types
import asyncio
import logging
from unittest.mock import patch

# 3rd party
import pytest

from telix.macros import (
    BUILTIN_MACROS,
    Macro,
    load_macros,
    save_macros,
    key_name_to_seq,
    build_macro_dispatch,
    key_name_to_ansi_seq,
    ensure_builtin_macros,
)

# local
from telix.client_repl import expand_commands
from telix.client_repl_commands import EDIT_RE, TOGGLE_RE, REPL_ACTION_RE, WALK_DIALOG_RE, _dispatch_repl_action

SK = "test.host:23"


def test_load_macros_valid(tmp_path):
    fp = tmp_path / "macros.json"
    fp.write_text(
        json.dumps({SK: {"macros": [{"key": "KEY_F5", "text": "look;"}, {"key": "KEY_ALT_N", "text": "north;"}]}})
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
    fp.write_text(json.dumps({SK: {"macros": [{"key": "", "text": "skip"}, {"key": "KEY_F6", "text": "keep;"}]}}))
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

    writer = types.SimpleNamespace(log=logging.getLogger("test"))
    macros = [Macro(key="KEY_LEFT", text="should be skipped"), Macro(key="KEY_ALT_E", text="should be kept")]
    with caplog.at_level(logging.WARNING):
        result = build_macro_dispatch(macros, writer, writer.log)
    assert "KEY_LEFT" not in result
    assert "\x1be" in result
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
    fp.write_text(json.dumps({SK: {"macros": [{"key": "KEY_F5", "text": "on", "toggle": True, "toggle_text": "off"}]}}))
    loaded = load_macros(str(fp), SK)
    assert loaded[0].toggle_state is False


def test_toggle_dispatch_alternates():
    pytest.importorskip("blessed")

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


def test_macro_error_echoed_and_logged(caplog):
    pytest.importorskip("blessed")

    echoed: list[str] = []
    prompt = types.SimpleNamespace(echo=echoed.append)
    ctx = types.SimpleNamespace(prompt=prompt)

    async def raise_exec(text, ctx, log):
        raise RuntimeError("boom")

    macro = Macro(key="KEY_F9", text="fail;")
    log = logging.getLogger("test")

    with patch("telix.client_repl.execute_macro_commands", raise_exec):
        dispatch = build_macro_dispatch([macro], ctx, log)
        handler = dispatch["KEY_F9"]
        loop = asyncio.new_event_loop()
        with caplog.at_level(logging.ERROR):
            loop.run_until_complete(handler())
            loop.run_until_complete(asyncio.sleep(0))
        loop.close()

    assert any("boom" in e for e in echoed)
    assert "macro execution failed" in caplog.text


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
    cmds = expand_commands("look;inventory;")
    assert cmds == ["look", "inventory"]


def test_expand_commands_no_semicolon():
    cmds = expand_commands("partial text")
    assert cmds == ["partial text"]


def test_macro_builtin_field_default_false():
    m = Macro(key="KEY_F5", text="look")
    assert m.builtin is False
    assert m.builtin_name == ""


def test_builtin_macro_roundtrip(tmp_path):
    fp = tmp_path / "macros.json"
    original = [
        Macro(key="KEY_F1", text="`help`", builtin=True, builtin_name="help"),
        Macro(key="KEY_ALT_M", text="`edit macros`", builtin=True, builtin_name="edit_macros"),
        Macro(key="KEY_F5", text="look;"),
    ]
    save_macros(str(fp), original, SK)
    loaded = load_macros(str(fp), SK)
    assert loaded[0].builtin is True
    assert loaded[0].builtin_name == "help"
    assert loaded[1].builtin is True
    assert loaded[1].builtin_name == "edit_macros"
    assert loaded[2].builtin is False
    assert loaded[2].builtin_name == ""


def test_ensure_builtin_macros_injects_into_empty():
    result = ensure_builtin_macros([])
    names = {m.builtin_name for m in result if m.builtin}
    assert "help" in names
    assert "edit_macros" in names
    assert "edit_highlights" in names
    assert "disconnect" in names
    assert "repaint" in names
    assert len(result) == len(BUILTIN_MACROS)


def test_ensure_builtin_macros_preserves_user_key_override():
    user = [Macro(key="KEY_F2", text="`help`", builtin=True, builtin_name="help")]
    result = ensure_builtin_macros(user)
    help_macros = [m for m in result if m.builtin_name == "help"]
    assert len(help_macros) == 1
    assert help_macros[0].key == "KEY_F2"


def test_ensure_builtin_macros_preserves_user_macros():
    user = [Macro(key="KEY_ALT_E", text="equip all;")]
    result = ensure_builtin_macros(user)
    user_kept = [m for m in result if not m.builtin]
    assert len(user_kept) == 1
    assert user_kept[0].key == "KEY_ALT_E"
    assert user_kept[0].text == "equip all;"


@pytest.mark.parametrize(
    "key_name, expected",
    [
        ("KEY_CTRL_L", "\x0c"),
        ("KEY_CTRL_CLOSE_BRACKET", "\x1d"),
        ("KEY_CTRL_A", "\x01"),
        ("KEY_ALT_H", "\x1bh"),
        ("KEY_ALT_M", "\x1bm"),
        ("KEY_ALT_SHIFT_H", "\x1bH"),
        ("KEY_ALT_SHIFT_T", "\x1bT"),
        ("KEY_F1", None),
        ("KEY_F3", None),
    ],
)
def test_key_name_to_seq(key_name, expected):
    assert key_name_to_seq(key_name) == expected


def test_build_dispatch_routes_alt_to_seq():
    pytest.importorskip("blessed")

    ctx = types.SimpleNamespace()
    macro = Macro(key="KEY_ALT_H", text="`edit highlights`")
    log = logging.getLogger("test")
    result = build_macro_dispatch([macro], ctx, log)
    assert "\x1bh" in result
    assert "KEY_ALT_H" not in result


def test_build_dispatch_routes_alt_shift_to_seq():
    pytest.importorskip("blessed")

    ctx = types.SimpleNamespace()
    macro = Macro(key="KEY_ALT_SHIFT_H", text="`toggle highlights`")
    log = logging.getLogger("test")
    result = build_macro_dispatch([macro], ctx, log)
    assert "\x1bH" in result
    assert "KEY_ALT_SHIFT_H" not in result


def test_build_dispatch_routes_ctrl_to_seq():
    pytest.importorskip("blessed")

    ctx = types.SimpleNamespace()
    macro = Macro(key="KEY_CTRL_L", text="`repaint`")
    log = logging.getLogger("test")
    result = build_macro_dispatch([macro], ctx, log)
    assert "\x0c" in result
    assert "KEY_CTRL_L" not in result


def test_builtin_macros_constant():
    assert len(BUILTIN_MACROS) == 16
    names = [m.builtin_name for m in BUILTIN_MACROS]
    assert len(names) == len(set(names))


def test_builtin_stopscript_macro():
    stopscript = next(m for m in BUILTIN_MACROS if m.builtin_name == "stopscript")
    assert stopscript.key == "KEY_ALT_Q"
    assert stopscript.text == "`stopscript`"


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("`help`", True),
        ("`disconnect`", True),
        ("`repaint`", True),
        ("`captures`", True),
        ("`CAPTURES`", True),
        ("`HELP`", True),
        ("`look`", False),
        ("help", False),
    ],
)
def test_repl_action_re(cmd, expected):
    assert bool(REPL_ACTION_RE.match(cmd)) is expected


@pytest.mark.parametrize(
    "cmd, expected_tab",
    [
        ("`edit macros`", "macros"),
        ("`edit highlights`", "highlights"),
        ("`edit triggers`", "triggers"),
        ("`edit rooms`", "rooms"),
        ("`edit captures`", "captures"),
        ("`edit bars`", "bars"),
        ("`edit theme`", "theme"),
        ("`Edit Macros`", "macros"),
    ],
)
def test_edit_re(cmd, expected_tab):
    m = EDIT_RE.match(cmd)
    assert m is not None
    assert m.group(1).lower() == expected_tab


@pytest.mark.parametrize(
    "cmd, expected_name",
    [("`toggle highlights`", "highlights"), ("`toggle triggers`", "triggers"), ("`Toggle Highlights`", "highlights")],
)
def test_toggle_re(cmd, expected_name):
    m = TOGGLE_RE.match(cmd)
    assert m is not None
    assert m.group(1).lower() == expected_name


@pytest.mark.parametrize(
    "cmd, expected_action",
    [
        ("`randomwalk dialog`", "randomwalk"),
        ("`autodiscover dialog`", "autodiscover"),
        ("`resume walk`", "resume"),
        ("`Randomwalk Dialog`", "randomwalk"),
    ],
)
def test_walk_dialog_re(cmd, expected_action):
    m = WALK_DIALOG_RE.match(cmd)
    assert m is not None
    assert m.group(1).lower() == expected_action


def test_dispatch_repl_action_calls_help():
    called = []
    ctx = types.SimpleNamespace(repl=types.SimpleNamespace(actions={"help": lambda: called.append("help")}))
    log = logging.getLogger("test")
    assert _dispatch_repl_action("`help`", ctx, log) is True
    assert called == ["help"]


def test_dispatch_repl_action_calls_edit():
    called = []
    ctx = types.SimpleNamespace(repl=types.SimpleNamespace(actions={"edit": called.append}))
    log = logging.getLogger("test")
    assert _dispatch_repl_action("`edit macros`", ctx, log) is True
    assert called == ["macros"]


def test_dispatch_repl_action_calls_toggle():
    called = []
    ctx = types.SimpleNamespace(repl=types.SimpleNamespace(actions={"toggle_highlights": lambda: called.append("th")}))
    log = logging.getLogger("test")
    assert _dispatch_repl_action("`toggle highlights`", ctx, log) is True
    assert called == ["th"]


def test_dispatch_repl_action_returns_false_for_plain():
    ctx = types.SimpleNamespace(repl=types.SimpleNamespace(actions={}))
    log = logging.getLogger("test")
    assert _dispatch_repl_action("look", ctx, log) is False


def test_dispatch_repl_action_noop_when_missing():
    ctx = types.SimpleNamespace(repl=types.SimpleNamespace(actions={}))
    log = logging.getLogger("test")
    assert _dispatch_repl_action("`help`", ctx, log) is True


@pytest.mark.parametrize(
    "key_name, expected",
    [
        ("KEY_UP", "\x1b[A"),
        ("KEY_DOWN", "\x1b[B"),
        ("KEY_RIGHT", "\x1b[C"),
        ("KEY_LEFT", "\x1b[D"),
        ("KEY_HOME", "\x1b[H"),
        ("KEY_END", "\x1b[F"),
        ("KEY_PGUP", "\x1b[5~"),
        ("KEY_PGDOWN", "\x1b[6~"),
        ("KEY_INSERT", "\x1b[2~"),
        ("KEY_DELETE", "\x1b[3~"),
        ("KEY_BTAB", "\x1b[Z"),
        ("KEY_F1", "\x1bOP"),
        ("KEY_F2", "\x1bOQ"),
        ("KEY_F3", "\x1bOR"),
        ("KEY_F4", "\x1bOS"),
        ("KEY_F5", "\x1b[15~"),
        ("KEY_F12", "\x1b[24~"),
    ],
)
def test_key_name_to_ansi_seq(key_name, expected):
    assert key_name_to_ansi_seq(key_name) == expected


def test_key_name_to_ansi_seq_unknown():
    assert key_name_to_ansi_seq("KEY_F99") is None
