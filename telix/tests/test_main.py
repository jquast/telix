"""Tests for telix.main -- CLI routing between TUI, telnet, and WebSocket."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from telix.main import main


class TestMainRouting:
    def test_ws_url_routes_to_run_ws_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ws:// positional arg calls run_ws_client, not run_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "ws://example.com:4000"])
        run_ws_calls = []

        async def fake_run_ws(url, shell, no_repl, logfile, typescript, logfile_mode, typescript_mode):
            run_ws_calls.append(url)

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert run_ws_calls == ["ws://example.com:4000"]

    def test_wss_url_routes_to_run_ws_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wss:// positional arg calls run_ws_client, not run_client."""
        monkeypatch.setattr(sys, "argv", ["telix", "wss://example.com"])
        run_ws_called = []

        async def fake_run_ws(url, shell, no_repl, logfile, typescript, logfile_mode, typescript_mode):
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
            patch("telix.main.telnetlib3.client.run_client") as mock_telnet,
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

        async def fake_run_ws(url, shell, no_repl, logfile, typescript, logfile_mode, typescript_mode):
            run_ws_calls.append(no_repl)

        with patch("telix.main.ws_client.run_ws_client", side_effect=fake_run_ws):
            main()

        assert run_ws_calls == [True]


class TestBuildWsCommandUsesMain:
    def test_ws_command_uses_telix_main(self) -> None:
        """build_ws_command spawns telix.main, not telix.ws_client."""
        from telix.client_tui_base import SessionConfig, build_ws_command

        cfg = SessionConfig(host="example.com", port=443, protocol="websocket", ssl=True)
        cmd = build_ws_command(cfg)
        assert "telix.main" in cmd[2]
        assert "ws_client" not in cmd[2]
