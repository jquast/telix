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
            args=[], returncode=1, stderr=b"NameError: something\n",
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


class TestLaunchChatViewerError:
    @pytest.mark.usefixtures("_stub_terminal")
    def test_error_prompt_on_nonzero(self, monkeypatch: Any) -> None:
        from telix.client_repl_dialogs import _launch_chat_viewer

        fail_result = subprocess.CompletedProcess(
            args=[], returncode=1, stderr=b"Error\n",
        )
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

        fail_result = subprocess.CompletedProcess(
            args=[], returncode=1, stderr=b"Error\n",
        )
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
