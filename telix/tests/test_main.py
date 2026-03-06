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


class TestBuildWsCommandUsesMain:
    def test_ws_command_uses_telix_main(self) -> None:
        """build_ws_command spawns telix.main, not telix.ws_client."""
        from telix.client_tui_base import SessionConfig, build_ws_command

        cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True)
        cmd = build_ws_command(cfg)
        assert "telix.main" in cmd[2]
        assert "ws_client" not in cmd[2]
