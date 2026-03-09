"""Tests for confirm dialog: thread-based launcher and in-app Textual screen."""

from __future__ import annotations

# std imports
import json
from typing import Any
from unittest.mock import MagicMock

# 3rd party
import pytest

# local
from telix.client_repl import confirm_dialog


@pytest.fixture()
def mock_thread(monkeypatch: Any) -> Any:
    """Stub _run_in_thread and run_confirm_dialog to write a result file without launching Textual."""
    result_data: dict[str, bool] = {"confirmed": False}

    class Holder:
        data = result_data

    def fake_run_confirm(title: str, body: str, warning: str = "", result_file: str = "") -> None:
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(Holder.data, f)

    monkeypatch.setattr("telix.client_tui_dialogs.run_confirm_dialog", fake_run_confirm)
    monkeypatch.setattr("telix.client_repl_dialogs._run_in_thread", lambda t, **kw: t())
    monkeypatch.setattr("telix.client_repl.restore_after_subprocess", lambda buf: None)
    monkeypatch.setattr("telix.client_repl.terminal_cleanup", lambda: "")
    monkeypatch.setattr(
        "telix.client_repl.get_term", lambda: MagicMock(change_scroll_region=MagicMock(return_value=""), height=24)
    )
    return Holder


def test_cancel_returns_false(mock_thread: Any) -> None:
    mock_thread.data = {"confirmed": False}
    ok = confirm_dialog("Test", "body")
    assert ok is False


def test_confirmed_returns_true(mock_thread: Any) -> None:
    mock_thread.data = {"confirmed": True}
    ok = confirm_dialog("Test", "body")
    assert ok is True


def test_warning_passed_in_command(mock_thread: Any) -> None:
    mock_thread.data = {"confirmed": False}
    ok = confirm_dialog("Test", "body", warning="Danger!")
    assert ok is False


pytest.importorskip("textual")

# 3rd party
from textual.app import App, ComposeResult
from textual.widgets import Button

# local
from telix.client_tui import ConfirmDialogScreen


class ConfirmTestApp(App[bool]):
    """Minimal app that pushes a ConfirmDialogScreen on mount."""

    def __init__(self, **dialog_kwargs: Any) -> None:
        super().__init__()
        self.dialog_kwargs = dialog_kwargs
        self.result: bool | None = None

    def compose(self) -> ComposeResult:
        yield Button("placeholder")

    def on_mount(self) -> None:
        self.push_screen(ConfirmDialogScreen(**self.dialog_kwargs), callback=self.do_result)

    def do_result(self, value: bool) -> None:
        self.result = value
        self.exit(value)


@pytest.mark.asyncio()
async def test_dismiss_true_on_ok() -> None:
    app = ConfirmTestApp(title="Delete?", body="Really delete?")
    async with app.run_test() as pilot:
        await pilot.click("#confirm-ok")
        assert app.result is True


@pytest.mark.asyncio()
async def test_dismiss_false_on_cancel() -> None:
    app = ConfirmTestApp(title="Delete?", body="Really delete?")
    async with app.run_test() as pilot:
        await pilot.click("#confirm-cancel")
        assert app.result is False


@pytest.mark.asyncio()
async def test_dismiss_false_on_escape() -> None:
    app = ConfirmTestApp(title="Delete?", body="Really delete?")
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app.result is False
