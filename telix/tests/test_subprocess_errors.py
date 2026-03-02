"""Tests for subprocess error prompts in client_repl_dialogs."""

from __future__ import annotations

# std imports
import sys
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

# 3rd party
import pytest

# local
from telix.session_context import SessionContext


@pytest.fixture()
def _stub_terminal(monkeypatch: Any) -> None:
    """Stub terminal helpers so subprocess launchers don't touch the real TTY."""
    monkeypatch.setattr("telix.client_repl_dialogs._get_logfile_path", lambda: "")
    monkeypatch.setattr("telix.client_repl._restore_after_subprocess", lambda buf: None)
    monkeypatch.setattr("telix.client_repl._terminal_cleanup", lambda: "")
    monkeypatch.setattr(
        "telix.client_repl._get_term",
        lambda: MagicMock(change_scroll_region=MagicMock(return_value=""), height=24),
    )
    monkeypatch.setattr(
        "telix.client_repl._blocking_fds",
        MagicMock(
            return_value=MagicMock(
                __enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)
            )
        ),
    )


def _make_ctx() -> SessionContext:
    ctx = SessionContext(session_key="host:1234")
    ctx.macros_file = "/tmp/macros.json"
    ctx.autoreplies_file = "/tmp/autoreplies.json"
    ctx.highlights_file = "/tmp/highlights.json"
    ctx.chat_file = "/tmp/chat.json"
    ctx.chat_messages = [{"channel": "ooc", "text": "hi"}]
    ctx.rooms_file = "/tmp/rooms.db"
    ctx.current_room_file = "/tmp/current_room"
    return ctx


class TestLaunchTuiEditorError:
    @pytest.mark.usefixtures("_stub_terminal")
    def test_error_prompt_on_nonzero_with_stderr(self, monkeypatch: Any) -> None:
        from telix.client_repl_dialogs import _launch_tui_editor

        fail_result = subprocess.CompletedProcess(
            args=[], returncode=1, stderr=b"NameError: something\n"
        )
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: fail_result)
        monkeypatch.setattr("os.path.exists", lambda p: False)

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        with patch("builtins.input", return_value="") as mock_input:
            _launch_tui_editor("macros", _make_ctx())

        assert any("Press RETURN" in s for s in written)
        mock_input.assert_called_once()

    @pytest.mark.usefixtures("_stub_terminal")
    def test_prompt_on_nonzero_without_stderr(self, monkeypatch: Any) -> None:
        """Non-zero exit always pauses, even without stderr output."""
        from telix.client_repl_dialogs import _launch_tui_editor

        fail_result = subprocess.CompletedProcess(args=[], returncode=1)
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: fail_result)
        monkeypatch.setattr("os.path.exists", lambda p: False)

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        with patch("builtins.input", return_value=""):
            _launch_tui_editor("macros", _make_ctx())
        assert any("Press RETURN" in s for s in written)

    @pytest.mark.usefixtures("_stub_terminal")
    def test_no_prompt_on_success(self, monkeypatch: Any) -> None:
        from telix.client_repl_dialogs import _launch_tui_editor

        ok_result = subprocess.CompletedProcess(args=[], returncode=0)
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: ok_result)
        monkeypatch.setattr("os.path.exists", lambda p: False)

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        _launch_tui_editor("macros", _make_ctx())
        assert not any("Press RETURN" in s for s in written)


class TestLaunchUnifiedEditor:
    @pytest.mark.usefixtures("_stub_terminal")
    def test_subprocess_receives_json_params(self, monkeypatch: Any) -> None:
        import json

        from telix.client_repl_dialogs import _launch_unified_editor

        captured_cmd: list[list[str]] = []
        ok_result = subprocess.CompletedProcess(args=[], returncode=0)

        def fake_run(cmd, check=False, **kw):
            captured_cmd.append(cmd)
            return ok_result

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("telix.rooms.read_fasttravel", lambda p: ([], False))

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        ctx = _make_ctx()
        ctx.room_graph = None
        _launch_unified_editor("macros", ctx)

        assert len(captured_cmd) == 1
        cmd = captured_cmd[0]
        assert "unified_editor_main" in cmd[2]
        params = json.loads(cmd[3])
        assert params["initial_tab"] == "macros"
        assert params["session_key"] == "host:1234"

    @pytest.mark.usefixtures("_stub_terminal")
    def test_all_tabs_pass_correct_initial_tab(self, monkeypatch: Any) -> None:
        import json

        from telix.client_repl_dialogs import _launch_unified_editor

        captured_tabs: list[str] = []
        ok_result = subprocess.CompletedProcess(args=[], returncode=0)

        def fake_run(cmd, check=False, **kw):
            params = json.loads(cmd[3])
            captured_tabs.append(params["initial_tab"])
            return ok_result

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("telix.rooms.read_fasttravel", lambda p: ([], False))

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        ctx = _make_ctx()
        ctx.room_graph = None
        for tab in ("help", "highlights", "rooms", "macros", "autoreplies", "captures", "bars"):
            _launch_unified_editor(tab, ctx)

        assert captured_tabs == [
            "help",
            "highlights",
            "rooms",
            "macros",
            "autoreplies",
            "captures",
            "bars",
        ]

    @pytest.mark.usefixtures("_stub_terminal")
    def test_error_prompt_on_nonzero(self, monkeypatch: Any) -> None:
        from telix.client_repl_dialogs import _launch_unified_editor

        fail_result = subprocess.CompletedProcess(args=[], returncode=1)
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: fail_result)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("telix.rooms.read_fasttravel", lambda p: ([], False))

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        ctx = _make_ctx()
        ctx.room_graph = None
        with patch("builtins.input", return_value="") as mock_input:
            _launch_unified_editor("macros", ctx)

        assert any("Press RETURN" in s for s in written)
        mock_input.assert_called_once()

    @pytest.mark.usefixtures("_stub_terminal")
    def test_reloads_all_configs(self, monkeypatch: Any) -> None:
        from telix.client_repl_dialogs import _launch_unified_editor

        ok_result = subprocess.CompletedProcess(args=[], returncode=0)
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: ok_result)
        monkeypatch.setattr("telix.rooms.read_fasttravel", lambda p: ([], False))

        reloaded: list[str] = []
        monkeypatch.setattr(
            "telix.client_repl_dialogs._reload_macros",
            lambda ctx, path, sk, log: reloaded.append("macros"),
        )
        monkeypatch.setattr(
            "telix.client_repl_dialogs._reload_highlights",
            lambda ctx, path, sk, log: reloaded.append("highlights"),
        )
        monkeypatch.setattr(
            "telix.client_repl_dialogs._reload_autoreplies",
            lambda ctx, path, sk, log: reloaded.append("autoreplies"),
        )
        monkeypatch.setattr(
            "telix.client_repl_dialogs._reload_progressbars",
            lambda ctx, path, sk, log: reloaded.append("progressbars"),
        )

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        ctx = _make_ctx()
        ctx.room_graph = None
        _launch_unified_editor("help", ctx)

        assert sorted(reloaded) == ["autoreplies", "highlights", "macros", "progressbars"]

    @pytest.mark.usefixtures("_stub_terminal")
    def test_handles_fast_travel(self, monkeypatch: Any) -> None:
        import asyncio

        from telix.client_repl_dialogs import _launch_unified_editor

        ok_result = subprocess.CompletedProcess(args=[], returncode=0)
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: ok_result)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("telix.rooms.read_fasttravel", lambda p: (["north", "east"], False))

        async def fake_fast_travel(steps, ctx, log, noreply=False):
            pass

        monkeypatch.setattr("telix.client_repl_travel._fast_travel", fake_fast_travel)

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ctx = _make_ctx()
            ctx.room_graph = None
            _launch_unified_editor("rooms", ctx)
            assert ctx.travel_task is not None
        finally:
            loop.close()
            asyncio.set_event_loop(None)


class TestLaunchChatViewerError:
    @pytest.mark.usefixtures("_stub_terminal")
    def test_error_prompt_on_nonzero(self, monkeypatch: Any) -> None:
        from telix.client_repl_dialogs import _launch_chat_viewer

        fail_result = subprocess.CompletedProcess(args=[], returncode=1, stderr=b"Error\n")
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: fail_result)

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        with patch("builtins.input", return_value="") as mock_input:
            _launch_chat_viewer(_make_ctx())

        assert any("Press RETURN" in s for s in written)
        mock_input.assert_called_once()


class TestLaunchRoomBrowserError:
    @pytest.mark.usefixtures("_stub_terminal")
    def test_error_prompt_on_nonzero(self, monkeypatch: Any) -> None:
        from telix.client_repl_dialogs import _launch_room_browser

        fail_result = subprocess.CompletedProcess(args=[], returncode=1, stderr=b"Error\n")
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: fail_result)
        monkeypatch.setattr("telix.rooms.read_fasttravel", lambda p: ([], False))

        written: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", MagicMock(write=lambda s: written.append(s), flush=MagicMock())
        )
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr(
            "sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False))
        )

        ctx = _make_ctx()
        ctx.room_graph = None

        with patch("builtins.input", return_value="") as mock_input:
            _launch_room_browser(ctx)

        assert any("Press RETURN" in s for s in written)
        mock_input.assert_called_once()
