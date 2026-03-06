"""Tests for telix.terminal (platform dispatcher) and telix.terminal_unix."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

if sys.platform == "win32":
    pytest.skip("POSIX-only tests", allow_module_level=True)

from telix import terminal, terminal_unix


def test_get_terminal_size_fallback(monkeypatch):
    """Falls back to LINES/COLUMNS env vars when ioctl raises OSError."""
    monkeypatch.setenv("LINES", "30")
    monkeypatch.setenv("COLUMNS", "100")
    with patch("fcntl.ioctl", side_effect=OSError("no tty")):
        rows, cols = terminal_unix.get_terminal_size()
    assert rows == 30
    assert cols == 100


def test_get_terminal_size_fallback_defaults(monkeypatch):
    """Falls back to 25x80 when env vars are absent."""
    monkeypatch.delenv("LINES", raising=False)
    monkeypatch.delenv("COLUMNS", raising=False)
    with patch("fcntl.ioctl", side_effect=OSError("no tty")):
        rows, cols = terminal_unix.get_terminal_size()
    assert rows == 25
    assert cols == 80


def test_blocking_fds_restores_on_exception(monkeypatch):
    """blocking_fds restores the original blocking state when the block raises."""
    restored = {}

    def fake_get_blocking(fd):
        return False

    def fake_set_blocking(fd, val):
        restored[fd] = val

    monkeypatch.setattr("os.get_blocking", fake_get_blocking)
    monkeypatch.setattr("os.set_blocking", fake_set_blocking)

    with pytest.raises(RuntimeError):
        with terminal_unix.blocking_fds():
            raise RuntimeError("inner")

    # Should have restored all 3 fds back to False (original state)
    assert restored.get(0) is False
    assert restored.get(1) is False
    assert restored.get(2) is False


def test_blocking_fds_sets_blocking_on_entry(monkeypatch):
    """blocking_fds sets non-blocking fds to blocking on entry."""
    set_calls = {}
    monkeypatch.setattr("os.get_blocking", lambda fd: False)
    monkeypatch.setattr("os.set_blocking", lambda fd, val: set_calls.update({fd: val}))
    with terminal_unix.blocking_fds():
        assert set_calls.get(0) is True
        assert set_calls.get(1) is True
        assert set_calls.get(2) is True


def test_flush_stdin_no_crash(monkeypatch):
    """flush_stdin does not raise even when tcflush is unavailable."""
    import termios

    monkeypatch.setattr(termios, "tcflush", lambda fd, how: (_ for _ in ()).throw(termios.error("fail")))
    terminal_unix.flush_stdin()  # should not raise


def test_set_blocking_stdout_returns_previous(monkeypatch):
    """set_blocking_stdout returns the previous blocking state."""
    monkeypatch.setattr("os.get_blocking", lambda fd: True)
    monkeypatch.setattr("os.set_blocking", lambda fd, val: None)
    was = terminal_unix.set_blocking_stdout(False)
    assert was is True


def test_terminal_dispatcher_exposes_functions():
    """The terminal module exposes all required platform functions."""
    for name in (
        "get_terminal_size",
        "blocking_fds",
        "set_blocking_stdout",
        "restore_io_blocking",
        "flush_stdin",
        "restore_opost",
        "pause_before_exit",
        "restore_blocking_fds",
    ):
        assert callable(getattr(terminal, name)), f"terminal.{name} is not callable"
