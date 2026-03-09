"""Tests for TUI thread launcher helpers and channel utilities."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from telix.session_context import TelixSessionContext
from telix.client_repl_dialogs import (
    launch_tui_editor,
    launch_chat_viewer,
    launch_room_browser,
    most_recent_channel,
    launch_unified_editor,
)


@pytest.fixture()
def stub_terminal(monkeypatch):
    """Stub terminal helpers and _run_in_thread so launchers don't touch the real TTY."""
    monkeypatch.setattr("telix.client_repl.restore_after_subprocess", lambda buf: None)
    monkeypatch.setattr("telix.client_repl.terminal_cleanup", lambda: "")
    monkeypatch.setattr(
        "telix.client_repl.get_term", lambda: MagicMock(change_scroll_region=MagicMock(return_value=""), height=24)
    )
    monkeypatch.setattr("telix.client_repl_dialogs._run_in_thread", lambda t, **kw: None)


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


class TestLauncherCallsThread:
    """Launchers delegate to _run_in_thread instead of subprocess.run."""

    @pytest.mark.usefixtures("stub_terminal")
    @pytest.mark.parametrize(
        "launcher,args",
        [
            (launch_tui_editor, ("macros", make_ctx())),
            (launch_room_browser, (make_ctx(),)),
            (launch_chat_viewer, (make_ctx(),)),
        ],
    )
    def test_launcher_calls_run_in_thread(self, monkeypatch, launcher, args):
        called = []

        def fake_run_in_thread(target, **kw):
            called.append(target)

        monkeypatch.setattr("telix.client_repl_dialogs._run_in_thread", fake_run_in_thread)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("telix.client_repl_dialogs.read_fasttravel", lambda p: ([], False))
        monkeypatch.setattr("sys.stdout", MagicMock(write=MagicMock(), flush=MagicMock()))
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr("sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False)))

        ctx_arg = [a for a in args if isinstance(a, TelixSessionContext)]
        if ctx_arg:
            ctx_arg[0].room_graph = None

        launcher(*args)
        assert len(called) == 1
        assert callable(called[0])

    @pytest.mark.usefixtures("stub_terminal")
    def test_unified_editor_calls_run_in_thread(self, monkeypatch):
        called = []

        def fake_run_in_thread(target, **kw):
            called.append(target)

        monkeypatch.setattr("telix.client_repl_dialogs._run_in_thread", fake_run_in_thread)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("telix.client_repl_dialogs.read_fasttravel", lambda p: ([], False))
        monkeypatch.setattr("sys.stdout", MagicMock(write=MagicMock(), flush=MagicMock()))
        monkeypatch.setattr("sys.stderr", MagicMock(flush=MagicMock()))
        monkeypatch.setattr("sys.__stderr__", MagicMock(flush=MagicMock(), isatty=MagicMock(return_value=False)))

        ctx = make_ctx()
        ctx.room_graph = None
        launch_unified_editor("macros", ctx)
        assert len(called) == 1
        assert callable(called[0])


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
        cap = {"tells": [{"ts": "2024-01-01T10:00:00"}], "ooc": [{"ts": "2024-01-01T12:00:00"}]}
        assert most_recent_channel([], cap) == "ooc"

    def test_no_ts_fields(self):
        msgs = [{"channel": "gossip"}]
        cap = {"tells": [{}]}
        result = most_recent_channel(msgs, cap)
        assert result in ("gossip", "tells", "")
