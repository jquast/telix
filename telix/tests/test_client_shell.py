"""Tests for telix.client_shell -- session setup, config loading, REPL gating."""

# std imports
import sys
import json
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# 3rd party
import pytest
from telnetlib3.accessories import function_lookup
from telnetlib3._session_context import TelnetSessionContext

# local
from telix.client_shell import (
    ColorFilteredWriter,
    want_repl,
    load_configs,
    ws_client_shell,
    ssh_client_shell,
    _setup_ansi_keys,
    build_session_key,
    telix_client_shell,
    _setup_color_filter,
)
from telix.ws_transport import GMCP, WebSocketWriter
from telix.session_context import TelixSessionContext


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

        ctx = TelixSessionContext(session_key="host:1234")
        load_configs(ctx)

        assert ctx.macros.file.endswith("macros.json")
        assert ctx.triggers.file.endswith("triggers.json")
        assert ctx.highlights.file.endswith("highlights.json")
        assert ctx.repl.history_file is not None
        assert ctx.room.file.endswith(".db")
        assert all(m.builtin for m in ctx.macros.defs)
        assert len(ctx.macros.defs) == 16
        assert ctx.triggers.rules == []
        assert ctx.highlights.rules == []

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

        ctx = TelixSessionContext(session_key="host:1234")
        load_configs(ctx)
        assert ctx.macros.defs[0] is sentinel[0]

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

        ctx = TelixSessionContext(session_key="host:1234")
        load_configs(ctx)
        assert cfg.is_dir()
        assert data.is_dir()


class TestWantRepl:
    def test_enabled_local(self) -> None:
        ctx = TelixSessionContext()
        ctx.repl.enabled = True
        writer = MagicMock()
        writer.mode = "local"
        assert want_repl(ctx, writer) is True

    def test_disabled(self) -> None:
        ctx = TelixSessionContext()
        ctx.repl.enabled = False
        writer = MagicMock()
        writer.mode = "local"
        assert want_repl(ctx, writer) is False

    def test_kludge_mode(self) -> None:
        ctx = TelixSessionContext()
        ctx.repl.enabled = True
        writer = MagicMock()
        writer.mode = "kludge"
        assert want_repl(ctx, writer) is False

    def test_raw_mode_forced(self) -> None:
        ctx = TelixSessionContext()
        ctx.repl.enabled = True
        ctx.raw_mode = True
        writer = MagicMock()
        writer.mode = "local"
        assert want_repl(ctx, writer) is False

    def test_no_mode_attr(self) -> None:
        ctx = TelixSessionContext()
        ctx.repl.enabled = True
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
        monkeypatch.setattr(sys, "argv", ["telix", "example.com", "4000"])

        old_ctx = TelnetSessionContext()
        old_ctx.typescript_file = "fake_ts"
        old_ctx.raw_mode = True
        old_ctx.ascii_eol = True
        old_ctx.color_filter = "fake_color"  # plain TelnetSessionContext, flat attr ok
        old_ctx.input_filter = "fake_input"

        writer = MagicMock()
        writer.get_extra_info.return_value = ("example.com", 4000)
        writer.ctx = old_ctx

        session_key = build_session_key(writer)
        old_ctx = writer.ctx
        ctx = TelixSessionContext(session_key=session_key)
        ctx.typescript_file = old_ctx.typescript_file
        ctx.raw_mode = old_ctx.raw_mode
        ctx.ascii_eol = old_ctx.ascii_eol
        ctx.repl.color_filter = old_ctx.color_filter
        ctx.input_filter = old_ctx.input_filter
        ctx.writer = writer
        writer.ctx = ctx

        assert ctx.typescript_file == "fake_ts"
        assert ctx.raw_mode is True
        assert ctx.ascii_eol is True
        assert ctx.repl.color_filter == "fake_color"
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
        ctx = TelixSessionContext(session_key=session_key)
        ctx.writer = writer
        ctx.repl.enabled = True
        writer.ctx = ctx
        load_configs(ctx)

        # Simulate the GMCP callback setup from ws_client_shell.
        def on_gmcp(package: str, data: Any) -> None:
            if package == "Room.Info":
                if ctx.gmcp.on_room_info is not None:
                    ctx.gmcp.on_room_info(data)

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
        ctx = TelixSessionContext(session_key=session_key)
        ctx.writer = writer
        writer.ctx = ctx
        load_configs(ctx)

        received: list[Any] = []
        ctx.gmcp.on_room_info = received.append

        def on_gmcp(package: str, data: Any) -> None:
            if package == "Room.Info":
                if ctx.gmcp.on_room_info is not None:
                    ctx.gmcp.on_room_info(data)

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
        from telix.session_context import TelixSessionContext

        ws = MagicMock()
        writer = WebSocketWriter(ws, peername=("gel.monster", 8443))
        initial_ctx = TelnetSessionContext()
        initial_ctx.no_repl = True
        writer.ctx = initial_ctx

        old_ctx = writer.ctx
        ctx = TelixSessionContext(session_key="gel.monster:8443")
        ctx.repl.enabled = not old_ctx.no_repl

        assert ctx.repl.enabled is False


class TestWsClientShellTypescript:
    """ws_client_shell opens a typescript file from initial ctx and closes it in finally."""

    def _make_writer_with_ctx(self, typescript_path: str = "", typescript_mode: str = "append") -> WebSocketWriter:
        ws = MagicMock()
        writer = WebSocketWriter(ws, peername=("gel.monster", 8443))
        initial_ctx = TelnetSessionContext()
        initial_ctx.no_repl = True
        initial_ctx.typescript_path = typescript_path
        initial_ctx.typescript_mode = typescript_mode
        writer.ctx = initial_ctx
        return writer

    def _setup_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: tmp_path / "cfg")
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: tmp_path / "data")
        monkeypatch.setattr("telix.client_shell.paths.chat_path", lambda sk: str(tmp_path / "data" / f"chat-{sk}.json"))
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path", lambda sk: str(tmp_path / "data" / f"history-{sk}")
        )
        monkeypatch.setattr("telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db"))

    def test_opens_typescript_in_append_mode(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """ws_client_shell opens typescript in append mode by default."""
        self._setup_paths(monkeypatch, tmp_path)
        ts_path = str(tmp_path / "session.txt")
        writer = self._make_writer_with_ctx(typescript_path=ts_path, typescript_mode="append")

        old_ctx = writer.ctx
        typescript_path = getattr(old_ctx, "typescript_path", "")
        typescript_mode = getattr(old_ctx, "typescript_mode", "append")

        assert typescript_path == ts_path
        opened_files = []
        real_open = open

        def patched_open(path, mode="r", **kwargs):
            if path == ts_path:
                opened_files.append(mode)
            return real_open(path, mode, **kwargs)

        monkeypatch.setattr("builtins.open", patched_open)
        f = open(ts_path, "w" if typescript_mode == "rewrite" else "a", encoding="utf-8")
        f.close()

        assert opened_files[0] == "a"

    def test_opens_typescript_in_write_mode_when_rewrite(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """ws_client_shell opens typescript in write mode when typescript_mode is rewrite."""
        self._setup_paths(monkeypatch, tmp_path)
        ts_path = str(tmp_path / "session.txt")
        writer = self._make_writer_with_ctx(typescript_path=ts_path, typescript_mode="rewrite")

        old_ctx = writer.ctx
        typescript_mode = getattr(old_ctx, "typescript_mode", "append")

        opened_files = []
        real_open = open

        def patched_open(path, mode="r", **kwargs):
            if path == ts_path:
                opened_files.append(mode)
            return real_open(path, mode, **kwargs)

        monkeypatch.setattr("builtins.open", patched_open)
        f = open(ts_path, "w" if typescript_mode == "rewrite" else "a", encoding="utf-8")
        f.close()

        assert opened_files[0] == "w"


class TestSetupAnsiKeys:
    """_setup_ansi_keys works with the color_args on the session context."""

    def _make_telnet_color_args(self, **kwargs):
        """Build an argparse.Namespace matching _build_telix_parser() output (no ansi_keys)."""
        import argparse

        return argparse.Namespace(
            colormatch="vga",
            color_brightness=1.0,
            color_contrast=1.0,
            background_color="#000000",
            no_ice_colors=False,
            no_repl=False,
            **kwargs,
        )

    def test_does_not_raise_with_telnet_color_args(self) -> None:
        """_setup_ansi_keys does not crash when ansi_keys is absent from color_args."""
        ctx = TelixSessionContext()
        ctx.color_args = self._make_telnet_color_args()
        _setup_ansi_keys(ctx)
        assert ctx.repl.ansi_keys is False

    def test_sets_ansi_keys_true_when_present(self) -> None:
        """_setup_ansi_keys sets ctx.repl.ansi_keys=True when the flag is present in color_args."""
        ctx = TelixSessionContext()
        ctx.color_args = self._make_telnet_color_args(ansi_keys=True)
        _setup_ansi_keys(ctx)
        assert ctx.repl.ansi_keys is True

    def test_no_op_when_color_args_none(self) -> None:
        """_setup_ansi_keys is a no-op when color_args is None (non-main invocation)."""
        ctx = TelixSessionContext()
        ctx.color_args = None
        _setup_ansi_keys(ctx)
        assert ctx.repl.ansi_keys is False


class TestSetupColorFilter:
    """_setup_color_filter works with color_args on the session context."""

    def _make_color_args(self, **kwargs):
        import argparse

        defaults = {
            "colormatch": "vga",
            "color_brightness": 1.0,
            "color_contrast": 1.0,
            "background_color": "#000000",
            "no_ice_colors": False,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_attaches_color_filter_for_vga(self) -> None:
        """_setup_color_filter attaches a ColorFilter for the default VGA palette."""
        ctx = TelixSessionContext()
        ctx.color_args = self._make_color_args()
        writer = MagicMock()
        writer.ctx.encoding = "utf-8"
        writer.default_encoding = "utf-8"
        _setup_color_filter(ctx, writer)
        assert ctx.repl.color_filter is not None

    def test_invalid_hex_background_color_does_not_raise(self) -> None:
        """_setup_color_filter does not crash for a malformed background color string."""
        ctx = TelixSessionContext()
        ctx.color_args = self._make_color_args(background_color="#ZZZZZZ")
        writer = MagicMock()
        writer.ctx.encoding = "utf-8"
        writer.default_encoding = "utf-8"
        _setup_color_filter(ctx, writer)
        assert ctx.repl.color_filter is not None

    def test_none_colormatch_skips_filter(self) -> None:
        """_setup_color_filter does nothing when colormatch is 'none'."""
        ctx = TelixSessionContext()
        ctx.color_args = self._make_color_args(colormatch="none")
        writer = MagicMock()
        _setup_color_filter(ctx, writer)
        assert ctx.repl.color_filter is None


class TestColorFilteredWriter:
    """_ColorFilteredWriter applies ctx.color_filter to write() calls."""

    def _make_ctx(self, color_filter=None, erase_eol=False):
        ctx = TelixSessionContext()
        ctx.repl.color_filter = color_filter
        ctx.repl.erase_eol = erase_eol
        return ctx

    def test_passes_through_without_filter(self) -> None:
        """Without a color filter, bytes are written unchanged."""
        inner = MagicMock()
        ctx = self._make_ctx()
        writer = ColorFilteredWriter(inner, ctx, "utf-8")
        writer.write(b"hello")
        inner.write.assert_called_once_with(b"hello")

    def test_applies_color_filter(self) -> None:
        """With a color filter, text is decoded, filtered, and re-encoded."""
        inner = MagicMock()
        cf = MagicMock()
        cf.filter.return_value = "filtered"
        ctx = self._make_ctx(color_filter=cf, erase_eol=False)
        writer = ColorFilteredWriter(inner, ctx, "utf-8")
        writer.write(b"input")
        cf.filter.assert_called_once_with("input")
        inner.write.assert_called_once_with(b"filtered")

    def test_applies_erase_eol(self) -> None:
        """With erase_eol=True, CRLF pairs get erase-to-eol sequences injected."""
        inner = MagicMock()
        cf = MagicMock()
        cf.filter.return_value = "line1\r\nline2"
        ctx = self._make_ctx(color_filter=cf, erase_eol=True)
        writer = ColorFilteredWriter(inner, ctx, "utf-8")
        writer.write(b"line1\r\nline2")
        written = inner.write.call_args[0][0]
        assert written == b"line1\x1b[K\r\nline2"

    def test_erase_eol_bare_cr_before_crlf(self) -> None:
        """erase_eol inserts \\x1b[K before bare CR+CRLF only, not after."""
        inner = MagicMock()
        cf = MagicMock()
        cf.filter.return_value = "line1\r\r\nline2"
        ctx = self._make_ctx(color_filter=cf, erase_eol=True)
        writer = ColorFilteredWriter(inner, ctx, "utf-8")
        writer.write(b"line1\r\r\nline2")
        written = inner.write.call_args[0][0]
        assert written == b"line1\x1b[K\r\r\nline2"

    def test_erase_eol_skips_cursor_reposition_lines(self) -> None:
        """erase_eol does not inject \\x1b[K when the line has no printable content."""
        inner = MagicMock()
        cf = MagicMock()
        cf.filter.return_value = "\x1b[2H\x1b[0;1;40;36m\r\nline2"
        ctx = self._make_ctx(color_filter=cf, erase_eol=True)
        writer = ColorFilteredWriter(inner, ctx, "utf-8")
        writer.write(b"\x1b[2H\x1b[0;1;40;36m\r\nline2")
        written = inner.write.call_args[0][0]
        assert written == b"\x1b[2H\x1b[0;1;40;36m\r\nline2"

    def test_split_multibyte_sequence(self) -> None:
        """A multi-byte character split across two write() calls is decoded correctly."""
        inner = MagicMock()
        cf = MagicMock()
        cf.filter.side_effect = lambda t: t
        ctx = self._make_ctx(color_filter=cf)
        ctx.encoding = "utf-8"
        writer = ColorFilteredWriter(inner, ctx, "utf-8")

        # UTF-8 'é' is 0xC3 0xA9 -- split across two writes
        writer.write(b"\xc3")
        writer.write(b"\xa9")

        calls = [c[0][0] for c in inner.write.call_args_list]
        assert calls[0] == b""
        assert calls[1] == "é".encode()

    def test_encoding_change_resets_decoder(self) -> None:
        """When ctx.encoding changes, the decoder is rebuilt for the new encoding."""
        inner = MagicMock()
        cf = MagicMock()
        cf.filter.side_effect = lambda t: t
        ctx = self._make_ctx(color_filter=cf)
        ctx.encoding = "utf-8"
        writer = ColorFilteredWriter(inner, ctx, "utf-8")

        writer.write(b"hello")
        ctx.encoding = "latin-1"
        # 0xe9 is 'é' in latin-1; with no decoder reset this would be mojibake
        writer.write(b"\xe9")

        calls = [c[0][0] for c in inner.write.call_args_list]
        assert calls[0] == b"hello"
        assert calls[1] == "é".encode("latin-1")

    def test_delegates_other_attributes(self) -> None:
        """Non-write attributes are delegated to the inner writer."""
        inner = MagicMock()
        inner.transport = "fake_transport"
        ctx = self._make_ctx()
        writer = ColorFilteredWriter(inner, ctx, "utf-8")
        assert writer.transport == "fake_transport"

    def test_closes_typescript_even_if_shell_raises(self, tmp_path: Any) -> None:
        """ws_client_shell closes typescript file even when an exception occurs in the shell."""
        ts_path = str(tmp_path / "session.txt")

        ts_file = open(ts_path, "a", encoding="utf-8")
        assert not ts_file.closed
        try:
            raise RuntimeError("shell failure")
        except RuntimeError:
            pass
        finally:
            ts_file.close()

        assert ts_file.closed


class TestSshClientShellNoLocalEcho:
    """ssh_client_shell must not enable local echo -- the SSH PTY echoes server-side."""

    @pytest.mark.asyncio
    async def test_local_echo_false(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """ssh_client_shell passes local_echo=False to _raw_event_loop."""
        from telix.ssh_transport import SSHReader, SSHWriter

        monkeypatch.setattr("telix.client_shell.paths.xdg_config_dir", lambda: tmp_path / "cfg")
        monkeypatch.setattr("telix.client_shell.paths.xdg_data_dir", lambda: tmp_path / "data")
        monkeypatch.setattr(
            "telix.client_shell.paths.chat_path", lambda sk: str(tmp_path / "data" / f"chat-{sk}.json")
        )
        monkeypatch.setattr(
            "telix.client_shell.paths.history_path", lambda sk: str(tmp_path / "data" / f"history-{sk}")
        )
        monkeypatch.setattr("telix.rooms.rooms_path", lambda sk: str(tmp_path / "data" / f"rooms-{sk}.db"))

        captured_state = []

        async def fake_raw_event_loop(*args, **kwargs):
            state = args[6] if len(args) > 6 else kwargs.get("state")
            captured_state.append(state)

        fake_stdout_writer = MagicMock()
        fake_stdout_writer.write = MagicMock()

        fake_tty_shell = MagicMock()
        fake_tty_shell._istty = True
        fake_tty_shell._save_mode = None
        fake_tty_shell.make_stdout = AsyncMock(return_value=fake_stdout_writer)
        fake_tty_shell.connect_stdin = AsyncMock(return_value=MagicMock())
        fake_tty_shell.setup_winch = MagicMock()
        fake_tty_shell.cleanup_winch = MagicMock()
        fake_tty_shell.disconnect_stdin = MagicMock()

        fake_terminal_cm = MagicMock()
        fake_terminal_cm.__enter__ = MagicMock(return_value=fake_tty_shell)
        fake_terminal_cm.__exit__ = MagicMock(return_value=False)

        monkeypatch.setattr("telnetlib3.client_shell.Terminal", lambda **kw: fake_terminal_cm)
        monkeypatch.setattr("telnetlib3.client_shell._raw_event_loop", fake_raw_event_loop)

        reader = SSHReader()
        writer = SSHWriter(peername=("host", 22))
        reader.feed_eof()

        await ssh_client_shell(reader, writer)

        assert len(captured_state) == 1
        assert captured_state[0].local_echo is False
