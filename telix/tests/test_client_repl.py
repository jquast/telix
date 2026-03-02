"""Tests for telix.client_repl and client_shell.ScrollRegion."""

from __future__ import annotations

# std imports
import os
import sys
import time
import types
import asyncio
import logging
import threading
from typing import Any

# 3rd party
import pytest
from blessed.line_editor import LineEditor, LineHistory
from telnetlib3.client_shell import Terminal

if sys.platform == "win32":
    pytest.skip("POSIX-only tests", allow_module_level=True)

# local
from telix.paths import DATA_DIR, history_path
from telix.macros import Macro, load_macros, save_macros
from telix.autoreply import SearchBuffer, AutoreplyEngine
from telix.client_repl import (
    TRAVEL_RE,
    STYLE_NORMAL,
    COMMAND_DELAY,
    PASSWORD_CHAR,
    STYLE_AUTOREPLY,
    ReplSession,
    ScrollRegion,
    LineHoldBuffer,
    get_term,
    fmt_value,
    randomwalk,
    make_styles,
    autodiscover,
    load_history,
    send_chained,
    collapse_runs,
    repl_scaffold,
    expand_commands,
    save_history_entry,
    render_command_queue,
    split_incomplete_esc,
    handle_travel_commands,
)
from telix.session_context import CommandQueue, SessionContext
from telix.client_repl_render import SEXTANT, SCRAMBLE_LEN, idle_rgb, idle_ar_rgb, scramble_password
from telix.client_repl_travel import MAX_STUCK_RETRIES
from telix.client_repl_commands import (
    StepResult,
    DispatchHooks,
    dispatch_one,
    active_cmd_fg,
    pending_cmd_rgb,
    expand_commands_ex,
)


class MockTransport:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closing = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    def is_closing(self) -> bool:
        return self.closing


def mock_stdout() -> asyncio.StreamWriter:
    transport = MockTransport()
    writer = types.SimpleNamespace(write=transport.write)
    return writer, transport  # type: ignore[return-value]


def mock_writer(will_echo: bool = False) -> object:
    return types.SimpleNamespace(
        will_echo=will_echo,
        log=types.SimpleNamespace(debug=lambda *a, **kw: None),
        get_extra_info=lambda name, default=None: default,
        set_iac_callback=lambda cmd, func: None,
    )


def test_scroll_region_rows_property() -> None:
    stdout, _ = mock_stdout()
    sr = ScrollRegion(stdout, rows=24, cols=80, reserve_bottom=1)
    assert sr.scroll_rows == 21


def test_scroll_region_rows_minimum() -> None:
    stdout, _ = mock_stdout()
    sr = ScrollRegion(stdout, rows=1, cols=80, reserve_bottom=1)
    assert sr.scroll_rows == 0


def test_scroll_region_input_row() -> None:
    stdout, _ = mock_stdout()
    sr = ScrollRegion(stdout, rows=24, cols=80)
    assert sr.input_row == 23


def test_scroll_region_input_row_reserve_2() -> None:
    stdout, _ = mock_stdout()
    sr = ScrollRegion(stdout, rows=24, cols=80, reserve_bottom=2)
    assert sr.scroll_rows == 20
    assert sr.input_row == 22


def test_scroll_region_decstbm_enter_exit() -> None:
    pytest.importorskip("blessed")
    stdout, transport = mock_stdout()
    with ScrollRegion(stdout, rows=24, cols=80, reserve_bottom=1) as sr:
        assert sr.active
    data_on_exit = bytes(transport.data)
    assert len(data_on_exit) > 0


def test_scroll_region_update_size() -> None:
    pytest.importorskip("blessed")
    stdout, transport = mock_stdout()
    with ScrollRegion(stdout, rows=24, cols=80, reserve_bottom=1) as sr:
        transport.data.clear()
        sr.update_size(30, 120)
        assert sr.scroll_rows == 27
        data = bytes(transport.data)
        assert len(data) > 0


def test_scroll_region_update_size_inactive() -> None:
    pytest.importorskip("blessed")
    stdout, transport = mock_stdout()
    sr = ScrollRegion(stdout, rows=24, cols=80, reserve_bottom=1)
    transport.data.clear()
    sr.update_size(30, 120)
    assert bytes(transport.data) == b""


def test_scroll_region_grow_reserve_emits_newlines() -> None:
    pytest.importorskip("blessed")
    stdout, transport = mock_stdout()
    with ScrollRegion(stdout, rows=24, cols=80, reserve_bottom=1) as sr:
        assert sr.scroll_rows == 21
        transport.data.clear()
        sr.grow_reserve(2)
        assert sr.scroll_rows == 20
        data = bytes(transport.data)
        assert b"\n" in data


def test_scroll_region_grow_reserve_noop_if_smaller() -> None:
    pytest.importorskip("blessed")
    stdout, transport = mock_stdout()
    with ScrollRegion(stdout, rows=24, cols=80, reserve_bottom=2) as sr:
        transport.data.clear()
        sr.grow_reserve(1)
        assert sr.scroll_rows == 20
        assert bytes(transport.data) == b""


def test_scroll_region_save_and_goto_input() -> None:
    pytest.importorskip("blessed")
    stdout, transport = mock_stdout()
    sr = ScrollRegion(stdout, rows=24, cols=80)
    transport.data.clear()
    sr.save_and_goto_input()
    data = bytes(transport.data)
    assert len(data) > 0


def test_scroll_region_restore_cursor() -> None:
    pytest.importorskip("blessed")
    stdout, transport = mock_stdout()
    sr = ScrollRegion(stdout, rows=24, cols=80)
    transport.data.clear()
    sr.restore_cursor()
    assert len(bytes(transport.data)) > 0


def test_scramble_password_empty_buf_no_replace() -> None:
    """str.replace with empty search inserts between every char; guard against it."""

    raw = "\x1b[45;1H\x1b[48;2;26;0;0m\x1b[0m" + (" " * 80)
    search = PASSWORD_CHAR * 0
    result = raw.replace(search, scramble_password())
    assert len(result) > len(raw) * 10

    guarded_raw = raw
    buf_len = 0
    if buf_len:
        guarded_raw = raw.replace(PASSWORD_CHAR * buf_len, scramble_password())
    assert guarded_raw == raw


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_adjusted_naws_active_scroll() -> None:
    pytest.importorskip("blessed")

    writer = mock_writer()
    writer.handle_send_naws = lambda: (24, 80)
    writer.local_option = types.SimpleNamespace(enabled=lambda _: False)
    writer.is_closing = lambda: False

    stdout, _ = mock_stdout()
    term = types.SimpleNamespace(on_resize=None)

    async with repl_scaffold(writer, term, stdout) as (scroll, _):
        result = writer.handle_send_naws()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result[0] == scroll.scroll_rows


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_adjusted_naws_inactive_returns_terminal_size() -> None:
    pytest.importorskip("blessed")

    writer = mock_writer()
    writer.handle_send_naws = lambda: (24, 80)
    writer.local_option = types.SimpleNamespace(enabled=lambda _: False)
    writer.is_closing = lambda: False

    stdout, _ = mock_stdout()
    term = types.SimpleNamespace(on_resize=None)

    patched_naws = None
    async with repl_scaffold(writer, term, stdout) as (scroll, _):
        patched_naws = writer.handle_send_naws
    result = patched_naws()
    assert isinstance(result, tuple)
    assert len(result) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_naws_restored_on_exception() -> None:
    """handle_send_naws is restored even if repl_scaffold body raises."""
    pytest.importorskip("blessed")

    def orig_handler() -> tuple[int, int]:
        return (24, 80)

    writer = mock_writer()
    writer.handle_send_naws = orig_handler
    writer.local_option = types.SimpleNamespace(enabled=lambda _: False)
    writer.is_closing = lambda: False

    stdout, _ = mock_stdout()
    term = types.SimpleNamespace(on_resize=None)

    with pytest.raises(RuntimeError, match="injected"):
        async with repl_scaffold(writer, term, stdout):
            raise RuntimeError("injected")

    assert writer.handle_send_naws is orig_handler


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_naws_restored_on_normal_exit() -> None:
    """handle_send_naws is restored after normal scaffold exit."""
    pytest.importorskip("blessed")

    def orig_handler() -> tuple[int, int]:
        return (24, 80)

    writer = mock_writer()
    writer.handle_send_naws = orig_handler
    writer.local_option = types.SimpleNamespace(enabled=lambda _: False)
    writer.is_closing = lambda: False

    stdout, _ = mock_stdout()
    term = types.SimpleNamespace(on_resize=None)

    async with repl_scaffold(writer, term, stdout) as (scroll, rc):
        assert writer.handle_send_naws is not orig_handler

    assert writer.handle_send_naws is orig_handler


@pytest.mark.parametrize(
    "line,expected",
    [
        ("5e", ["e"] * 5),
        ("3north", ["north"] * 3),
        ("5east", ["east"] * 5),
        ("6e;9n;rocks", ["e"] * 6 + ["n"] * 9 + ["rocks"]),
        ("look", ["look"]),
        ("n;e;s;w", ["n", "e", "s", "w"]),
        ("2n;look;3s", ["n", "n", "look", "s", "s", "s"]),
        ("42", ["42"]),
        ("100", ["100"]),
        ("2 apples", ["2 apples"]),
        ("", []),
        ("`travel 42`", ["`travel 42`"]),
        ("look;`delay 1s`;north", ["look", "`delay 1s`", "north"]),
        ("`randomwalk`", ["`randomwalk`"]),
        ("3e;`travel 99`", ["e", "e", "e", "`travel 99`"]),
        ("a ; b", ["a", "b"]),
        ("a;\nb", ["a", "b"]),
        ("a\n;\nb", ["a", "b"]),
        ("  a  ;  b  ;  c  ", ["a", "b", "c"]),
        ("3n\n;\n2e", ["n", "n", "n", "e", "e"]),
        (r"say hey \;)", ["say hey ;)"]),
        (r"say whats up \|", ["say whats up |"]),
        (r"say a\;b;look", ["say a;b", "look"]),
        (r"say a\|b|look", ["say a|b", "look"]),
        (r"say \\o/", [r"say \o/"]),
    ],
)
def test_expand_commands(line: str, expected: list[str]) -> None:
    assert expand_commands(line) == expected


@pytest.mark.parametrize(
    "line,expected_cmds,expected_imm",
    [
        ("a;b", ["a", "b"], frozenset()),
        ("a|b", ["a", "b"], frozenset({1})),
        ("a|b;c", ["a", "b", "c"], frozenset({1})),
        ("a;b|c|d;e", ["a", "b", "c", "d", "e"], frozenset({2, 3})),
        ("`until 4 foo:bar`|attack", ["`until 4 foo:bar`", "attack"], frozenset({1})),
        ("`delay 1s`|look", ["`delay 1s`", "look"], frozenset({1})),
        ("3e|look", ["e", "e", "e", "look"], frozenset({3})),
        ("a|b|c", ["a", "b", "c"], frozenset({1, 2})),
        ("cast heal|`when HP%>=100`", ["cast heal", "`when HP%>=100`"], frozenset({1})),
        ("", [], frozenset()),
        (r"say \;|look", ["say ;", "look"], frozenset({1})),
        (r"say \||look", ["say |", "look"], frozenset({1})),
    ],
)
def test_expand_commands_ex(line: str, expected_cmds: list[str], expected_imm: frozenset[int]) -> None:
    result = expand_commands_ex(line)
    assert result.commands == expected_cmds
    assert result.immediate_set == expected_imm


def test_expand_commands_with_pipe_separator() -> None:
    assert expand_commands("a|b") == ["a", "b"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (1500, "1.5k"),
        (12345, "12.3k"),
        (999900, "999.9k"),
        (1000000, "1.0m"),
        (1500000, "1.5m"),
        (123456789, "123.5m"),
    ],
)
def test_fmt_value(value: int, expected: str) -> None:
    assert fmt_value(value) == expected


@pytest.mark.parametrize(
    "data, flush, hold",
    [
        (b"", b"", b""),
        (b"hello", b"hello", b""),
        (b"\x1b[32mgreen\x1b[0m", b"\x1b[32mgreen\x1b[0m", b""),
        (b"text\x1b", b"text", b"\x1b"),
        (b"text\x1b[", b"text", b"\x1b["),
        (b"text\x1b[1", b"text", b"\x1b[1"),
        (b"text\x1b[1;33", b"text", b"\x1b[1;33"),
        (b"text\x1b[1;33;48;2;255;128;0", b"text", b"\x1b[1;33;48;2;255;128;0"),
        (b"text\x1b[1;33m", b"text\x1b[1;33m", b""),
        (b"\x1b[1m\x1b", b"\x1b[1m", b"\x1b"),
        (b"\x1b[1m\x1b[32m", b"\x1b[1m\x1b[32m", b""),
        (b"text\x1b]8;;http://x", b"text", b"\x1b]8;;http://x"),
        (b"text\x1b]8;;\x07", b"text\x1b]8;;\x07", b""),
        (b"text\x1b]0;title\x1b\\", b"text\x1b]0;title\x1b\\", b""),
        (b"text\x1bP0;1|data", b"text", b"\x1bP0;1|data"),
        (b"text\x1bP0;1|data\x1b\\", b"text\x1bP0;1|data\x1b\\", b""),
        (b"text\x1b7", b"text\x1b7", b""),
        (b"text\x1b(B", b"text\x1b(B", b""),
    ],
)
def test_split_incomplete_esc(data: bytes, flush: bytes, hold: bytes) -> None:
    got_flush, got_hold = split_incomplete_esc(data)
    assert got_flush == flush
    assert got_hold == hold
    assert got_flush + got_hold == data


@pytest.mark.parametrize(
    "cmd, match",
    [
        ("`travel 123`", True),
        ("`travel 456 noreply`", True),
        ("`return`", True),
        ("`return noreply`", True),
        ("`Travel 123`", True),
        ("`TRAVEL 789`", True),
        ("`travel`", True),
        ("`randomwalk`", True),
        ("`RANDOMWALK`", True),
        ("`home`", True),
        ("`HOME`", True),
        ("travel 123", False),
        ("`fast travel 123`", False),
        ("north", False),
        ("look", False),
    ],
)
def test_travel_re_matching(cmd: str, match: bool) -> None:
    assert bool(TRAVEL_RE.match(cmd)) is match


def test_style_normal_populated() -> None:
    pytest.importorskip("blessed")
    make_styles()
    assert isinstance(STYLE_NORMAL, dict)
    assert STYLE_NORMAL["text_sgr"] != ""
    assert STYLE_NORMAL["bg_sgr"] != ""
    assert STYLE_NORMAL["suggestion_sgr"] != ""


def test_style_autoreply_populated() -> None:
    pytest.importorskip("blessed")
    make_styles()
    assert isinstance(STYLE_AUTOREPLY, dict)
    assert STYLE_AUTOREPLY["text_sgr"] != ""
    assert STYLE_AUTOREPLY["bg_sgr"] != ""


def test_style_normal_and_autoreply_differ() -> None:
    pytest.importorskip("blessed")
    make_styles()
    assert STYLE_NORMAL["bg_sgr"] != STYLE_AUTOREPLY["bg_sgr"]
    assert STYLE_NORMAL["text_sgr"] != STYLE_AUTOREPLY["text_sgr"]


def test_render_input_line_basic() -> None:
    blessed = pytest.importorskip("blessed")

    term = blessed.Terminal(force_styling=True)
    editor = LineEditor(max_width=80)
    for ch in "hello":
        editor.feed_key(ch)
    output = editor.render(term, row=21, width=80)
    assert "hello" in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_scaffold_resize_handler_updates_scroll() -> None:
    """repl_scaffold resize handler updates scroll region dimensions."""
    pytest.importorskip("blessed")

    writer = mock_writer()
    writer.handle_send_naws = lambda: (24, 80)
    writer.local_option = types.SimpleNamespace(enabled=lambda _: False)
    writer.is_closing = lambda: False

    stdout, transport = mock_stdout()
    term = types.SimpleNamespace(on_resize=None)

    async with repl_scaffold(writer, term, stdout) as (scroll, rc):
        assert term.on_resize is not None
        term.on_resize(30, 120)
        assert rc == [30, 120]
        assert scroll.rows == 30
        assert scroll.cols == 120


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
def test_resize_pending_flag_is_threading_event() -> None:
    """Terminal._resize_pending is a threading.Event (signal-safe)."""

    writer = mock_writer()
    writer.client = True
    writer.remote_option = types.SimpleNamespace(enabled=lambda _: False)
    term = Terminal.__new__(Terminal)
    term.telnet_writer = writer
    term._fileno = 0
    term._istty = False
    term._save_mode = None
    term.software_echo = False
    term._remove_winch = False
    term._resize_pending = threading.Event()
    term.on_resize = None
    term._stdin_transport = None
    assert isinstance(term._resize_pending, threading.Event)
    assert not term._resize_pending.is_set()
    term._resize_pending.set()
    assert term._resize_pending.is_set()
    term._resize_pending.clear()
    assert not term._resize_pending.is_set()


def test_load_history_populates_entries(tmp_path: os.PathLike[str]) -> None:
    pytest.importorskip("blessed")
    hfile = tmp_path / "history"
    hfile.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    history = LineHistory()
    load_history(history, str(hfile))
    assert history.entries == ["alpha", "beta", "gamma"]


def test_save_history_entry_appends(tmp_path: os.PathLike[str]) -> None:
    hfile = tmp_path / "history"
    save_history_entry("first", str(hfile))
    save_history_entry("second", str(hfile))
    lines = hfile.read_text(encoding="utf-8").splitlines()
    assert lines == ["first", "second"]


def test_load_history_missing_file(tmp_path: os.PathLike[str]) -> None:
    pytest.importorskip("blessed")
    history = LineHistory()
    load_history(history, str(tmp_path / "does-not-exist"))
    assert history.entries == []


def test_history_path_per_session() -> None:
    p1 = history_path("mud.example.com:4000")
    p2 = history_path("other.host:23")
    assert p1 != p2
    assert os.path.basename(p1).startswith("history-")
    assert os.path.basename(p2).startswith("history-")
    assert len(os.path.basename(p1).split("-", 1)[1]) == 12


def test_history_path_no_traversal() -> None:
    malicious = "../../etc/passwd:22"
    result = history_path(malicious)
    assert result.startswith(DATA_DIR)
    assert ".." not in os.path.basename(result)


class DynamicRoomContext:
    """SessionContext subclass with property-based current_room_num."""

    def __init__(self, room_num: str, room_sequence: list[str] | None) -> None:
        self.real_ctx = SessionContext(session_key="test")
        self.room_val = room_num
        self.seq_iter = iter(room_sequence) if room_sequence else None

    @property
    def current_room_num(self) -> str:
        if self.seq_iter is not None:
            val = next(self.seq_iter, None)
            if val is not None:
                self.room_val = val
        return self.room_val

    @current_room_num.setter
    def current_room_num(self, value: str) -> None:
        self.room_val = value

    def __getattr__(self, name: str) -> object:
        return getattr(self.real_ctx, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in ("real_ctx", "room_val", "seq_iter"):
            super().__setattr__(name, value)
        elif name == "current_room_num":
            super().__setattr__("room_val", value)
        else:
            setattr(self.real_ctx, name, value)


class WalkWriter:
    """Mock writer for randomwalk / autodiscover tests."""

    def __init__(
        self,
        room_num: str = "room1",
        adj: dict[str, dict[str, str]] | None = None,
        room_sequence: list[str] | None = None,
    ) -> None:
        self.sent: list[str] = []
        self.echo_log: list[str] = []
        self.ctx = DynamicRoomContext(room_num, room_sequence)
        self.ctx.writer = self  # type: ignore[assignment]
        self.ctx.echo_command = self.echo_log.append
        self.ctx.room_arrival_timeout = 0.0
        self.ctx.room_graph = types.SimpleNamespace(
            adj=adj or {},
            rooms={},
            get_room=lambda num: types.SimpleNamespace(name=num),
            find_branches=lambda pos, **kw: [],
            blocked_rooms=lambda: frozenset(),
        )

    def write(self, data: str) -> None:
        self.sent.append(data)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_stuck_room_stops(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """After 3 consecutive failed moves, randomwalk marks exits exhausted and stops."""

    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}}
    writer = WalkWriter(room_num="room1", adj=adj)

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=50)

    retry_msgs = [m for m in writer.echo_log if "temporarily blocked" in m]
    assert len(retry_msgs) == MAX_STUCK_RETRIES
    stop_msgs = [m for m in writer.echo_log if "all exits blocked, stopping" in m]
    assert len(stop_msgs) == 1
    assert not writer.ctx.randomwalk_active


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_retries_when_temporarily_stuck(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """Walker retries after temporary block and continues when exit clears."""

    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}, "room2": {"south": "room1"}}
    seq = (
        ["room1"] * 3  # initial + step 1 fail (current, check)
        + ["room1", "room2", "room2"]  # step 2 after retry: current=room1, check=room2, actual
        + ["room2", "room1", "room1"]  # step 3: south -> room1, actual
        + ["room1", "room2", "room2"]  # step 4: north -> room2, actual
        + ["room2", "room1", "room1"]  # step 5: south -> room1
        + ["room1"] * 30  # padding
    )
    writer = WalkWriter(room_num="room1", adj=adj, room_sequence=seq)

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=5)

    retry_msgs = [m for m in writer.echo_log if "temporarily blocked" in m]
    assert len(retry_msgs) >= 1
    stop_msgs = [m for m in writer.echo_log if "all exits blocked, stopping" in m]
    assert len(stop_msgs) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_resets_stuck_on_success(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """A successful move after a blocked exit continues walking."""

    adj: dict[str, dict[str, str]] = {
        "room1": {"north": "room2", "south": "room3"},
        "room2": {"south": "room1"},
        "room3": {"north": "room1"},
    }
    seq = (
        ["room1"]  # initial read
        + ["room1"] * 31  # fail north
        + ["room3"] * 31  # succeed south -> room3
        + ["room1"] * 31  # succeed north -> room1
        + ["room1"] * 100  # more failures
    )
    writer = WalkWriter(room_num="room1", adj=adj, room_sequence=seq)

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=20)

    no_change_msgs = [m for m in writer.echo_log if "no room change" in m]
    assert len(no_change_msgs) >= 1
    assert ("room1", "north") in writer.ctx.blocked_exits


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_autodiscover_stuck_gateway_stops(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """After 3 failures from the same room, autodiscover stops."""

    adj: dict[str, dict[str, str]] = {
        "room1": {"north": "gw1"},
        "gw1": {"east": "target1"},
        "gw2": {"west": "target2"},
        "gw3": {"south": "target3"},
    }
    writer = WalkWriter(room_num="room1", adj=adj)

    def fake_find_branches(pos: str, **kw: object) -> list[tuple[str, str, str]]:
        return [("gw1", "east", "target1"), ("gw2", "west", "target2"), ("gw3", "south", "target3")]

    writer.ctx.room_graph.find_branches = fake_find_branches
    writer.ctx.room_graph.find_path_with_rooms = lambda src, dst, **kw: [("north", dst)]

    async def fake_fast_travel(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr("telix.client_repl_travel.fast_travel", fake_fast_travel)

    await autodiscover(writer.ctx, logging.getLogger("test"), limit=20)

    stuck_msgs = [m for m in writer.echo_log if "all routes blocked" in m]
    assert len(stuck_msgs) == 1
    assert not writer.ctx.discover_active


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_autodiscover_blocked_edge_avoids_retrying(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """When a path edge is impassable, subsequent gateways behind it are skipped."""

    adj: dict[str, dict[str, str]] = {
        "start": {"portal": "island"},
        "island": {"east": "gw1", "west": "gw2"},
        "gw1": {"north": "t1"},
        "gw2": {"south": "t2"},
    }
    writer = WalkWriter(room_num="start", adj=adj)

    branch_idx = 0

    def fake_find_branches(pos: str, **kw: object) -> list[tuple[str, str, str]]:
        nonlocal branch_idx
        branch_idx += 1
        if branch_idx == 1:
            return [("gw1", "north", "t1"), ("gw2", "south", "t2")]
        return [("gw2", "south", "t2")]

    writer.ctx.room_graph.find_branches = fake_find_branches

    def fake_find_path(src: str, dst: str, **kw: object) -> list[tuple[str, str]] | None:
        if src == "start" and "portal" not in adj.get("start", {}):
            return None
        return [("portal", "island"), ("east", dst)]

    writer.ctx.room_graph.find_path_with_rooms = fake_find_path

    fast_travel_calls = 0

    async def fake_fast_travel(*args: object, **kwargs: object) -> None:
        nonlocal fast_travel_calls
        fast_travel_calls += 1

    monkeypatch.setattr("telix.client_repl_travel.fast_travel", fake_fast_travel)

    await autodiscover(writer.ctx, logging.getLogger("test"), limit=20)

    assert fast_travel_calls == 1
    assert "portal" in adj["start"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_send_chained_mixed_uses_move_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Consecutive identical commands in a mixed list get movement delay pacing."""

    sleep_args: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(duration: float) -> None:
        sleep_args.append(duration)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", tracking_sleep)

    writer = WalkWriter(room_num="room1")
    writer.ctx.wait_for_prompt = None
    writer.ctx.prompt_ready = None
    writer.ctx.room_changed = None

    commands = ["e", "e", "e", "n", "n", "rocks"]
    seq = ["room2", "room3", "room4", "room4a", "room4b", "room4c"]
    writer.ctx.seq_iter = iter(seq)

    await send_chained(commands, writer.ctx, logging.getLogger("test"))

    assert len(writer.sent) == 5
    move_delays = [d for d in sleep_args if d == COMMAND_DELAY]
    assert len(move_delays) >= 3


@pytest.mark.asyncio
async def test_send_chained_repeated_non_move(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated non-movement commands all execute with delay pacing."""

    sleep_args: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(duration: float) -> None:
        sleep_args.append(duration)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", tracking_sleep)

    writer = WalkWriter(room_num="room1")
    writer.ctx.wait_for_prompt = None
    writer.ctx.prompt_ready = None
    writer.ctx.room_changed = None

    commands = ["buy coffee"] * 5
    await send_chained(commands, writer.ctx, logging.getLogger("test"))

    sent = [s.strip() for s in writer.sent]
    assert sent.count("buy coffee") == 4
    delays = [d for d in sleep_args if d == COMMAND_DELAY]
    assert len(delays) >= 3


def test_collapse_runs_basic() -> None:
    """Consecutive identical commands are collapsed into count×cmd groups."""

    result = collapse_runs(["e", "e", "e", "n", "n", "rocks"])
    assert result == [("3\u00d7e", 0, 2), ("2\u00d7n", 3, 4), ("rocks", 5, 5)]


def test_collapse_runs_single() -> None:
    """A single command produces one entry with no count prefix."""

    result = collapse_runs(["look"])
    assert result == [("look", 0, 0)]


def test_collapse_runs_all_same() -> None:
    """All-identical list collapses to one group."""

    result = collapse_runs(["e", "e", "e", "e"])
    assert result == [("4\u00d7e", 0, 3)]


def test_collapse_runs_with_start() -> None:
    """Collapsing from a non-zero start skips earlier entries."""

    result = collapse_runs(["e", "e", "e", "n", "n", "rocks"], start=3)
    assert result == [("2\u00d7n", 3, 4), ("rocks", 5, 5)]


def test_collapse_runs_empty_start_past_end() -> None:
    """Start index beyond commands returns empty list."""

    assert collapse_runs(["e", "n"], start=5) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_send_chained_queue_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting cancelled on a CommandQueue stops send_chained early."""

    real_sleep = asyncio.sleep

    async def fast_sleep(duration: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    writer = WalkWriter(room_num="room1")
    writer.ctx.wait_for_prompt = None
    writer.ctx.prompt_ready = None
    writer.ctx.room_changed = None

    commands = ["e", "e", "e", "e", "e"]
    seq = ["room2", "room3", "room4", "room5", "room6"]
    writer.ctx.seq_iter = iter(seq)

    render_calls: list[int] = []
    queue = CommandQueue(commands, render=lambda: render_calls.append(1))

    orig_render = queue.render

    def cancel_after_two() -> None:
        orig_render()
        if queue.current_idx >= 2:
            queue.cancelled = True
            queue.cancel_event.set()

    queue.render = cancel_after_two

    await send_chained(commands, writer.ctx, logging.getLogger("test"), queue=queue)

    assert len(writer.sent) <= 2
    assert queue.cancelled
    assert len(render_calls) >= 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_send_chained_delay_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backtick delay commands in a chained sequence pause without sending."""

    sleep_args: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(duration: float) -> None:
        sleep_args.append(duration)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", tracking_sleep)

    writer = WalkWriter(room_num="room1")
    writer.ctx.wait_for_prompt = None
    writer.ctx.prompt_ready = None
    writer.ctx.room_changed = None

    commands = ["look", "`delay 2s`", "north"]

    await send_chained(commands, writer.ctx, logging.getLogger("test"))

    assert len(writer.sent) == 1
    assert writer.sent[0] == "north\r\n"
    assert 2.0 in sleep_args


def dispatch_hooks(**overrides: Any) -> tuple[Any, list[str], list[str]]:
    """Build a DispatchHooks with recording send/echo and return (hooks, sent, echoed)."""

    sent: list[str] = []
    echoed: list[str] = []
    status: list[str] = []
    defaults = {
        "ctx": types.SimpleNamespace(gmcp_data={}, captures={}, autoreply_engine=None),
        "log": __import__("logging").getLogger("test"),
        "wait_fn": None,
        "send_fn": sent.append,
        "echo_fn": echoed.append,
        "on_status": status.append,
        "prompt_ready": None,
    }
    defaults.update(overrides)
    hooks = DispatchHooks(**defaults)
    return hooks, sent, echoed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd, expected_result", [("`delay 0ms`", "HANDLED"), ("`delay 1s`", "HANDLED"), ("`delay 500ms`", "HANDLED")]
)
async def test_dispatch_one_delay(monkeypatch: pytest.MonkeyPatch, cmd: str, expected_result: str) -> None:
    """Delay commands return HANDLED."""

    sleep_args: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(duration: float) -> None:
        sleep_args.append(duration)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", tracking_sleep)
    hooks, sent, _ = dispatch_hooks()
    result = await dispatch_one(cmd, 0, 0, frozenset(), hooks)
    assert result is StepResult[expected_result]
    assert not sent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_delay_calls_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delay command calls on_progress and on_progress_clear."""

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda d: real_sleep(0))
    progress_calls: list[tuple[float, float]] = []
    clear_calls: list[int] = []
    hooks, _, _ = dispatch_hooks(
        on_progress=lambda s, d: progress_calls.append((s, d)), on_progress_clear=lambda: clear_calls.append(1)
    )
    result = await dispatch_one("`delay 2s`", 0, 0, frozenset(), hooks)
    assert result is StepResult.HANDLED
    assert len(progress_calls) == 1
    assert len(clear_calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_when_pass() -> None:
    """When condition that passes returns HANDLED."""

    hooks, sent, _ = dispatch_hooks(
        ctx=types.SimpleNamespace(
            gmcp_data={"Char.Vitals": {"hp": "80", "maxhp": "100"}}, captures={}, autoreply_engine=None
        )
    )
    result = await dispatch_one("`when HP%>=50`", 0, 0, frozenset(), hooks)
    assert result is StepResult.HANDLED
    assert not sent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_when_fail() -> None:
    """When condition that fails returns ABORT."""

    hooks, sent, _ = dispatch_hooks(
        ctx=types.SimpleNamespace(
            gmcp_data={"Char.Vitals": {"hp": "20", "maxhp": "100"}}, captures={}, autoreply_engine=None
        )
    )
    result = await dispatch_one("`when HP%>=50`", 0, 0, frozenset(), hooks)
    assert result is StepResult.ABORT
    assert not sent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_plain_send_first_skips_wait() -> None:
    """First plain send (sent_count=0) does not call wait_fn."""

    wait_calls: list[int] = []

    async def wait() -> None:
        wait_calls.append(1)

    hooks, sent, echoed = dispatch_hooks(wait_fn=wait)
    result = await dispatch_one("look", 0, 0, frozenset(), hooks)
    assert result is StepResult.SENT
    assert sent == ["look"]
    assert echoed == ["look"]
    assert not wait_calls


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_plain_send_subsequent_waits() -> None:
    """Subsequent plain sends (sent_count>0) call wait_fn."""

    wait_calls: list[int] = []

    async def wait() -> None:
        wait_calls.append(1)

    hooks, sent, _ = dispatch_hooks(wait_fn=wait)
    result = await dispatch_one("north", 1, 1, frozenset(), hooks)
    assert result is StepResult.SENT
    assert sent == ["north"]
    assert len(wait_calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_immediate_skips_wait() -> None:
    """Commands in the immediate set skip wait_fn even when sent_count>0."""

    wait_calls: list[int] = []

    async def wait() -> None:
        wait_calls.append(1)

    hooks, sent, _ = dispatch_hooks(wait_fn=wait)
    result = await dispatch_one("south", 2, 1, frozenset({2}), hooks)
    assert result is StepResult.SENT
    assert sent == ["south"]
    assert not wait_calls


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_until_timeout_aborts() -> None:
    """Until command that times out returns ABORT."""

    buf = SearchBuffer(max_lines=100)
    hooks, sent, _ = dispatch_hooks(search_buffer=buf)
    result = await dispatch_one("`until 0.01 nope`", 0, 0, frozenset(), hooks)
    assert result is StepResult.ABORT
    assert not sent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_until_matches() -> None:
    """Until command returns HANDLED when pattern appears."""

    buf = SearchBuffer(max_lines=100)
    hooks, sent, _ = dispatch_hooks(search_buffer=buf)

    async def feed_later() -> None:
        await asyncio.sleep(0.01)
        buf.add_text("the mob died.\n")

    asyncio.ensure_future(feed_later())
    result = await dispatch_one("`until 2 died`", 0, 0, frozenset(), hooks)
    assert result is StepResult.HANDLED
    assert not sent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_untils_case_sensitive_no_match() -> None:
    """Untils (case-sensitive) times out when casing doesn't match."""

    buf = SearchBuffer(max_lines=100)
    hooks, sent, _ = dispatch_hooks(search_buffer=buf)

    async def feed_later() -> None:
        await asyncio.sleep(0.005)
        buf.add_text("dead\n")

    asyncio.ensure_future(feed_later())
    result = await dispatch_one("`untils 0.02 DEAD`", 0, 0, frozenset(), hooks)
    assert result is StepResult.ABORT


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_dispatch_one_mask_send() -> None:
    """When mask_send=True, status shows (masked) instead of command."""

    status_log: list[str] = []
    hooks, sent, _ = dispatch_hooks(on_status=status_log.append)
    result = await dispatch_one("secret", 0, 0, frozenset(), hooks, mask_send=True)
    assert result is StepResult.SENT
    assert any("(masked)" in s for s in status_log)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
def test_render_command_queue_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue wider than terminal is truncated with ellipsis."""
    pytest.importorskip("blessed")

    stdout, transport = mock_stdout()

    blessed_term = get_term()
    monkeypatch.setattr(type(blessed_term), "width", property(lambda self: 20))

    class FakeScroll:
        input_row = 10

    cmds = ["north", "south", "east", "west", "north", "south", "east", "west"]
    queue = CommandQueue(cmds, render=lambda: None)
    queue.current_idx = 0

    render_command_queue(queue, FakeScroll(), stdout)

    output = transport.data.decode("utf-8", errors="replace")
    assert "\u2026" in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
def test_render_command_queue_highlight_active() -> None:
    """Active run uses subdued foreground, pending runs use dim grey."""
    pytest.importorskip("blessed")
    stdout, transport = mock_stdout()

    blessed_term = get_term()

    class FakeScroll:
        input_row = 10

    cmds = ["e", "e", "n"]
    queue = CommandQueue(cmds, render=lambda: None)
    queue.current_idx = 0

    total_w = render_command_queue(queue, FakeScroll(), stdout)

    output = transport.data.decode("utf-8", errors="replace")
    active_sgr = str(blessed_term.color_hex(active_cmd_fg()))
    pending_sgr = blessed_term.color_rgb(*pending_cmd_rgb())
    assert active_sgr in output
    assert pending_sgr in output
    assert total_w > 0


# local
from telix.client_repl_render import (
    HOLD,
    WARM_UP,
    DURATION,
    ELLIPSIS,
    ActivityDot,
    lerp_rgb,
    peak_red,
    peak_yellow,
    activity_hint,
)


class FakeEngine:
    def __init__(self, status="", idx=None):
        self.status_text = status
        self.exclusive_rule_index = idx


class TestActivityHint:
    def test_none_engine(self):
        assert activity_hint(None) == ""

    def test_no_truncation_when_cols_zero(self):
        e = FakeEngine(status="until /very long pattern here/", idx=3)
        hint = activity_hint(e, cols=0)
        assert "[return to cancel]" in hint
        assert ELLIPSIS not in hint

    def test_truncation_with_ellipsis(self):
        e = FakeEngine(status="until /a]very{long}pattern/", idx=5)
        hint = activity_hint(e, cols=30)
        assert len(hint) <= 15
        assert hint.endswith(ELLIPSIS)

    def test_short_hint_not_truncated(self):
        e = FakeEngine(status="delay 1", idx=1)
        hint = activity_hint(e, cols=200)
        assert ELLIPSIS not in hint
        assert "[return to cancel]" in hint


def test_modem_dot_idle_before_trigger():
    dot = ActivityDot()
    assert dot.intensity() == 0.0
    assert not dot.is_animating()
    assert dot.color() == idle_rgb()


def test_modem_dot_peak_after_trigger(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    dot = ActivityDot()
    dot.trigger()
    now[0] += WARM_UP + 0.001
    assert dot.intensity() == pytest.approx(1.0, abs=0.05)
    assert dot.is_animating()
    r, g, b = dot.color()
    assert r == peak_red()[0]
    assert g == peak_red()[1]


def test_modem_dot_idle_after_duration(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    dot = ActivityDot()
    dot.trigger()
    now[0] += DURATION + 0.001
    assert dot.intensity() == 0.0
    assert not dot.is_animating()
    assert dot.color() == idle_rgb()


def test_modem_dot_yellow_peak():
    dot = ActivityDot(peak_rgb=peak_yellow())
    assert dot.color() == idle_rgb()


def test_modem_dot_retrigger_during_glowdown(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    dot = ActivityDot()
    dot.trigger()
    now[0] += WARM_UP + HOLD + 0.050
    mid_intensity = dot.intensity()
    assert 0.0 < mid_intensity < 1.0

    dot.trigger()
    now[0] += WARM_UP * 0.5
    assert dot.intensity() > mid_intensity


def test_modem_dot_autoreply_bg_uses_alt_idle():
    dot = ActivityDot()
    assert dot.color(autoreply_bg=True) == idle_ar_rgb()


def testlerp_rgb_endpoints():
    c1 = (0, 0, 0)
    c2 = (100, 200, 50)
    assert lerp_rgb(c1, c2, 0.0) == c1
    assert lerp_rgb(c1, c2, 1.0) == c2


def testlerp_rgb_midpoint():
    c1 = (0, 0, 0)
    c2 = (100, 200, 50)
    r, g, b = lerp_rgb(c1, c2, 0.5)
    assert r == 50
    assert g == 100
    assert b == 25


class CommandTrackingContext(DynamicRoomContext):
    """Context that changes room based on commands written, not read count."""

    def __init__(self, room_num: str, adj: dict[str, dict[str, str]], blocked_directions: set[tuple[str, str]]) -> None:
        super().__init__(room_num, room_sequence=None)
        self.adj = adj
        self.blocked = blocked_directions

    def on_write(self, data: str | bytes) -> None:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        direction = text.strip()
        current = self.room_val
        if (current, direction) in self.blocked:
            return
        exits = self.adj.get(current, {})
        if direction in exits:
            self.room_val = exits[direction]
            self.room_changed.set()


class TrackingWalkWriter(WalkWriter):
    """Walk writer that updates room based on commands sent."""

    def __init__(self, room_num: str, adj: dict[str, dict[str, str]], blocked_directions: set[tuple[str, str]]) -> None:
        self.sent: list[str] = []
        self.echo_log: list[str] = []
        self.ctx = CommandTrackingContext(room_num, adj, blocked_directions)
        self.ctx.writer = self  # type: ignore[assignment]
        self.ctx.echo_command = self.echo_log.append
        self.ctx.room_arrival_timeout = 0.0
        self.ctx.room_graph = types.SimpleNamespace(
            adj=adj,
            rooms={},
            get_room=lambda num: types.SimpleNamespace(name=num),
            find_branches=lambda pos, **kw: [],
            blocked_rooms=lambda: frozenset(),
        )

    def write(self, data: str) -> None:
        self.sent.append(data)
        self.ctx.on_write(data)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_blocked_exit_tries_other_direction(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """When one exit is blocked, randomwalk marks it and uses the other."""

    adj: dict[str, dict[str, str]] = {
        "room1": {"north": "room2", "south": "room3"},
        "room2": {"south": "room1"},
        "room3": {"north": "room1"},
    }
    blocked = {("room1", "north")}
    writer = TrackingWalkWriter(room_num="room1", adj=adj, blocked_directions=blocked)

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=5)

    assert ("room1", "north") in writer.ctx.blocked_exits
    assert "room3" in writer.ctx.last_walk_visited


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_autodiscover_skips_persistently_blocked_exits(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """Pre-seeded blocked_exits are never attempted by autodiscover."""

    adj: dict[str, dict[str, str]] = {"room1": {"east": "room2", "west": "room3"}}
    writer = WalkWriter(room_num="room1", adj=adj)

    explored_dirs: list[str] = []

    def fake_find_branches(pos: str, **kw: object) -> list[tuple[str, str, str]]:
        return [("room1", "east", "room2"), ("room1", "west", "room3")]

    writer.ctx.room_graph.find_branches = fake_find_branches

    def track_send(direction: str) -> None:
        explored_dirs.append(direction)

    writer.ctx.send_line = track_send

    # Pre-seed east as blocked
    writer.ctx.blocked_exits.add(("room1", "east"))

    await autodiscover(writer.ctx, logging.getLogger("test"), limit=10)

    assert "east" not in explored_dirs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_resume_randomwalk_from_same_room(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """Resume carries over visited set from the previous randomwalk."""

    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}, "room2": {"south": "room1"}}
    writer = WalkWriter(room_num="room1", adj=adj)

    # Run once -- walks nowhere (north blocked), saves state
    await randomwalk(writer.ctx, logging.getLogger("test"), limit=2)

    assert writer.ctx.last_walk_mode == "randomwalk"
    assert writer.ctx.last_walk_room == "room1"
    saved_visited = writer.ctx.last_walk_visited.copy()
    assert "room1" in saved_visited

    # Resume: visited set should be carried over
    await randomwalk(writer.ctx, logging.getLogger("test"), limit=2, resume=True)

    assert saved_visited.issubset(writer.ctx.last_walk_visited)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_resume_not_used_on_room_change(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """Resume state is NOT used when the room changed since last walk."""

    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}}
    writer = WalkWriter(room_num="room1", adj=adj)

    # Simulate previous walk ended at different room
    writer.ctx.last_walk_mode = "randomwalk"
    writer.ctx.last_walk_room = "room99"
    writer.ctx.last_walk_visited = {"room99"}

    parts = ["`resume`"]
    remainder = await handle_travel_commands(parts, writer.ctx, logging.getLogger("test"))

    refuse_msgs = [m for m in writer.echo_log if "cannot resume" in m]
    assert len(refuse_msgs) == 1
    assert remainder == []


def test_travel_re_matches_resume() -> None:
    assert TRAVEL_RE.match("`resume`") is not None
    assert TRAVEL_RE.match("`RESUME`") is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_bounce_detection(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """Walker detects 2-room bounce and blocks the bouncing direction."""

    # Need enough rooms that reachable.issubset(visited) doesn't
    # trigger before the bounce is detected.  room3/room4 are
    # reachable but exits from room1/room2 are blocked, forcing
    # the walker to bounce between room1 and room2.
    adj: dict[str, dict[str, str]] = {
        "room1": {"north": "room2"},
        "room2": {"south": "room1", "east": "room3"},
        "room3": {"west": "room2", "east": "room4"},
        "room4": {"west": "room3"},
    }
    blocked = {("room2", "east")}
    writer = TrackingWalkWriter(room_num="room1", adj=adj, blocked_directions=blocked)

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=30)

    bounce_msgs = [m for m in writer.echo_log if "bounce" in m]
    assert len(bounce_msgs) >= 1
    assert not writer.ctx.randomwalk_active


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_bounce_blocks_reverse(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """After bounce-blocking dead-end south, also block the reverse (north into dead-end)."""

    # dead_end has only south → junction; junction has north → dead_end and south → room3.
    # The walker should not get stuck re-entering dead_end after blocking its south exit.
    adj: dict[str, dict[str, str]] = {
        "junction": {"north": "dead_end", "south": "room3"},
        "dead_end": {"south": "junction"},
        "room3": {"north": "junction", "east": "room4"},
        "room4": {"west": "room3"},
    }
    writer = TrackingWalkWriter(room_num="junction", adj=adj, blocked_directions=set())

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=30)

    assert "room3" in writer.ctx.last_walk_visited
    stop_msgs = [m for m in writer.echo_log if "all exits blocked, stopping" in m]
    assert not stop_msgs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_corridor_no_false_bounce(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """Corridor with exits on both ends must not trigger bounce-blocking."""

    adj: dict[str, dict[str, str]] = {
        "hub": {"southeast": "corridor_a", "east": "market"},
        "corridor_a": {"northwest": "hub", "southeast": "corridor_b"},
        "corridor_b": {"northwest": "corridor_a", "east": "plaza"},
        "plaza": {"west": "corridor_b", "south": "dock"},
        "market": {"west": "hub", "south": "dock"},
        "dock": {"north": "plaza", "west": "market"},
    }
    writer = TrackingWalkWriter(room_num="corridor_a", adj=adj, blocked_directions=set())

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=40)

    bounce_msgs = [m for m in writer.echo_log if "bounce" in m]
    assert not bounce_msgs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_blocked_exit_not_global(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """A blocked exit (A->east->B) must not prevent reaching B from C->north->B."""

    adj: dict[str, dict[str, str]] = {
        "room1": {"east": "room2", "south": "room3"},
        "room2": {"west": "room1"},
        "room3": {"north": "room1", "east": "room2"},
    }
    blocked = {("room1", "east")}
    writer = TrackingWalkWriter(room_num="room1", adj=adj, blocked_directions=blocked)
    writer.ctx.blocked_exits.add(("room1", "east"))

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=10)

    assert "room3" in writer.ctx.last_walk_visited
    assert "room2" in writer.ctx.last_walk_visited


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_noncardinal_deprioritized(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """Non-cardinal exits (up, enter, etc.) are tried after cardinal ones."""

    adj: dict[str, dict[str, str]] = {
        "room1": {"north": "room2", "up": "room3"},
        "room2": {"south": "room1"},
        "room3": {"down": "room1"},
    }
    writer = TrackingWalkWriter(room_num="room1", adj=adj, blocked_directions=set())

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=3)

    sent_dirs = [s.decode("utf-8").strip() if isinstance(s, bytes) else s.strip() for s in writer.sent]
    first_move = sent_dirs[0]
    assert first_move == "north"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_visit_level(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """With visit_level=2 the walk continues until every room is visited twice."""

    adj: dict[str, dict[str, str]] = {
        "room1": {"north": "room2"},
        "room2": {"south": "room1", "east": "room3"},
        "room3": {"west": "room2"},
    }
    writer = TrackingWalkWriter(room_num="room1", adj=adj, blocked_directions=set())

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=50, visit_level=2)

    visited_msgs = [m for m in writer.echo_log if "reachable rooms visited" in m]
    assert len(visited_msgs) == 1
    assert "2x" in visited_msgs[0]

    sent_dirs = [s.decode("utf-8").strip() if isinstance(s, bytes) else s.strip() for s in writer.sent]
    assert len(sent_dirs) >= 4


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_visit_level_1(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """With visit_level=1 the walk stops after visiting each room once."""

    adj: dict[str, dict[str, str]] = {
        "room1": {"north": "room2"},
        "room2": {"south": "room1", "east": "room3"},
        "room3": {"west": "room2"},
    }
    writer = TrackingWalkWriter(room_num="room1", adj=adj, blocked_directions=set())

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=50, visit_level=1)

    visited_msgs = [m for m in writer.echo_log if "reachable rooms visited" in m]
    assert len(visited_msgs) == 1
    assert "1x" in visited_msgs[0]

    sent_dirs = [s.decode("utf-8").strip() if isinstance(s, bytes) else s.strip() for s in writer.sent]
    assert len(sent_dirs) >= 2


def test_macro_last_used_round_trip(tmp_path: Any) -> None:
    path = str(tmp_path / "macros.json")
    macros = [
        Macro(key="KEY_F1", text="test", last_used="2025-06-01T12:00:00+00:00"),
        Macro(key="KEY_F2", text="other"),
    ]
    save_macros(path, macros, "localhost:23")
    loaded = load_macros(path, "localhost:23")
    assert loaded[0].last_used == "2025-06-01T12:00:00+00:00"
    assert loaded[1].last_used == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_skips_blocked_rooms(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    adj: dict[str, dict[str, str]] = {
        "room1": {"north": "room2", "south": "room3"},
        "room2": {"south": "room1"},
        "room3": {"north": "room1"},
    }
    writer = WalkWriter(room_num="room1", adj=adj)
    writer.ctx.room_graph.blocked_rooms = lambda: frozenset({"room2"})

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=5)

    assert "room2" not in writer.ctx.last_walk_visited


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_home_travel_command(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}, "room2": {"south": "room1"}}
    writer = WalkWriter(room_num="room1", adj=adj)
    writer.ctx.room_graph.room_area = lambda num: "town"
    writer.ctx.room_graph.get_home_for_area = lambda area: "room2"
    writer.ctx.room_graph.find_path_with_rooms = lambda src, dst, **kw: (
        [("north", "room2")] if dst == "room2" else None
    )

    fast_travel_args: list[object] = []
    __import__("telix.client_repl_travel", fromlist=["fast_travel"]).fast_travel

    async def mock_fast_travel(*args: object, **kwargs: object) -> None:
        fast_travel_args.append((args, kwargs))

    monkeypatch.setattr("telix.client_repl_travel.fast_travel", mock_fast_travel)

    parts = ["`home`"]
    remainder = await handle_travel_commands(parts, writer.ctx, logging.getLogger("test"))
    assert remainder == []
    assert len(fast_travel_args) == 1


def test_travel_re_matches_home() -> None:
    assert TRAVEL_RE.match("`home`") is not None
    assert TRAVEL_RE.match("`HOME`") is not None


class MockHighlightEngine:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[str] = []

    def process_line(self, line: str) -> tuple[str, bool]:
        self.calls.append(line)
        return f"[{line}]", True

    def process_block(self, block: str) -> tuple[str, bool]:
        return block, False


class TestLineHoldBuffer:
    def make_buf(self, engine: Any = None):  # -> LineHoldBuffer
        return LineHoldBuffer(lambda: engine)

    def test_complete_line_passes_through(self) -> None:
        buf = self.make_buf()
        emit, held = buf.add("hello world\n")
        assert emit == "hello world\n"
        assert held == ""

    def test_incomplete_line_held_back(self) -> None:
        buf = self.make_buf()
        emit, held = buf.add("partial")
        assert emit == ""
        assert held == "partial"

    def test_mixed_complete_and_incomplete(self) -> None:
        buf = self.make_buf()
        emit, held = buf.add("line1\nline2\npartial")
        assert emit == "line1\nline2\n"
        assert held == "partial"

    def test_accumulates_partials(self) -> None:
        buf = self.make_buf()
        buf.add("hel")
        emit, held = buf.add("lo\n")
        assert emit == "hello\n"
        assert held == ""

    def test_flush_raw(self) -> None:
        buf = self.make_buf()
        buf.add("pending")
        text = buf.flush_raw()
        assert text == "pending"
        assert buf.pending == ""

    def test_flush_raw_empty(self) -> None:
        buf = self.make_buf()
        assert buf.flush_raw() == ""

    def test_flush_for_prompt(self) -> None:
        engine = MockHighlightEngine()
        buf = self.make_buf(engine)
        buf.add("prompt>")
        text = buf.flush_for_prompt()
        assert text == "[prompt>]"
        assert buf.pending == ""

    def test_flush_for_prompt_empty(self) -> None:
        buf = self.make_buf()
        assert buf.flush_for_prompt() == ""

    def test_highlights_complete_lines(self) -> None:
        engine = MockHighlightEngine()
        buf = self.make_buf(engine)
        emit, held = buf.add("line1\nline2\npartial")
        assert "[line1]" in emit
        assert "[line2]" in emit
        assert held == "partial"

    def test_no_highlights_when_disabled(self) -> None:
        engine = MockHighlightEngine(enabled=False)
        buf = self.make_buf(engine)
        emit, held = buf.add("hello\n")
        assert emit == "hello\n"
        assert engine.calls == []

    def test_no_highlights_when_engine_none(self) -> None:
        buf = self.make_buf(None)
        emit, held = buf.add("hello\n")
        assert emit == "hello\n"

    def test_flush_for_prompt_no_highlight_when_disabled(self) -> None:
        engine = MockHighlightEngine(enabled=False)
        buf = self.make_buf(engine)
        buf.add("prompt>")
        text = buf.flush_for_prompt()
        assert text == "prompt>"
        assert engine.calls == []

    def test_partial_then_newline_highlights_full_line(self) -> None:
        engine = MockHighlightEngine()
        buf = self.make_buf(engine)
        buf.add("hello ")
        emit, held = buf.add("world\n")
        assert "[hello world]" in emit

    def test_multiple_lines_in_one_chunk(self) -> None:
        engine = MockHighlightEngine()
        buf = self.make_buf(engine)
        emit, held = buf.add("a\nb\nc\n")
        assert emit == "[a]\n[b]\n[c]\n"
        assert held == ""

    def test_pending_property(self) -> None:
        buf = self.make_buf()
        buf.add("hello")
        assert buf.pending == "hello"
        buf.flush_raw()
        assert buf.pending == ""


def test_typescript_file_default_none() -> None:
    """SessionContext.typescript_file defaults to None."""

    ctx = SessionContext()
    assert ctx.typescript_file is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_send_chained_typescript_no_echo(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """When echo is off, send_chained records commands to typescript."""

    real_sleep = asyncio.sleep

    async def fast_sleep(duration: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    writer = WalkWriter(room_num="room1")
    writer.will_echo = False
    writer.ctx.wait_for_prompt = None
    writer.ctx.prompt_ready = None
    writer.ctx.room_changed = None

    ts_path = tmp_path / "typescript"
    ts_file = open(ts_path, "w", encoding="utf-8")
    writer.ctx.typescript_file = ts_file

    commands = ["look", "north", "east"]
    await send_chained(commands, writer.ctx, logging.getLogger("test"))
    ts_file.close()

    content = ts_path.read_text(encoding="utf-8")
    assert "north\n" in content
    assert "east\n" in content
    assert "look" not in content


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_send_chained_typescript_echo_on(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """When echo is on, send_chained does not record to typescript."""

    real_sleep = asyncio.sleep

    async def fast_sleep(duration: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    writer = WalkWriter(room_num="room1")
    writer.will_echo = True
    writer.ctx.wait_for_prompt = None
    writer.ctx.prompt_ready = None
    writer.ctx.room_changed = None

    ts_path = tmp_path / "typescript"
    ts_file = open(ts_path, "w", encoding="utf-8")
    writer.ctx.typescript_file = ts_file

    commands = ["look", "north", "east"]
    await send_chained(commands, writer.ctx, logging.getLogger("test"))
    ts_file.close()

    content = ts_path.read_text(encoding="utf-8")
    assert content == ""


def test_echo_autoreply_writes_typescript(tmp_path: Any) -> None:
    """echo_autoreply writes the command to typescript_file."""
    pytest.importorskip("blessed")

    bt = types.SimpleNamespace(restore="", save="", cyan="", normal="", move_yx=lambda row, col: "")
    scroll = types.SimpleNamespace(input_row=0)
    stdout_writer, _ = mock_stdout()

    ctx = SessionContext()
    ts_path = tmp_path / "typescript"
    ts_file = open(ts_path, "w", encoding="utf-8", newline="")
    ctx.typescript_file = ts_file

    repl = object.__new__(ReplSession)
    repl.blessed_term = bt
    repl.scroll = scroll
    repl.stdout = stdout_writer
    repl.ctx = ctx
    repl.replay_buf = []
    repl.editor = types.SimpleNamespace(display=types.SimpleNamespace(cursor=0), password_mode=False, buf=[])
    repl.telnet_writer = types.SimpleNamespace(will_echo=False)

    repl.echo_autoreply("look")
    ts_file.close()

    content = ts_path.read_text(encoding="utf-8", newline="")
    assert content == "look\r\n"


def test_echo_autoreply_no_typescript() -> None:
    """echo_autoreply with no typescript_file does not crash."""
    pytest.importorskip("blessed")

    bt = types.SimpleNamespace(restore="", save="", cyan="", normal="", move_yx=lambda row, col: "")
    scroll = types.SimpleNamespace(input_row=0)
    stdout_writer, _ = mock_stdout()

    ctx = SessionContext()
    ctx.typescript_file = None

    repl = object.__new__(ReplSession)
    repl.blessed_term = bt
    repl.scroll = scroll
    repl.stdout = stdout_writer
    repl.ctx = ctx
    repl.replay_buf = []
    repl.editor = types.SimpleNamespace(display=types.SimpleNamespace(cursor=0), password_mode=False, buf=[])
    repl.telnet_writer = types.SimpleNamespace(will_echo=False)

    repl.echo_autoreply("look")


def test_echo_autoreply_masks_when_will_echo(tmp_path: Any) -> None:
    """echo_autoreply masks display and typescript when will_echo is True."""
    pytest.importorskip("blessed")

    bt = types.SimpleNamespace(restore="", save="", cyan="", normal="", move_yx=lambda row, col: "")
    scroll = types.SimpleNamespace(input_row=0)
    stdout_writer, stdout_buf = mock_stdout()

    ctx = SessionContext()
    ts_path = tmp_path / "typescript"
    ts_file = open(ts_path, "w", encoding="utf-8", newline="")
    ctx.typescript_file = ts_file

    repl = object.__new__(ReplSession)
    repl.blessed_term = bt
    repl.scroll = scroll
    repl.stdout = stdout_writer
    repl.ctx = ctx
    repl.replay_buf = []
    repl.editor = types.SimpleNamespace(display=types.SimpleNamespace(cursor=0), password_mode=False, buf=[])
    repl.telnet_writer = types.SimpleNamespace(will_echo=True)

    repl.echo_autoreply("secret123")
    ts_file.close()

    ts_content = ts_path.read_text(encoding="utf-8", newline="")
    assert ts_content == "\r\n"

    display_output = bytes(stdout_buf.data).decode()
    assert "secret123" not in display_output
    sextant_set = set(SEXTANT[1:])
    masked = [ch for ch in display_output if ch in sextant_set]
    assert len(masked) == SCRAMBLE_LEN

    replay = b"".join(repl.replay_buf).decode()
    assert "secret123" not in replay


def test_typescript_will_echo_writes_bare_crlf(tmp_path: Any) -> None:
    """When will_echo is True, read_input writes bare \\r\\n to typescript."""
    pytest.importorskip("blessed")

    ctx = SessionContext()
    ts_path = tmp_path / "typescript"
    ts_file = open(ts_path, "w", encoding="utf-8", newline="")
    ctx.typescript_file = ts_file
    ts_file.write("Password: ")
    ts_file.flush()

    ts_file.write("\r\n")
    ts_file.flush()

    ts_file.write("Welcome to Dune.\r\n")
    ts_file.flush()
    ts_file.close()

    content = ts_path.read_text(encoding="utf-8", newline="")
    assert "Password: \r\nWelcome" in content


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_handle_travel_noreply_parsed(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """``noreply`` keyword is parsed and passed to autodiscover."""

    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}}
    writer = WalkWriter(room_num="room1", adj=adj)

    captured_kw: list[dict] = []

    async def fake_autodiscover(ctx, log, **kw):
        captured_kw.append(kw)

    monkeypatch.setattr("telix.client_repl_travel.autodiscover", fake_autodiscover)

    parts = ["`autodiscover noreply`"]
    await handle_travel_commands(parts, writer.ctx, logging.getLogger("test"))

    assert len(captured_kw) == 1
    assert captured_kw[0]["noreply"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_randomwalk_noreply_disables_engine(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """``noreply=True`` disables autoreply engine during randomwalk and restores after."""

    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}}
    writer = WalkWriter(room_num="room1", adj=adj)
    engine = AutoreplyEngine(rules=[], ctx=writer.ctx, log=logging.getLogger("test"))
    writer.ctx.autoreply_engine = engine
    assert engine.enabled is True

    await randomwalk(writer.ctx, logging.getLogger("test"), limit=2, noreply=True)

    assert engine.enabled is True
    assert writer.ctx.last_walk_noreply is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_autodiscover_noreply_disables_engine(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """``noreply=True`` disables autoreply engine during autodiscover and restores after."""

    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}, "room2": {"south": "room1"}}
    writer = WalkWriter(room_num="room1", adj=adj)
    writer.ctx.room_graph.find_branches = lambda pos, **kw: [("room1", "north", "room2")]
    engine = AutoreplyEngine(rules=[], ctx=writer.ctx, log=logging.getLogger("test"))
    writer.ctx.autoreply_engine = engine

    async def noop_fast_travel(*args, **kwargs):
        pass

    monkeypatch.setattr("telix.client_repl_travel.fast_travel", noop_fast_travel)

    await autodiscover(writer.ctx, logging.getLogger("test"), limit=1, noreply=True)

    assert engine.enabled is True
    assert writer.ctx.last_walk_noreply is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
@pytest.mark.asyncio
async def test_resume_inherits_noreply(monkeypatch: pytest.MonkeyPatch, fast_sleep) -> None:
    """``resume`` picks up ``noreply`` from saved walk state."""

    adj: dict[str, dict[str, str]] = {"room1": {"north": "room2"}}
    writer = WalkWriter(room_num="room1", adj=adj)
    writer.ctx.last_walk_mode = "randomwalk"
    writer.ctx.last_walk_room = "room1"
    writer.ctx.last_walk_noreply = True

    captured_noreply: list[bool] = []

    async def fake_randomwalk(ctx, log, **kw):
        captured_noreply.append(kw.get("noreply", False))

    monkeypatch.setattr("telix.client_repl_travel.randomwalk", fake_randomwalk)

    parts = ["`resume`"]
    await handle_travel_commands(parts, writer.ctx, logging.getLogger("test"))

    assert captured_noreply == [True]
