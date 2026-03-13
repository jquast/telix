"""Tests for telix.main -- CLI routing between TUI, telnet, and WebSocket."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from telix import main as main_mod
from telix.main import main, _detect_terminal_colors


@pytest.fixture(autouse=True)
def _no_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "_detect_terminal_colors", lambda: None)


class TestMainRouting:
    def test_ws_url_routes_to_run_ws_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ws:// positional arg calls run_ws_client, not run_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "ws://example.com:4000"])
        run_ws_calls = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            run_ws_calls.append(url)

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert run_ws_calls == ["ws://example.com:4000"]

    def test_wss_url_routes_to_run_ws_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wss:// positional arg calls run_ws_client, not run_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "wss://example.com"])
        run_ws_called = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            run_ws_called.append(url)

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert run_ws_called == ["wss://example.com"]

    def test_host_routes_to_telnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A plain host arg calls telnetlib3.client.run_client, not run_ws_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "mud.example.com", "4000"])
        with (
            patch("telix.main.ws_client.run_ws_client") as mock_ws,
            patch("telix.main.asyncio.run") as mock_run,
            patch("telix.main.telnetlib3.client.run_client"),
        ):
            main()
        mock_ws.assert_not_called()
        mock_run.assert_called_once()

    def test_no_args_launches_tui(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No positional args launches the TUI session manager."""
        monkeypatch.setattr(sys, "argv", ["telix"])
        with (
            patch("telix.main.client_tui_dialogs.tui_main") as mock_tui,
            patch("telix.main.ws_client.run_ws_client") as mock_ws,
        ):
            main()
        mock_tui.assert_called_once()
        mock_ws.assert_not_called()

    def test_ws_no_repl_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--no-repl is forwarded to run_ws_client when connecting via ws://."""
        monkeypatch.setattr(sys, "argv", ["telix", "ws://example.com:4000", "--no-repl"])
        run_ws_calls = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            run_ws_calls.append(no_repl)

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert run_ws_calls == [True]


class TestServerTypePresets:
    def test_bbs_telnet_injects_raw_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--bbs injects --raw-mode, VGA colormatch, and the telix shell."""
        monkeypatch.setattr(sys, "argv", ["telix", "--bbs", "bbs.example.com"])
        with patch("telix.main.asyncio.run"), patch("telix.main.telnetlib3.client.run_client"):
            main()
        assert "--raw-mode" in sys.argv
        assert main_mod._color_args.colormatch == "vga"
        assert "--colormatch" not in sys.argv
        assert "--shell=telix.client_shell.telix_client_shell" in sys.argv

    def test_mud_telnet_injects_line_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--mud injects --line-mode, --compression, and the telix shell."""
        monkeypatch.setattr(sys, "argv", ["telix", "--mud", "mud.example.com", "4000"])
        with patch("telix.main.asyncio.run"), patch("telix.main.telnetlib3.client.run_client"):
            main()
        assert "--line-mode" in sys.argv
        assert "--compression" in sys.argv
        assert main_mod._color_args.colormatch == "none"
        assert main_mod._color_args.no_ice_colors is True
        assert "--colormatch" not in sys.argv
        assert "--no-ice-colors" not in sys.argv
        assert "--shell=telix.client_shell.telix_client_shell" in sys.argv

    def test_bbs_ws_sets_no_repl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--bbs sets no_repl=True for WebSocket connections."""
        monkeypatch.setattr(sys, "argv", ["telix", "--bbs", "ws://bbs.example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append(no_repl)

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [True]

    def test_raw_mode_forwarded_for_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--raw-mode and --encoding are forwarded to run_ws_client for ws:// connections."""
        monkeypatch.setattr(sys, "argv", ["telix", "--encoding=cp437", "--raw-mode", "wss://example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append((kwargs.get("encoding"), kwargs.get("raw_mode")))

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [("cp437", True)]

    def test_line_mode_forwarded_for_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--line-mode maps to raw_mode=False for ws:// connections."""
        monkeypatch.setattr(sys, "argv", ["telix", "--line-mode", "wss://example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append(kwargs.get("raw_mode"))

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [False]

    def test_ansi_keys_ascii_eol_forwarded_for_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--ansi-keys and --ascii-eol are forwarded to run_ws_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "--ansi-keys", "--ascii-eol", "wss://example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append((kwargs.get("ansi_keys"), kwargs.get("ascii_eol")))

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [(True, True)]

    def test_mud_ws_keeps_repl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--mud keeps no_repl=False for WebSocket connections."""
        monkeypatch.setattr(sys, "argv", ["telix", "--mud", "ws://mud.example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append(no_repl)

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [False]

    def test_always_do_forwarded_for_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--always-do is parsed to a set of bytes and forwarded to run_ws_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "--always-do", "GMCP", "wss://example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append(kwargs.get("always_do"))

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        import telnetlib3.telopt

        assert captured == [{telnetlib3.telopt.GMCP}]

    def test_compression_forwarded_for_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--compression sets compression=True for ws:// connections."""
        monkeypatch.setattr(sys, "argv", ["telix", "--compression", "wss://example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append(kwargs.get("compression"))

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [True]

    def test_no_compression_forwarded_for_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--no-compression sets compression=False for ws:// connections."""
        monkeypatch.setattr(sys, "argv", ["telix", "--no-compression", "wss://example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append(kwargs.get("compression"))

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [False]

    def test_term_speed_send_environ_forwarded_for_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--term, --speed, and --send-environ are forwarded to run_ws_client."""
        monkeypatch.setattr(
            sys, "argv", ["telix", "--term=xterm-256color", "--speed=9600", "--send-environ=TERM", "wss://example.com"]
        )
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append((kwargs.get("term"), kwargs.get("speed"), kwargs.get("send_environ")))

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [("xterm-256color", 9600, ("TERM",))]

    def test_gmcp_modules_forwarded_for_ws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--gmcp-modules is parsed to a list and forwarded to run_ws_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "--gmcp-modules=Core.Supports,Char.Vitals", "wss://example.com"])
        captured = []

        async def fake_run_ws(
            url, shell, no_repl, loglevel, logfile, typescript, logfile_mode, typescript_mode, **kwargs
        ):
            captured.append(kwargs.get("gmcp_modules"))

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert captured == [["Core.Supports", "Char.Vitals"]]

    def test_bbs_flag_removed_from_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--bbs is removed from sys.argv before telnetlib3 parses it."""
        monkeypatch.setattr(sys, "argv", ["telix", "--bbs", "bbs.example.com"])
        with patch("telix.main.asyncio.run"), patch("telix.main.telnetlib3.client.run_client"):
            main()
        assert "--bbs" not in sys.argv


class TestDetectTerminalColors:
    def test_stores_detected_colors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_term = MagicMock()
        mock_term.cbreak.return_value.__enter__ = MagicMock()
        mock_term.cbreak.return_value.__exit__ = MagicMock(return_value=False)
        mock_term.get_bgcolor.return_value = (40, 40, 40)
        mock_term.get_fgcolor.return_value = (200, 200, 200)
        monkeypatch.setattr("blessed.Terminal", lambda: mock_term)
        monkeypatch.delenv("TELIX_DETECTED_BG", raising=False)
        monkeypatch.delenv("TELIX_DETECTED_FG", raising=False)

        _detect_terminal_colors()

        assert os.environ.get("TELIX_DETECTED_BG") == "40,40,40"
        assert os.environ.get("TELIX_DETECTED_FG") == "200,200,200"

    def test_converts_sentinel_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_term = MagicMock()
        mock_term.cbreak.return_value.__enter__ = MagicMock()
        mock_term.cbreak.return_value.__exit__ = MagicMock(return_value=False)
        mock_term.get_bgcolor.return_value = (-1, -1, -1)
        mock_term.get_fgcolor.return_value = (-1, -1, -1)
        monkeypatch.setattr("blessed.Terminal", lambda: mock_term)
        monkeypatch.setenv("TELIX_DETECTED_BG", "1,2,3")
        monkeypatch.setenv("TELIX_DETECTED_FG", "4,5,6")

        _detect_terminal_colors()

        assert "TELIX_DETECTED_BG" not in os.environ
        assert "TELIX_DETECTED_FG" not in os.environ

    def test_called_before_tui(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["telix"])
        call_order: list[str] = []

        def fake_detect() -> None:
            call_order.append("detect")

        def fake_tui() -> None:
            call_order.append("tui")

        monkeypatch.setattr(main_mod, "_detect_terminal_colors", fake_detect)
        with patch("telix.main.client_tui_dialogs.tui_main", side_effect=fake_tui):
            main()

        assert call_order == ["detect", "tui"]

    def test_called_before_telnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["telix", "mud.example.com", "4000"])
        call_order: list[str] = []

        def fake_detect() -> None:
            call_order.append("detect")

        monkeypatch.setattr(main_mod, "_detect_terminal_colors", fake_detect)
        with (
            patch("telix.main.asyncio.run", side_effect=lambda _: call_order.append("telnet")),
            patch("telix.main.telnetlib3.client.run_client"),
        ):
            main()

        assert call_order == ["detect", "telnet"]


class TestTelnetUrlRouting:
    def test_telnet_url_routes_to_telnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A telnet:// URL is rewritten to a bare host and falls through to telnetlib3."""
        monkeypatch.setattr(sys, "argv", ["telix", "telnet://mud.example.com"])
        with patch("telix.main.asyncio.run"), patch("telix.main.telnetlib3.client.run_client"):
            main()
        assert "mud.example.com" in sys.argv
        assert not any(a.startswith("telnet://") for a in sys.argv)

    def test_telnet_url_with_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """telnet://host:port injects both host and port into argv."""
        monkeypatch.setattr(sys, "argv", ["telix", "telnet://mud.example.com:4000"])
        with patch("telix.main.asyncio.run"), patch("telix.main.telnetlib3.client.run_client"):
            main()
        assert "mud.example.com" in sys.argv
        assert "4000" in sys.argv

    def test_telnets_url_injects_ssl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Telnets:// injects --ssl into argv."""
        monkeypatch.setattr(sys, "argv", ["telix", "telnets://mud.example.com"])
        with patch("telix.main.asyncio.run"), patch("telix.main.telnetlib3.client.run_client"):
            main()
        assert "--ssl" in sys.argv

    def test_telnets_does_not_duplicate_ssl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Telnets:// does not inject --ssl if already present."""
        monkeypatch.setattr(sys, "argv", ["telix", "telnets://mud.example.com", "--ssl"])
        with patch("telix.main.asyncio.run"), patch("telix.main.telnetlib3.client.run_client"):
            main()
        assert sys.argv.count("--ssl") == 1


class TestSshUrlRouting:
    def test_ssh_url_routes_to_run_ssh_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ssh:// URL calls run_ssh_client, not run_ws_client or run_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "ssh://bbs.example.com"])
        captured = []

        async def fake_run_ssh(host, port, username, key_file, term_type, shell, **kwargs):
            captured.append((host, port, username))

        with patch("telix.main.ssh_client.run_ssh_client", side_effect=fake_run_ssh):
            main()

        assert captured == [("bbs.example.com", 22, "")]

    def test_ssh_url_with_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ssh://host:port extracts the port from the URL."""
        monkeypatch.setattr(sys, "argv", ["telix", "ssh://bbs.example.com:2222"])
        captured = []

        async def fake_run_ssh(host, port, username, key_file, term_type, shell, **kwargs):
            captured.append((host, port))

        with patch("telix.main.ssh_client.run_ssh_client", side_effect=fake_run_ssh):
            main()

        assert captured == [("bbs.example.com", 2222)]

    def test_ssh_url_with_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ssh://user@host extracts the username from the URL."""
        monkeypatch.setattr(sys, "argv", ["telix", "ssh://sysop@bbs.example.com"])
        captured = []

        async def fake_run_ssh(host, port, username, key_file, term_type, shell, **kwargs):
            captured.append((host, username))

        with patch("telix.main.ssh_client.run_ssh_client", side_effect=fake_run_ssh):
            main()

        assert captured == [("bbs.example.com", "sysop")]

    def test_ssh_url_username_flag_overrides_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--username flag takes precedence over username embedded in the URL."""
        monkeypatch.setattr(sys, "argv", ["telix", "ssh://sysop@bbs.example.com", "--username", "admin"])
        captured = []

        async def fake_run_ssh(host, port, username, key_file, term_type, shell, **kwargs):
            captured.append(username)

        with patch("telix.main.ssh_client.run_ssh_client", side_effect=fake_run_ssh):
            main()

        assert captured == ["admin"]

    def test_ssh_url_key_file_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--key-file is forwarded to run_ssh_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "ssh://bbs.example.com", "--key-file", "id_ed25519"])
        captured = []

        async def fake_run_ssh(host, port, username, key_file, term_type, shell, **kwargs):
            captured.append(key_file)

        with patch("telix.main.ssh_client.run_ssh_client", side_effect=fake_run_ssh):
            main()

        assert captured == ["id_ed25519"]

    def test_ssh_url_with_user_and_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ssh://user@host:port extracts all three components from the URL."""
        monkeypatch.setattr(sys, "argv", ["telix", "ssh://sysop@bbs.example.com:2222"])
        captured = []

        async def fake_run_ssh(host, port, username, key_file, term_type, shell, **kwargs):
            captured.append((host, port, username))

        with patch("telix.main.ssh_client.run_ssh_client", side_effect=fake_run_ssh):
            main()

        assert captured == [("bbs.example.com", 2222, "sysop")]

    def test_ssh_url_does_not_call_ws_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ssh:// URL does not call run_ws_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "ssh://bbs.example.com"])

        async def fake_run_ssh(host, port, username, key_file, term_type, shell, **kwargs):
            pass

        with (
            patch("telix.main.ssh_client.run_ssh_client", side_effect=fake_run_ssh),
            patch("telix.main.ws_client.run_ws_client") as mock_ws,
        ):
            main()

        mock_ws.assert_not_called()


class TestGetArgvValue:
    def test_flag_equals_form(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "--term=xterm-256color", "host"])
        assert main_mod._get_argv_value("--term", "unknown") == "xterm-256color"

    def test_flag_space_form(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "--term", "vt100", "host"])
        assert main_mod._get_argv_value("--term", "unknown") == "vt100"

    def test_flag_missing_returns_default(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "host"])
        assert main_mod._get_argv_value("--term", "fallback") == "fallback"

    def test_flag_at_end_without_value(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "--term"])
        assert main_mod._get_argv_value("--term", "default") == "default"


class TestGetTermValue:
    def test_uses_argv_term(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "--term=xterm", "host"])
        assert main_mod._get_term_value() == "xterm"

    def test_falls_back_to_env(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "host"])
        monkeypatch.setenv("TERM", "screen-256color")
        assert main_mod._get_term_value() == "screen-256color"

    def test_falls_back_to_ansi(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "host"])
        monkeypatch.delenv("TERM", raising=False)
        assert main_mod._get_term_value() == "ansi"


class TestPopServerType:
    def test_bbs_flag(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "--bbs", "host"])
        assert main_mod.pop_server_type() == "bbs"
        assert "--bbs" not in sys.argv

    def test_mud_flag(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "--mud", "host"])
        assert main_mod.pop_server_type() == "mud"
        assert "--mud" not in sys.argv

    def test_no_flag(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["telix", "host"])
        assert main_mod.pop_server_type() == ""


class TestBuildWsCommandUsesMain:
    def test_ws_command_uses_telix_main(self) -> None:
        """build_ws_command spawns telix.main, not telix.ws_client."""
        from telix.client_tui_session_manager import SessionConfig, build_ws_command

        cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True)
        cmd = build_ws_command(cfg)
        assert "telix.main" in cmd[2]
        assert "ws_client" not in cmd[2]
