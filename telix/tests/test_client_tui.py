"""Tests for :mod:`telix.client_tui` data model, persistence, and command builder."""

from __future__ import annotations

# std imports
import re
import sys
import json
import datetime
from typing import Any
from dataclasses import asdict, fields
from unittest.mock import MagicMock, patch

# 3rd party
import pytest

pytest.importorskip("textual", reason="textual not installed")

# local
from telix.macros import load_macros
from telix.autoreply import load_autoreplies
from telix.client_tui import (
    EDITOR_TABS,
    DEFAULTS_KEY,
    PRIMARY_PASTE_COMMANDS,
    CapsPane,
    MacroEditPane,
    SessionConfig,
    ThemeEditPane,
    AutoreplyTuple,
    MacroEditScreen,
    RoomBrowserPane,
    TelnetSessionApp,
    AutoreplyEditPane,
    CommandHelpScreen,
    HighlightEditPane,
    SessionListScreen,
    TabbedEditorScreen,
    AutoreplyEditScreen,
    ProgressBarEditPane,
    RandomwalkDialogScreen,
    AutodiscoverDialogScreen,
    int_val,
    tui_main,
    float_val,
    build_command,
    load_sessions,
    relative_time,
    save_sessions,
    build_tooltips,
    get_help_topic,
    read_primary_selection,
)


@pytest.fixture
def tui_tmp_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("telix.paths.SESSIONS_FILE", tmp_path / "s.json")
    monkeypatch.setattr("telix.paths.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("telix.paths.DATA_DIR", str(tmp_path))
    return tmp_path


def test_session_config_defaults() -> None:
    cfg = SessionConfig()
    assert cfg.port == 23
    assert cfg.encoding == "utf8"
    assert cfg.mode == "auto"
    assert cfg.colormatch == "vga"
    assert cfg.speed == 38400
    assert cfg.ssl is False
    assert cfg.no_repl is False
    assert cfg.compression is None


def test_session_config_roundtrip() -> None:
    cfg = SessionConfig(name="test", host="example.com", port=2323, ssl=True, encoding="cp437", mode="raw")
    data = asdict(cfg)
    restored = SessionConfig(**data)
    assert restored == cfg


def test_session_config_unknown_fields_ignored() -> None:
    data = asdict(SessionConfig(name="x"))
    data["unknown_future_field"] = 42
    known = {f.name for f in fields(SessionConfig)}
    filtered = {k: v for k, v in data.items() if k in known}
    cfg = SessionConfig(**filtered)
    assert cfg.name == "x"


def test_persistence_save_load_roundtrip(tui_tmp_paths) -> None:
    sessions = {
        "myserver": SessionConfig(name="myserver", host="example.com", port=23),
        DEFAULTS_KEY: SessionConfig(encoding="cp437", colormatch="cga"),
    }
    save_sessions(sessions)
    loaded = load_sessions()
    assert "myserver" in loaded
    assert loaded["myserver"].host == "example.com"
    assert loaded[DEFAULTS_KEY].encoding == "cp437"
    assert loaded[DEFAULTS_KEY].colormatch == "cga"


def test_persistence_load_empty(tui_tmp_paths, monkeypatch) -> None:
    monkeypatch.setattr("telix.paths.SESSIONS_FILE", tui_tmp_paths / "nope.json")
    assert not load_sessions()


def test_build_command_minimal() -> None:
    cfg = SessionConfig(host="example.com", port=23)
    cmd = build_command(cfg)
    assert cmd[0] == sys.executable
    assert cmd[1] == "-c"
    assert "example.com" in cmd
    assert "23" in cmd
    assert "--ssl" not in cmd
    assert "--raw-mode" not in cmd
    assert "--line-mode" not in cmd


@pytest.mark.parametrize("mode,flag", [("raw", "--raw-mode"), ("line", "--line-mode")])
def test_build_command_mode_flags(mode: str, flag: str) -> None:
    cfg = SessionConfig(host="h", port=23, mode=mode)
    assert flag in build_command(cfg)


def test_build_command_auto_mode_no_flag() -> None:
    cfg = SessionConfig(host="h", port=23, mode="auto")
    cmd = build_command(cfg)
    assert "--raw-mode" not in cmd
    assert "--line-mode" not in cmd


@pytest.mark.parametrize(
    "cfg_kwargs,expected_flags",
    [
        ({"ssl": True, "ssl_no_verify": True, "port": 992}, ["--ssl", "--ssl-no-verify"]),
        ({"colormatch": "cga", "background_color": "#101010"}, ["--colormatch", "--background-color"]),
        ({"no_repl": True}, ["--no-repl"]),
        ({"connect_timeout": 5.0}, ["--connect-timeout"]),
        ({"ansi_keys": True, "ascii_eol": True}, ["--ansi-keys", "--ascii-eol"]),
    ],
)
def test_build_command_flags(
    cfg_kwargs: dict,
    expected_flags: list[str],  # type: ignore[type-arg]
) -> None:
    cfg = SessionConfig(host="h", port=cfg_kwargs.pop("port", 23), **cfg_kwargs)
    cmd = build_command(cfg)
    for flag in expected_flags:
        assert flag in cmd


def test_build_command_ssl_cafile() -> None:
    cfg = SessionConfig(host="h", port=992, ssl_cafile="/tmp/ca.pem")
    cmd = build_command(cfg)
    assert "--ssl-cafile" in cmd
    idx = cmd.index("--ssl-cafile")
    assert cmd[idx + 1] == "/tmp/ca.pem"


def test_build_command_encoding() -> None:
    cfg = SessionConfig(host="h", port=23, encoding="cp437")
    cmd = build_command(cfg)
    assert "--encoding" in cmd
    idx = cmd.index("--encoding")
    assert cmd[idx + 1] == "cp437"


def test_build_command_default_encoding_omitted() -> None:
    cfg = SessionConfig(host="h", port=23, encoding="utf8")
    assert "--encoding" not in build_command(cfg)


def test_build_command_always_will_do() -> None:
    cfg = SessionConfig(host="h", port=23, always_will="MXP,GMCP", always_do="MSSP")
    cmd = build_command(cfg)
    will_indices = [i for i, v in enumerate(cmd) if v == "--always-will"]
    assert len(will_indices) == 2
    assert cmd[will_indices[0] + 1] == "MXP"
    assert cmd[will_indices[1] + 1] == "GMCP"
    do_idx = cmd.index("--always-do")
    assert cmd[do_idx + 1] == "MSSP"


def test_build_command_empty_always_will_omitted() -> None:
    cfg = SessionConfig(host="h", port=23, always_will="")
    assert "--always-will" not in build_command(cfg)


def test_build_command_connect_timeout_default_omitted() -> None:
    cfg = SessionConfig(host="h", port=23, connect_timeout=10.0)
    assert "--connect-timeout" not in build_command(cfg)


def test_defaults_inheritance_new_from_defaults() -> None:
    defaults = SessionConfig(name=DEFAULTS_KEY, encoding="cp437", colormatch="cga", mode="raw", loglevel="debug")
    new_cfg = SessionConfig(**asdict(defaults))
    new_cfg.name = "new_session"
    new_cfg.host = "example.com"
    new_cfg.last_connected = ""

    assert new_cfg.encoding == "cp437"
    assert new_cfg.colormatch == "cga"
    assert new_cfg.mode == "raw"
    assert new_cfg.loglevel == "debug"
    assert new_cfg.name == "new_session"
    assert new_cfg.host == "example.com"


def test_persistence_corrupted_json(tui_tmp_paths, monkeypatch) -> None:
    sessions_file = tui_tmp_paths / "sessions.json"
    sessions_file.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setattr("telix.paths.SESSIONS_FILE", sessions_file)
    with pytest.raises(Exception):
        load_sessions()


@pytest.mark.parametrize(
    "compression,expected_flag,absent_flag",
    [(True, "--compression", "--no-compression"), (False, "--no-compression", "--compression")],
)
def test_build_command_compression(compression: bool, expected_flag: str, absent_flag: str) -> None:
    cfg = SessionConfig(host="h", port=23, compression=compression)
    cmd = build_command(cfg)
    assert expected_flag in cmd
    assert absent_flag not in cmd


def test_build_command_compression_passive_omitted() -> None:
    cfg = SessionConfig(host="h", port=23, compression=None)
    cmd = build_command(cfg)
    assert "--compression" not in cmd
    assert "--no-compression" not in cmd


def test_build_command_missing_host() -> None:
    cfg = SessionConfig(host="", port=23)
    cmd = build_command(cfg)
    assert "" in cmd


def test_build_command_websocket_with_ssl() -> None:
    cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True)
    cmd = build_command(cfg)
    assert "wss://example.com" in cmd
    assert cmd[0] == sys.executable
    assert "ws_client" in cmd[2]


def test_build_command_websocket_without_ssl() -> None:
    cfg = SessionConfig(host="example.com", port=4000, protocol="websocket", ssl=False)
    cmd = build_command(cfg)
    assert "ws://example.com:4000" in cmd


def test_build_command_websocket_standard_port_omitted() -> None:
    cfg = SessionConfig(host="example.com", port=80, protocol="websocket", ssl=False)
    cmd = build_command(cfg)
    assert "ws://example.com" in cmd
    assert ":80" not in cmd[-1]


def test_build_command_websocket_ws_path() -> None:
    cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True, ws_path="/ws")
    cmd = build_command(cfg)
    assert "wss://example.com/ws" in cmd


def test_build_command_websocket_ws_path_no_leading_slash() -> None:
    cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True, ws_path="ws")
    cmd = build_command(cfg)
    assert "wss://example.com/ws" in cmd


def test_build_command_websocket_ws_path_empty() -> None:
    cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True, ws_path="")
    cmd = build_command(cfg)
    assert "wss://example.com" in cmd


def test_build_command_telnet_default_protocol() -> None:
    cfg = SessionConfig(host="example.com", port=23)
    cmd = build_command(cfg)
    assert "telnetlib3" in cmd[2]
    assert "ws_client" not in cmd[2]


def test_build_command_websocket_no_repl_forwarded() -> None:
    cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True, no_repl=True)
    cmd = build_command(cfg)
    assert "--no-repl" in cmd


def test_build_command_websocket_repl_default_omitted() -> None:
    cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True, no_repl=False)
    cmd = build_command(cfg)
    assert "--no-repl" not in cmd


def test_macro_screen_loads_empty(tmp_path) -> None:
    path = str(tmp_path / "macros.json")
    screen = MacroEditScreen(path=path)
    assert screen.pane.path == path
    assert screen.pane.macros == []


def test_macro_screen_loads_file(tmp_path) -> None:
    sk = "test.host:23"
    fp = tmp_path / "macros.json"
    fp.write_text(json.dumps({sk: {"macros": [{"key": "KEY_F5", "text": "look;"}]}}))
    screen = MacroEditScreen(path=str(fp), session_key=sk)
    screen.pane.load_from_file()
    assert len(screen.pane.macros) == 1
    assert screen.pane.macros[0] == ("KEY_F5", "look;", True, "", False, "")


def test_macro_screen_save(tmp_path) -> None:
    sk = "test.host:23"
    fp = tmp_path / "macros.json"
    screen = MacroEditScreen(path=str(fp), session_key=sk)
    screen.pane.macros = [("KEY_F5", "look;", True, "", False, ""), ("KEY_ALT_N", "north;", True, "", False, "")]
    screen.pane.save_to_file()

    loaded = load_macros(str(fp), sk)
    assert len(loaded) == 2
    assert loaded[0].key == "KEY_F5"
    assert loaded[0].text == "look;"
    assert loaded[1].key == "KEY_ALT_N"


def test_autoreply_screen_loads_empty(tmp_path) -> None:
    path = str(tmp_path / "autoreplies.json")
    screen = AutoreplyEditScreen(path=path)
    assert screen.pane.path == path
    assert screen.pane.rules == []


def test_autoreply_screen_loads_file(tmp_path) -> None:
    sk = "test.host:23"
    fp = tmp_path / "autoreplies.json"
    fp.write_text(json.dumps({sk: {"autoreplies": [{"pattern": r"\d+ gold", "reply": "get gold;"}]}}))
    screen = AutoreplyEditScreen(path=str(fp), session_key=sk)
    screen.pane.load_from_file()
    assert len(screen.pane.rules) == 1
    assert screen.pane.rules[0] == AutoreplyTuple(r"\d+ gold", "get gold;")


def test_autoreply_screen_save(tmp_path) -> None:
    sk = "test.host:23"
    fp = tmp_path / "autoreplies.json"
    screen = AutoreplyEditScreen(path=str(fp), session_key=sk)
    screen.pane.rules = [AutoreplyTuple(r"\d+ gold", "get gold;")]
    screen.pane.save_to_file()

    loaded = load_autoreplies(str(fp), sk)
    assert len(loaded) == 1
    assert loaded[0].pattern.pattern == r"\d+ gold"
    assert loaded[0].reply == "get gold;"


@pytest.mark.parametrize(
    "entry_extra,field_idx,expected", [({"when": {"HP%": ">50"}}, 4, {"HP%": ">50"}), ({"immediate": True}, 5, True)]
)
def test_autoreply_screen_loads_field(tmp_path, entry_extra, field_idx, expected) -> None:
    sk = "test.host:23"
    fp = tmp_path / "autoreplies.json"
    entry = {"pattern": "x", "reply": "y;", **entry_extra}
    fp.write_text(json.dumps({sk: {"autoreplies": [entry]}}))
    screen = AutoreplyEditScreen(path=str(fp), session_key=sk)
    screen.pane.load_from_file()
    assert screen.pane.rules[0][field_idx] == expected


@pytest.mark.parametrize(
    "rule_kwargs,json_key,expected,absent",
    [
        ({"when": {"MP%": ">=30"}}, "when", {"MP%": ">=30"}, False),
        ({"immediate": True}, "immediate", True, False),
        ({}, "immediate", None, True),
    ],
)
def test_autoreply_screen_saves_field(tmp_path, rule_kwargs, json_key, expected, absent) -> None:
    sk = "test.host:23"
    fp = tmp_path / "autoreplies.json"
    screen = AutoreplyEditScreen(path=str(fp), session_key=sk)
    screen.pane.rules = [AutoreplyTuple("x", "y;", **rule_kwargs)]
    screen.pane.save_to_file()
    raw = json.loads(fp.read_text())
    entry = raw[sk]["autoreplies"][0]
    if absent:
        assert json_key not in entry
    else:
        assert entry[json_key] == expected


def test_autoreply_screen_rejects_bad_regex(tmp_path) -> None:
    fp = tmp_path / "autoreplies.json"
    screen = AutoreplyEditScreen(path=str(fp))
    screen.pane.rules = [AutoreplyTuple("[invalid", "x")]
    with pytest.raises(re.error):
        screen.pane.save_to_file()


def test_helper_relative_time_empty() -> None:
    assert not relative_time("")


def test_helper_relative_time_invalid() -> None:
    result = relative_time("not-a-date")
    assert result == "not-a-date"[:10]


@pytest.mark.parametrize(
    "timedelta_kwargs,expected_substr",
    [({"days": 5}, "5d ago"), ({"minutes": 10}, "10m ago"), ({"hours": 3}, "3h ago")],
)
def test_helper_relative_time(timedelta_kwargs, expected_substr) -> None:
    past = datetime.datetime.now() - datetime.timedelta(**timedelta_kwargs)
    assert expected_substr in relative_time(past.isoformat())


def test_helper_relative_time_seconds_ago() -> None:
    past = datetime.datetime.now() - datetime.timedelta(seconds=30)
    result = relative_time(past.isoformat())
    assert "30s ago" in result or "29s ago" in result


@pytest.mark.parametrize(
    "func,input_val,fallback,expected",
    [(int_val, "42", 0, 42), (int_val, "abc", 42, 42), (float_val, "1.5", 0.0, 1.5), (float_val, "abc", 1.5, 1.5)],
)
def test_helper_val_conversion(func, input_val, fallback, expected) -> None:
    assert func(input_val, fallback) == expected


def test_helper_build_tooltips() -> None:
    tips = build_tooltips()
    assert isinstance(tips, dict)
    assert len(tips) > 0


def test_tui_main(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(TelnetSessionApp, "run", lambda self: called.append(True))
    tui_main()
    assert called


@pytest.mark.parametrize("topic", ["macro", "autoreply", "highlight", "session"])
def test_help_topics_exist(topic: str) -> None:
    content = get_help_topic(topic)
    assert len(content) > 100


@pytest.mark.parametrize("topic", ["macro", "autoreply", "highlight", "session"])
def test_help_screen_creates(topic: str) -> None:
    screen = CommandHelpScreen(topic=topic)
    assert screen.pane.topic == topic


def test_help_topic_macro_contains_key_sections() -> None:
    content = get_help_topic("macro")
    assert "## Macro Editor" in content
    assert "## Command Syntax" in content
    assert "## Backtick Commands" in content
    assert "`autodiscover`" in content
    assert "`randomwalk`" in content
    assert "`resume`" in content


def test_help_topic_autoreply_contains_key_sections() -> None:
    content = get_help_topic("autoreply")
    assert "## Autoreply Editor" in content
    assert "### Flags Explained" in content
    assert "### Pattern Syntax" in content
    assert "### Backreferences in Reply" in content
    assert "### Condition Gate" in content
    assert "\\1" in content


def test_help_topic_highlight_contains_key_sections() -> None:
    content = get_help_topic("highlight")
    assert "## Highlight Editor" in content
    assert "### Flags Explained" in content
    assert "### Style Names" in content
    assert "bold_red" in content


def test_help_topic_session_contains_key_sections() -> None:
    content = get_help_topic("session")
    assert "## Session Manager" in content
    assert "### Keyboard Shortcuts" in content
    assert "### Bookmarks" in content
    assert "### Flags" in content
    assert "### Search" in content


def test_randomwalk_dialog_writes_visit_level(tmp_path: Any) -> None:
    """The randomwalk dialog writes visit_level to the result file."""
    result_file = str(tmp_path / "result.json")
    screen = RandomwalkDialogScreen(result_file=result_file, default_visit_level=3)
    screen.write_result(True, 3)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["confirmed"] is True
    assert data["visit_level"] == 3


def test_randomwalk_dialog_default_visit_level() -> None:
    """The dialog initialises with the given default visit level."""
    screen = RandomwalkDialogScreen(default_visit_level=5)
    assert screen.default_visit_level == 5


def test_randomwalk_dialog_command_field(tmp_path: Any) -> None:
    """The randomwalk dialog result includes a command string."""
    result_file = str(tmp_path / "result.json")
    screen = RandomwalkDialogScreen(result_file=result_file, default_visit_level=2)
    screen.write_result(True, 3, auto_search=True, auto_evaluate=False)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`randomwalk 999 3 autosearch`"


def test_randomwalk_dialog_command_all_flags(tmp_path: Any) -> None:
    """The command string includes both autosearch and autoevaluate."""
    result_file = str(tmp_path / "result.json")
    screen = RandomwalkDialogScreen(result_file=result_file, default_visit_level=2)
    screen.write_result(True, 5, auto_search=True, auto_evaluate=True)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`randomwalk 999 5 autosearch autoevaluate`"


def test_randomwalk_dialog_command_no_flags(tmp_path: Any) -> None:
    """The command string omits flags when both are off."""
    result_file = str(tmp_path / "result.json")
    screen = RandomwalkDialogScreen(result_file=result_file, default_visit_level=2)
    screen.write_result(True, 2, auto_search=False, auto_evaluate=False)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`randomwalk 999 2`"


def test_autodiscover_dialog_writes_bfs(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = AutodiscoverDialogScreen(result_file=result_file, default_strategy="bfs")
    screen.write_result(True, "bfs")

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["confirmed"] is True
    assert data["strategy"] == "bfs"
    assert data["command"] == "`autodiscover bfs`"


def test_autodiscover_dialog_writes_dfs(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = AutodiscoverDialogScreen(result_file=result_file, default_strategy="dfs")
    screen.write_result(True, "dfs")

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["confirmed"] is True
    assert data["strategy"] == "dfs"
    assert data["command"] == "`autodiscover dfs`"


def test_autodiscover_dialog_cancel(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = AutodiscoverDialogScreen(result_file=result_file, default_strategy="bfs")
    screen.write_result(False, "bfs")

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["confirmed"] is False


def test_autodiscover_dialog_default_strategy() -> None:
    screen = AutodiscoverDialogScreen(default_strategy="dfs")
    assert screen.default_strategy == "dfs"


def test_autodiscover_dialog_all_flags(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = AutodiscoverDialogScreen(result_file=result_file, default_strategy="bfs")
    screen.write_result(True, "bfs", auto_search=True, auto_evaluate=True, auto_survey=True, autoreplies=True)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`autodiscover bfs autosearch autoevaluate autosurvey`"
    assert data["auto_search"] is True
    assert data["auto_evaluate"] is True
    assert data["auto_survey"] is True
    assert data["autoreplies"] is True


def test_autodiscover_dialog_noreply(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = AutodiscoverDialogScreen(result_file=result_file, default_strategy="dfs")
    screen.write_result(True, "dfs", autoreplies=False)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`autodiscover dfs noreply`"
    assert data["autoreplies"] is False


def test_autodiscover_dialog_autosurvey_only(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = AutodiscoverDialogScreen(result_file=result_file, default_strategy="bfs")
    screen.write_result(True, "bfs", auto_survey=True)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`autodiscover bfs autosurvey`"


def test_autodiscover_dialog_default_booleans() -> None:
    screen = AutodiscoverDialogScreen(
        default_auto_search=True, default_auto_evaluate=True, default_auto_survey=True, default_autoreplies=False
    )
    assert screen.default_auto_search is True
    assert screen.default_auto_evaluate is True
    assert screen.default_auto_survey is True
    assert screen.default_autoreplies is False


def test_randomwalk_dialog_autosurvey(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = RandomwalkDialogScreen(result_file=result_file, default_visit_level=2)
    screen.write_result(True, 2, auto_survey=True)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`randomwalk 999 2 autosurvey`"
    assert data["auto_survey"] is True


def test_randomwalk_dialog_noreply(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = RandomwalkDialogScreen(result_file=result_file, default_visit_level=2)
    screen.write_result(True, 2, autoreplies=False)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`randomwalk 999 2 noreply`"
    assert data["autoreplies"] is False


def test_randomwalk_dialog_all_new_flags(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = RandomwalkDialogScreen(result_file=result_file, default_visit_level=3)
    screen.write_result(True, 3, auto_search=True, auto_evaluate=True, auto_survey=True, autoreplies=True)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`randomwalk 999 3 autosearch autoevaluate autosurvey`"


def test_randomwalk_dialog_noreply_with_flags(tmp_path: Any) -> None:
    result_file = str(tmp_path / "result.json")
    screen = RandomwalkDialogScreen(result_file=result_file, default_visit_level=2)
    screen.write_result(True, 2, auto_search=True, autoreplies=False)

    with open(result_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["command"] == "`randomwalk 999 2 autosearch noreply`"


def test_randomwalk_dialog_default_autoreplies() -> None:
    screen = RandomwalkDialogScreen(default_auto_survey=True, default_autoreplies=False)
    assert screen.default_auto_survey is True
    assert screen.default_autoreplies is False


def make_sessions(n: int) -> dict[str, SessionConfig]:
    sessions: dict[str, SessionConfig] = {}
    for i in range(n):
        key = f"s{i:04d}"
        sessions[key] = SessionConfig(name=key, host=f"{key}.example.com")
    return sessions


def test_stale_generation_skips_batch(tui_tmp_paths: Any) -> None:
    screen = SessionListScreen()
    screen.sessions = make_sessions(5)
    screen.pending_rows = list(screen.sessions.items())[:3]
    screen.refresh_gen = 5
    screen.load_next_batch(gen=4)
    assert len(screen.pending_rows) == 3


class TestTabbedEditorScreen:
    def make_params(self, tmp_path: Any, initial_tab: str = "highlights") -> dict:
        return {
            "session_key": "test:4000",
            "macros_file": str(tmp_path / "macros.json"),
            "autoreplies_file": str(tmp_path / "autoreplies.json"),
            "highlights_file": str(tmp_path / "highlights.json"),
            "progressbars_file": str(tmp_path / "progressbars.json"),
            "gmcp_snapshot_file": "",
            "rooms_file": str(tmp_path / "rooms.db"),
            "current_room_file": str(tmp_path / "current_room.json"),
            "fasttravel_file": str(tmp_path / "fasttravel.json"),
            "chat_file": str(tmp_path / "chat.json"),
            "capture_file": "",
            "initial_tab": initial_tab,
            "initial_channel": "",
            "select_pattern": "",
            "logfile": "",
        }

    def test_creates_all_panes(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path)
        screen = TabbedEditorScreen(params)
        assert screen.initial_tab == "highlights"

    def test_initial_tab_macros(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path, initial_tab="macros")
        screen = TabbedEditorScreen(params)
        assert screen.initial_tab == "macros"

    def test_editor_tabs_has_seven_entries(self) -> None:
        assert len(EDITOR_TABS) == 7

    def test_editor_tabs_ids(self) -> None:
        ids = [tab_id for _, tab_id in EDITOR_TABS]
        assert ids == ["highlights", "rooms", "macros", "autoreplies", "captures", "bars", "theme"]

    def test_create_pane_macros(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path)
        screen = TabbedEditorScreen(params)
        pane = screen.create_pane("macros")
        assert isinstance(pane, MacroEditPane)
        assert pane.id == "macros"

    def test_create_pane_autoreplies(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path)
        screen = TabbedEditorScreen(params)
        pane = screen.create_pane("autoreplies")
        assert isinstance(pane, AutoreplyEditPane)
        assert pane.id == "autoreplies"

    def test_create_pane_highlights(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path)
        screen = TabbedEditorScreen(params)
        pane = screen.create_pane("highlights")
        assert isinstance(pane, HighlightEditPane)
        assert pane.id == "highlights"

    def test_create_pane_bars(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path)
        screen = TabbedEditorScreen(params)
        pane = screen.create_pane("bars")
        assert isinstance(pane, ProgressBarEditPane)
        assert pane.id == "bars"

    def test_create_pane_rooms(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path)
        screen = TabbedEditorScreen(params)
        pane = screen.create_pane("rooms")
        assert isinstance(pane, RoomBrowserPane)
        assert pane.id == "rooms"

    def test_create_pane_captures(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path)
        screen = TabbedEditorScreen(params)
        pane = screen.create_pane("captures")
        assert isinstance(pane, CapsPane)
        assert pane.id == "captures"

    def test_create_pane_theme(self, tmp_path: Any) -> None:
        params = self.make_params(tmp_path)
        screen = TabbedEditorScreen(params)
        pane = screen.create_pane("theme")
        assert isinstance(pane, ThemeEditPane)
        assert pane.id == "theme"


class TestActionConnectScreenRefresh:
    def test_screen_refresh_after_suspend(self, tui_tmp_paths: Any) -> None:
        """screen.refresh() is called after returning from app.suspend()."""
        screen = SessionListScreen()
        screen.sessions = {"srv": SessionConfig(name="srv", host="example.com")}

        screen.selected_key = MagicMock(return_value="srv")
        screen.save = MagicMock()
        screen.refresh_table = MagicMock()
        screen.select_row = MagicMock()
        screen.notify = MagicMock()

        mock_screen = MagicMock()
        screen._screen = mock_screen
        type(screen).screen = property(lambda self: self._screen)

        mock_app = MagicMock()
        mock_app.suspend.return_value.__enter__ = MagicMock()
        mock_app.suspend.return_value.__exit__ = MagicMock(return_value=False)
        screen._app = mock_app
        type(screen).app = property(lambda self: self._app)

        with (
            patch("telix.client_tui_base.subprocess.Popen") as mock_popen,
            patch("telix.client_tui_base.os.get_terminal_size") as mock_ts,
            patch("telix.client_tui_base.os.set_blocking"),
            patch("telix.client_tui_base.sys.stdout"),
        ):
            mock_ts.return_value = MagicMock(lines=24, columns=80)
            mock_popen.return_value = MagicMock()

            screen.action_connect()

        mock_screen.refresh.assert_called_once()


class TestReadPrimarySelection:
    def test_returns_text_from_first_available_helper(self, monkeypatch: Any) -> None:
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = b"hello world"
        with patch("telix.client_tui_base.subprocess.run", return_value=fake_result) as m:
            result = read_primary_selection()
        assert result == "hello world"
        m.assert_called_once_with(PRIMARY_PASTE_COMMANDS[0], capture_output=True, timeout=2, check=False)

    def test_tries_next_command_on_file_not_found(self, monkeypatch: Any) -> None:
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = b"from xsel"
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "xclip":
                raise FileNotFoundError
            return fake_result

        with patch("telix.client_tui_base.subprocess.run", side_effect=fake_run):
            result = read_primary_selection()
        assert result == "from xsel"
        assert calls[0][0] == "xclip"
        assert calls[1][0] == "xsel"

    def test_returns_empty_when_no_helpers_available(self) -> None:
        with patch("telix.client_tui_base.subprocess.run", side_effect=FileNotFoundError):
            assert read_primary_selection() == ""

    def test_skips_helper_with_nonzero_exit(self) -> None:
        fail = MagicMock(returncode=1, stdout=b"")
        ok = MagicMock(returncode=0, stdout=b"ok")

        def fake_run(cmd, **kwargs):
            return fail if cmd[0] == "xclip" else ok

        with patch("telix.client_tui_base.subprocess.run", side_effect=fake_run):
            assert read_primary_selection() == "ok"
