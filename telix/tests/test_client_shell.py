"""Tests for telix.client_shell -- session setup, config loading, REPL gating."""

from __future__ import annotations

# std imports
import json
import asyncio
from typing import Any
from unittest.mock import MagicMock

# 3rd party
import pytest

# local
from telix.client_shell import want_repl, load_configs, build_session_key, telix_client_shell
from telix.session_context import SessionContext


class TestBuildSessionKey:
    def test_from_peername(self) -> None:
        writer = MagicMock()
        writer.get_extra_info.return_value = ("example.com", 4000)
        assert build_session_key(writer) == "example.com:4000"

    def test_no_peername(self) -> None:
        writer = MagicMock()
        writer.get_extra_info.return_value = None
        assert build_session_key(writer) == ""

    def test_ipv4(self) -> None:
        writer = MagicMock()
        writer.get_extra_info.return_value = ("192.168.1.1", 23)
        assert build_session_key(writer) == "192.168.1.1:23"

    def test_prefers_hostname_from_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["telix", "--shell=telix.client_shell.telix_client_shell", "dunemud.net", "6788"],
        )
        writer = MagicMock()
        writer.get_extra_info.return_value = ("138.197.134.82", 6788)
        assert build_session_key(writer) == "dunemud.net:6788"

    def test_falls_back_to_peername_without_host_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["telix"])
        writer = MagicMock()
        writer.get_extra_info.return_value = ("10.0.0.1", 23)
        assert build_session_key(writer) == "10.0.0.1:23"


class TestLoadConfigs:
    def test_empty_dirs(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("telix.client_shell.paths.CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setattr("telix.client_shell.paths.DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: tmp_path / "cfg")
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: tmp_path / "data")
        monkeypatch.setattr(
            "telix.client_shell.paths.chat_path",
            lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"),
        )
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path",
            lambda sk: str(tmp_path / "data" / f"history-{sk}"),
        )
        monkeypatch.setattr(
            "telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db")
        )

        ctx = SessionContext(session_key="host:1234")
        load_configs(ctx)

        assert ctx.macros_file.endswith("macros.json")
        assert ctx.autoreplies_file.endswith("autoreplies.json")
        assert ctx.highlights_file.endswith("highlights.json")
        assert ctx.history_file is not None
        assert ctx.rooms_file.endswith(".db")
        assert ctx.macro_defs == []
        assert ctx.autoreply_rules == []
        assert ctx.highlight_rules == []

    def test_loads_macros(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        macros_file = cfg / "macros.json"
        macros_file.write_text(json.dumps({"macros": []}))

        monkeypatch.setattr("telix.client_shell.paths.CONFIG_DIR", str(cfg))
        monkeypatch.setattr("telix.client_shell.paths.DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: cfg)
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: tmp_path / "data")
        monkeypatch.setattr(
            "telix.client_shell.paths.chat_path",
            lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"),
        )
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path",
            lambda sk: str(tmp_path / "data" / f"history-{sk}"),
        )
        monkeypatch.setattr(
            "telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db")
        )

        sentinel = [MagicMock()]
        monkeypatch.setattr("telix.macros.load_macros", lambda path, sk: sentinel)

        ctx = SessionContext(session_key="host:1234")
        load_configs(ctx)
        assert ctx.macro_defs is sentinel

    def test_creates_dirs(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "new_cfg"
        data = tmp_path / "new_data"
        monkeypatch.setattr("telix.client_shell.paths.CONFIG_DIR", str(cfg))
        monkeypatch.setattr("telix.client_shell.paths.DATA_DIR", str(data))
        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: cfg)
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: data)
        monkeypatch.setattr(
            "telix.client_shell.paths.chat_path", lambda sk: str(data / f"chat-{sk}.json")
        )
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path", lambda sk: str(data / f"history-{sk}")
        )
        monkeypatch.setattr("telix.rooms.rooms_path", lambda sk: str(data / f"rooms-{sk}.db"))

        ctx = SessionContext(session_key="host:1234")
        load_configs(ctx)
        assert cfg.is_dir()
        assert data.is_dir()


class TestWantRepl:
    def test_enabled_local(self) -> None:
        ctx = SessionContext()
        ctx.repl_enabled = True
        writer = MagicMock()
        writer.mode = "local"
        assert want_repl(ctx, writer) is True

    def test_disabled(self) -> None:
        ctx = SessionContext()
        ctx.repl_enabled = False
        writer = MagicMock()
        writer.mode = "local"
        assert want_repl(ctx, writer) is False

    def test_kludge_mode(self) -> None:
        ctx = SessionContext()
        ctx.repl_enabled = True
        writer = MagicMock()
        writer.mode = "kludge"
        assert want_repl(ctx, writer) is False

    def test_no_mode_attr(self) -> None:
        ctx = SessionContext()
        ctx.repl_enabled = True
        writer = MagicMock(spec=[])
        assert want_repl(ctx, writer) is True


class TestCtxPreservation:
    def test_preserves_base_ctx_attributes(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from telnetlib3._session_context import TelnetSessionContext

        monkeypatch.setattr("telix.client_shell.paths.CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setattr("telix.client_shell.paths.DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: tmp_path / "cfg")
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: tmp_path / "data")
        monkeypatch.setattr(
            "telix.client_shell.paths.chat_path",
            lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"),
        )
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path",
            lambda sk: str(tmp_path / "data" / f"history-{sk}"),
        )
        monkeypatch.setattr(
            "telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db")
        )

        old_ctx = TelnetSessionContext()
        old_ctx.typescript_file = "fake_ts"
        old_ctx.raw_mode = True
        old_ctx.ascii_eol = True
        old_ctx.color_filter = "fake_color"
        old_ctx.input_filter = "fake_input"

        writer = MagicMock()
        writer.get_extra_info.return_value = ("example.com", 4000)
        writer.ctx = old_ctx

        from telix.client_shell import load_configs, build_session_key

        session_key = build_session_key(writer)
        old_ctx = writer.ctx
        ctx = SessionContext(session_key=session_key)
        ctx.typescript_file = old_ctx.typescript_file
        ctx.raw_mode = old_ctx.raw_mode
        ctx.ascii_eol = old_ctx.ascii_eol
        ctx.color_filter = old_ctx.color_filter
        ctx.input_filter = old_ctx.input_filter
        ctx.writer = writer
        writer.ctx = ctx

        assert ctx.typescript_file == "fake_ts"
        assert ctx.raw_mode is True
        assert ctx.ascii_eol is True
        assert ctx.color_filter == "fake_color"
        assert ctx.input_filter == "fake_input"
        assert ctx.session_key == "example.com:4000"


class TestShellSignature:
    def test_is_coroutine_function(self) -> None:
        assert asyncio.iscoroutinefunction(telix_client_shell)

    def test_resolvable_via_function_lookup(self) -> None:
        from telnetlib3.accessories import function_lookup

        fn = function_lookup("telix.client_shell.telix_client_shell")
        assert fn is telix_client_shell
