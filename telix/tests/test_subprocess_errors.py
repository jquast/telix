"""Tests for subprocess crash file handling."""

from __future__ import annotations

import os
import sys
import json
import tempfile
import subprocess
from unittest.mock import MagicMock

import pytest

from telix.client_tui_base import write_crash_file
from telix.session_context import TelixSessionContext
from telix.client_repl_dialogs import (
    read_crash_file,
    handle_crash_file,
    launch_tui_editor,
    launch_chat_viewer,
    format_crash_banner,
    launch_room_browser,
    launch_unified_editor,
    most_recent_channel,
)


@pytest.fixture()
def stub_terminal(monkeypatch):
    """Stub terminal helpers so subprocess launchers don't touch the real TTY."""
    monkeypatch.setattr("telix.client_repl_dialogs.get_logfile_path", lambda: "")
    monkeypatch.setattr("telix.client_repl.restore_after_subprocess", lambda buf: None)
    monkeypatch.setattr("telix.client_repl.terminal_cleanup", lambda: "")
    monkeypatch.setattr(
        "telix.client_repl.get_term", lambda: MagicMock(change_scroll_region=MagicMock(return_value=""), height=24)
    )
    monkeypatch.setattr(
        "telix.client_repl.blocking_fds",
        MagicMock(
            return_value=MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))
        ),
    )


def make_ctx():
    ctx = TelixSessionContext(session_key="host:1234")
    ctx.macros_file = "/tmp/macros.json"
    ctx.autoreplies_file = "/tmp/autoreplies.json"
    ctx.highlights_file = "/tmp/highlights.json"
    ctx.chat_file = "/tmp/chat.json"
    ctx.chat_messages = [{"channel": "ooc", "text": "hi"}]
    ctx.rooms_file = "/tmp/rooms.db"
    ctx.current_room_file = "/tmp/current_room"
    return ctx


class TestWriteCrashFile:
    def test_writes_and_reads_json(self):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="crash-test-")
        os.close(fd)
        try:
            write_crash_file(path, "Traceback ...\nNameError: x", "exception")
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            assert data["traceback"] == "Traceback ...\nNameError: x"
            assert data["pid"] == os.getpid()
            assert data["source"] == "exception"
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class TestReadCrashFile:
    def test_returns_data(self):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="crash-test-")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"traceback": "tb", "pid": 42, "source": "exception"}, fh)
            result = read_crash_file(path)
            assert result == {"traceback": "tb", "pid": 42, "source": "exception"}
            assert os.path.exists(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_missing_file_returns_none(self):
        assert read_crash_file("/tmp/nonexistent-crash-file.json") is None


class TestFormatCrashBanner:
    def test_contains_command_traceback_and_bookends(self):
        data = {"traceback": "Traceback (most recent call last):\n  NameError: x", "pid": 12345}
        cmd = [sys.executable, "-c", "import sys"]
        banner = format_crash_banner(data, cmd, "/tmp/crash-test.json", 1)
        assert "NameError: x" in banner
        assert "import sys" in banner
        assert "exit code=1" in banner
        assert "/tmp/crash-test.json" in banner
        assert "\r\n" in banner


class TestHandleCrashFile:
    def test_injects_into_replay_buf_and_preserves_file(self):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="crash-test-")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"traceback": "NameError: x", "pid": 99, "source": "exception"}, fh)
            replay_buf = []
            result = subprocess.CompletedProcess(args=[], returncode=1)
            handle_crash_file(path, ["python", "-c", "pass"], replay_buf, result)
            assert len(replay_buf) == 1
            assert b"NameError: x" in replay_buf[0]
            assert os.path.exists(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_noop_on_success(self):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="crash-test-")
        os.close(fd)
        replay_buf = []
        result = subprocess.CompletedProcess(args=[], returncode=0)
        handle_crash_file(path, ["python"], replay_buf, result)
        assert replay_buf == []
        assert not os.path.exists(path)


class TestLauncherCrashEnv:
    @pytest.mark.usefixtures("stub_terminal")
    @pytest.mark.parametrize(
        "launcher,args",
        [
            (launch_tui_editor, ("macros", make_ctx())),
            (launch_room_browser, (make_ctx(),)),
            (launch_chat_viewer, (make_ctx(),)),
        ],
    )
    def test_launcher_passes_crash_env(self, monkeypatch, launcher, args):
        captured_kw = {}
        ok_result = subprocess.CompletedProcess(args=[], returncode=0)

        def fake_run(cmd, check=False, **kw):
            captured_kw.update(kw)
            return ok_result

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("telix.client_repl_dialogs.read_fasttravel", lambda p: ([], False))

        monkeypatch.setattr("sys.stdout", MagicMock(write=MagicMock(), flush=MagicMock()))
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr("sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False)))

        ctx_arg = [a for a in args if isinstance(a, TelixSessionContext)]
        if ctx_arg:
            ctx_arg[0].room_graph = None

        launcher(*args)
        assert "env" in captured_kw
        assert "TELIX_CRASH_FILE" in captured_kw["env"]

    @pytest.mark.usefixtures("stub_terminal")
    def test_unified_editor_passes_crash_env(self, monkeypatch):
        captured_kw = {}
        ok_result = subprocess.CompletedProcess(args=[], returncode=0)

        def fake_run(cmd, check=False, **kw):
            captured_kw.update(kw)
            return ok_result

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("telix.client_repl_dialogs.read_fasttravel", lambda p: ([], False))

        monkeypatch.setattr("sys.stdout", MagicMock(write=MagicMock(), flush=MagicMock()))
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr("sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False)))

        ctx = make_ctx()
        ctx.room_graph = None
        launch_unified_editor("macros", ctx)
        assert "env" in captured_kw
        assert "TELIX_CRASH_FILE" in captured_kw["env"]


class TestNoPromptOnSuccess:
    @pytest.mark.usefixtures("stub_terminal")
    def test_no_crash_banner_on_success(self, monkeypatch):
        ok_result = subprocess.CompletedProcess(args=[], returncode=0)
        monkeypatch.setattr("subprocess.run", lambda cmd, check=False, **kw: ok_result)
        monkeypatch.setattr("os.path.exists", lambda p: False)

        written = []
        monkeypatch.setattr("sys.stdout", MagicMock(write=written.append, flush=MagicMock()))
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr("sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False)))

        launch_tui_editor("macros", make_ctx())
        assert not any("crashed" in str(s) for s in written)


class TestMostRecentChannel:
    def test_empty_returns_empty(self):
        assert most_recent_channel([], {}) == ""

    def test_chat_only(self):
        msgs = [{"ts": "2024-01-01T10:00:00", "channel": "gossip"}]
        assert most_recent_channel(msgs, {}) == "gossip"

    def test_capture_only(self):
        cap = {"tells": [{"ts": "2024-01-01T11:00:00"}]}
        assert most_recent_channel([], cap) == "tells"

    def test_capture_newer_than_chat(self):
        msgs = [{"ts": "2024-01-01T10:00:00", "channel": "gossip"}]
        cap = {"tells": [{"ts": "2024-01-01T11:00:00"}]}
        assert most_recent_channel(msgs, cap) == "tells"

    def test_chat_newer_than_capture(self):
        msgs = [{"ts": "2024-01-01T12:00:00", "channel": "gossip"}]
        cap = {"tells": [{"ts": "2024-01-01T11:00:00"}]}
        assert most_recent_channel(msgs, cap) == "gossip"

    def test_multiple_capture_channels(self):
        cap = {
            "tells": [{"ts": "2024-01-01T10:00:00"}],
            "ooc": [{"ts": "2024-01-01T12:00:00"}],
        }
        assert most_recent_channel([], cap) == "ooc"

    def test_no_ts_fields(self):
        msgs = [{"channel": "gossip"}]
        cap = {"tells": [{}]}
        result = most_recent_channel(msgs, cap)
        assert result in ("gossip", "tells", "")
