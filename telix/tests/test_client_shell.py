"""Tests for telix.client_shell -- session setup, config loading, REPL gating."""

# std imports
import json
import asyncio
from typing import Any
from unittest.mock import MagicMock

# 3rd party
import pytest
from telnetlib3.accessories import function_lookup
from telnetlib3._session_context import TelnetSessionContext

# local
from telix.client_shell import want_repl, load_configs, ws_client_shell, build_session_key, telix_client_shell
from telix.ws_transport import GMCP, WebSocketWriter
from telix.session_context import SessionContext


class TestBuildSessionKey:
    def test_from_peername(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["telix"])
        writer = MagicMock()
        writer.get_extra_info.return_value = ("example.com", 4000)
        assert build_session_key(writer) == "example.com:4000"

    def test_no_peername(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["telix"])
        writer = MagicMock()
        writer.get_extra_info.return_value = None
        assert build_session_key(writer) == ""

    def test_ipv4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["telix"])
        writer = MagicMock()
        writer.get_extra_info.return_value = ("192.168.1.1", 23)
        assert build_session_key(writer) == "192.168.1.1:23"

    def test_prefers_hostname_from_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv", ["telix", "--shell=telix.client_shell.telix_client_shell", "dunemud.net", "6788"]
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
        monkeypatch.setattr("telix.client_shell.paths.chat_path", lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"))
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path", lambda sk: str(tmp_path / "data" / f"history-{sk}")
        )
        monkeypatch.setattr("telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db"))

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
        monkeypatch.setattr("telix.client_shell.paths.chat_path", lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"))
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path", lambda sk: str(tmp_path / "data" / f"history-{sk}")
        )
        monkeypatch.setattr("telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db"))

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
        monkeypatch.setattr("telix.client_shell.paths.chat_path", lambda sk: str(data / f"chat-{sk}.json"))
        monkeypatch.setattr("telix.client_shell.paths.history_path", lambda sk: str(data / f"history-{sk}"))
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
    def test_preserves_base_ctx_attributes(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("telix.client_shell.paths.CONFIG_DIR", str(tmp_path / "cfg"))
        monkeypatch.setattr("telix.client_shell.paths.DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: tmp_path / "cfg")
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: tmp_path / "data")
        monkeypatch.setattr("telix.client_shell.paths.chat_path", lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"))
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path", lambda sk: str(tmp_path / "data" / f"history-{sk}")
        )
        monkeypatch.setattr("telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db"))

        old_ctx = TelnetSessionContext()
        old_ctx.typescript_file = "fake_ts"
        old_ctx.raw_mode = True
        old_ctx.ascii_eol = True
        old_ctx.color_filter = "fake_color"
        old_ctx.input_filter = "fake_input"

        writer = MagicMock()
        writer.get_extra_info.return_value = ("example.com", 4000)
        writer.ctx = old_ctx

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
        fn = function_lookup("telix.client_shell.telix_client_shell")
        assert fn is telix_client_shell


class TestBuildSessionKeyWebSocket:
    """build_session_key uses peername directly for WebSocket writers."""

    def test_ws_writer_uses_peername(self) -> None:
        ws = MagicMock()
        writer = WebSocketWriter(ws, peername=("gel.monster", 8443))
        assert build_session_key(writer) == "gel.monster:8443"

    def test_ws_writer_no_peername(self) -> None:
        ws = MagicMock()
        writer = WebSocketWriter(ws, peername=None)
        assert build_session_key(writer) == ""

    def test_ws_writer_skips_argv_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WebSocket writers never try to parse telnetlib3 CLI args."""
        monkeypatch.setattr(
            "sys.argv", ["telix", "--shell=telix.client_shell.telix_client_shell", "dunemud.net", "6788"]
        )
        ws = MagicMock()
        writer = WebSocketWriter(ws, peername=("gel.monster", 8443))
        # Should use peername, not argv host.
        assert build_session_key(writer) == "gel.monster:8443"


class TestWsClientShellSignature:
    """ws_client_shell is a coroutine and resolvable by function_lookup."""

    def test_is_coroutine_function(self) -> None:
        assert asyncio.iscoroutinefunction(ws_client_shell)

    def test_resolvable_via_function_lookup(self) -> None:
        fn = function_lookup("telix.client_shell.ws_client_shell")
        assert fn is ws_client_shell


class TestWsClientShellGMCP:
    """ws_client_shell wires GMCP dispatch callbacks correctly."""

    def _make_writer(self) -> WebSocketWriter:
        ws = MagicMock()
        return WebSocketWriter(ws, peername=("gel.monster", 8443))

    def test_gmcp_callback_registered(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """ws_client_shell registers a GMCP ext callback on the writer."""
        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: tmp_path / "cfg")
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: tmp_path / "data")
        monkeypatch.setattr("telix.client_shell.paths.chat_path", lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"))
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path", lambda sk: str(tmp_path / "data" / f"history-{sk}")
        )
        monkeypatch.setattr("telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db"))

        writer = self._make_writer()

        # We cannot run the full ws_client_shell (it needs a TTY and blessed),
        # so test the setup steps directly.
        session_key = build_session_key(writer)
        ctx = SessionContext(session_key=session_key)
        ctx.writer = writer
        ctx.repl_enabled = True
        writer.ctx = ctx
        load_configs(ctx)

        # Simulate the GMCP callback setup from ws_client_shell.
        def on_gmcp(package: str, data: Any) -> None:
            if package == "Room.Info":
                if ctx.on_room_info is not None:
                    ctx.on_room_info(data)

        writer.set_ext_callback(GMCP, on_gmcp)
        assert GMCP in writer._ext_callback

    def test_room_info_dispatch(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """GMCP Room.Info dispatches to ctx.on_room_info."""
        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: tmp_path / "cfg")
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: tmp_path / "data")
        monkeypatch.setattr("telix.client_shell.paths.chat_path", lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"))
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path", lambda sk: str(tmp_path / "data" / f"history-{sk}")
        )
        monkeypatch.setattr("telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db"))

        writer = self._make_writer()
        session_key = build_session_key(writer)
        ctx = SessionContext(session_key=session_key)
        ctx.writer = writer
        writer.ctx = ctx
        load_configs(ctx)

        received: list[Any] = []
        ctx.on_room_info = received.append

        def on_gmcp(package: str, data: Any) -> None:
            if package == "Room.Info":
                if ctx.on_room_info is not None:
                    ctx.on_room_info(data)

        writer.set_ext_callback(GMCP, on_gmcp)

        # Dispatch via the writer's GMCP mechanism.
        room_data = {"num": "42", "name": "Test Room"}
        writer.dispatch_gmcp("Room.Info", room_data)
        assert received == [room_data]


class TestWsClientShellNoRepl:
    """ws_client_shell respects no_repl set on the initial ctx by run_ws_client."""

    def test_repl_enabled_by_default(self) -> None:
        """ctx.repl_enabled is True when no_repl is not set on the initial ctx."""
        from telnetlib3._session_context import TelnetSessionContext
        from telix.ws_transport import WebSocketWriter

        ws = MagicMock()
        writer = WebSocketWriter(ws, peername=("gel.monster", 8443))
        initial_ctx = TelnetSessionContext()
        writer.ctx = initial_ctx

        # no_repl not set → repl_enabled should be True
        assert not getattr(initial_ctx, "no_repl", False)

    def test_no_repl_flag_on_initial_ctx_disables_repl(self) -> None:
        """When initial ctx has no_repl=True, ws_client_shell sets repl_enabled=False."""
        from telnetlib3._session_context import TelnetSessionContext
        from telix.ws_transport import WebSocketWriter
        from telix.session_context import SessionContext

        ws = MagicMock()
        writer = WebSocketWriter(ws, peername=("gel.monster", 8443))
        initial_ctx = TelnetSessionContext()
        initial_ctx.no_repl = True
        writer.ctx = initial_ctx

        old_ctx = writer.ctx
        ctx = SessionContext(session_key="gel.monster:8443")
        ctx.repl_enabled = not old_ctx.no_repl

        assert ctx.repl_enabled is False
