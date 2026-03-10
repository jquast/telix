"""Tests for scripts.py: ScriptOutputBuffer, ScriptContext, ScriptManager."""

import re
import sys
import types
import asyncio
import logging
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from telix import scripts
from telix.client_repl_commands import (
    ASYNC_CMD_RE,
    AWAIT_CMD_RE,
    STOPSCRIPT_CMD_RE,
    SCRIPTS_CMD_RE,
    DispatchHooks,
    StepResult,
    dispatch_one,
)

# ---------------------------------------------------------------------------
# Backtick regex constants
# ---------------------------------------------------------------------------


class TestAsyncCmdRe:
    """ASYNC_CMD_RE matches backtick-enclosed async (fire-and-forget) script commands."""

    def test_matches_bare_module(self):
        m = ASYNC_CMD_RE.match("`async demo`")
        assert m is not None
        assert m.group(1) == "demo"

    def test_matches_dotted_function(self):
        m = ASYNC_CMD_RE.match("`async combat.hunt`")
        assert m is not None
        assert m.group(1) == "combat.hunt"

    def test_matches_with_args(self):
        m = ASYNC_CMD_RE.match("`async rooms.goto 12345`")
        assert m is not None
        assert m.group(1) == "rooms.goto 12345"

    def test_does_not_match_plain_text(self):
        assert ASYNC_CMD_RE.match("async demo") is None

    def test_case_insensitive(self):
        assert ASYNC_CMD_RE.match("`ASYNC demo`") is not None


class TestAwaitCmdRe:
    """AWAIT_CMD_RE matches backtick-enclosed await (blocking) script commands."""

    def test_matches_bare_module(self):
        m = AWAIT_CMD_RE.match("`await demo`")
        assert m is not None
        assert m.group(1) == "demo"

    def test_matches_dotted_function(self):
        m = AWAIT_CMD_RE.match("`await combat.hunt`")
        assert m is not None
        assert m.group(1) == "combat.hunt"

    def test_matches_with_args(self):
        m = AWAIT_CMD_RE.match("`await rooms.goto 12345`")
        assert m is not None
        assert m.group(1) == "rooms.goto 12345"

    def test_does_not_match_plain_text(self):
        assert AWAIT_CMD_RE.match("await demo") is None

    def test_case_insensitive(self):
        assert AWAIT_CMD_RE.match("`AWAIT demo`") is not None


class TestScriptsCmdRe:
    """SCRIPTS_CMD_RE matches the bare `scripts` backtick command."""

    def test_matches(self):
        assert SCRIPTS_CMD_RE.match("`scripts`") is not None

    def test_case_insensitive(self):
        assert SCRIPTS_CMD_RE.match("`SCRIPTS`") is not None

    def test_does_not_match_with_args(self):
        assert SCRIPTS_CMD_RE.match("`scripts foo`") is None

    def test_does_not_match_plain_text(self):
        assert SCRIPTS_CMD_RE.match("scripts") is None


class TestStopscriptCmdRe:
    """STOPSCRIPT_CMD_RE matches backtick-enclosed stopscript commands."""

    def test_matches_stop_all(self):
        m = STOPSCRIPT_CMD_RE.match("`stopscript`")
        assert m is not None
        assert m.group(1) is None

    def test_matches_named_script(self):
        m = STOPSCRIPT_CMD_RE.match("`stopscript combat.hunt`")
        assert m is not None
        assert m.group(1) == "combat.hunt"

    def test_does_not_match_plain_text(self):
        assert STOPSCRIPT_CMD_RE.match("stopscript") is None


# ---------------------------------------------------------------------------
# ScriptOutputBuffer tests
# ---------------------------------------------------------------------------


class TestScriptOutputBufferFeed:
    """ScriptOutputBuffer.feed accumulates stripped lines."""

    def test_partial_line_no_newline(self):
        buf = scripts.ScriptOutputBuffer()
        buf.feed("hello")
        assert buf.output() == "hello"

    def test_complete_line(self):
        buf = scripts.ScriptOutputBuffer()
        buf.feed("hello\n")
        assert "hello" in buf.output()

    def test_ansi_stripped(self):
        buf = scripts.ScriptOutputBuffer()
        buf.feed("\x1b[31mred text\x1b[0m\n")
        text = buf.output()
        assert "red text" in text
        assert "\x1b" not in text

    def test_multiple_chunks(self):
        buf = scripts.ScriptOutputBuffer()
        buf.feed("line one\n")
        buf.feed("line two\n")
        text = buf.output()
        assert "line one" in text
        assert "line two" in text

    def test_clear_flag(self):
        buf = scripts.ScriptOutputBuffer()
        buf.feed("hello\n")
        buf.output(clear=True)
        assert buf.output() == ""


class TestScriptOutputBufferTurns:
    """ScriptOutputBuffer.turns returns prompt-delimited blocks."""

    def test_no_turns_initially(self):
        buf = scripts.ScriptOutputBuffer()
        assert buf.turns() == []

    def test_one_turn_after_prompt(self):
        buf = scripts.ScriptOutputBuffer()
        buf.feed("line one\n")
        buf.on_prompt()
        turns = buf.turns(5)
        assert len(turns) == 1
        assert "line one" in turns[0]

    def test_multiple_turns(self):
        buf = scripts.ScriptOutputBuffer()
        buf.feed("turn1\n")
        buf.on_prompt()
        buf.feed("turn2\n")
        buf.on_prompt()
        turns = buf.turns(5)
        assert len(turns) == 2
        assert "turn1" in turns[0]
        assert "turn2" in turns[1]

    def test_turns_limit(self):
        buf = scripts.ScriptOutputBuffer()
        for i in range(10):
            buf.feed(f"line{i}\n")
            buf.on_prompt()
        assert len(buf.turns(3)) == 3


class TestScriptOutputBufferWaitForPattern:
    """ScriptOutputBuffer.wait_for_pattern async matching."""

    @pytest.mark.asyncio
    async def test_already_present(self):
        buf = scripts.ScriptOutputBuffer()
        buf.feed("You have died\n")
        pattern = re.compile(r"You have died", re.IGNORECASE)
        m = await buf.wait_for_pattern(pattern, timeout=1.0)
        assert m is not None

    @pytest.mark.asyncio
    async def test_arrives_later(self):
        buf = scripts.ScriptOutputBuffer()
        pattern = re.compile(r"arrived", re.IGNORECASE)

        async def feeder():
            await asyncio.sleep(0.05)
            buf.feed("You have arrived\n")

        asyncio.ensure_future(feeder())
        m = await buf.wait_for_pattern(pattern, timeout=1.0)
        assert m is not None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        buf = scripts.ScriptOutputBuffer()
        pattern = re.compile(r"never", re.IGNORECASE)
        m = await buf.wait_for_pattern(pattern, timeout=0.05)
        assert m is None


class TestScriptOutputBufferWaitForPrompt:
    """ScriptOutputBuffer.wait_for_prompt async prompt counting."""

    @pytest.mark.asyncio
    async def test_prompt_arrives(self):
        buf = scripts.ScriptOutputBuffer()

        async def send_prompt():
            await asyncio.sleep(0.05)
            buf.on_prompt()

        asyncio.ensure_future(send_prompt())
        result = await buf.wait_for_prompt(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_timeout(self):
        buf = scripts.ScriptOutputBuffer()
        result = await buf.wait_for_prompt(timeout=0.05)
        assert result is False


# ---------------------------------------------------------------------------
# ScriptContext tests
# ---------------------------------------------------------------------------


def make_ctx():
    """Return a minimal mock TelixSessionContext."""
    ctx = MagicMock()
    ctx.gmcp_data = {"Char.Vitals": {"hp": 80, "maxhp": 100}}
    ctx.room.current = "42"
    ctx.highlights.captures = {"Kills": 5}
    ctx.room.graph = MagicMock()
    ctx.room.graph.adj = {"42": {"north": "43", "south": "41"}}
    ctx.room.graph.find_path.return_value = ["north", "north"]
    ctx.room.graph.get_room.return_value = MagicMock(name="Forest", area="Wilds")
    ctx.prompt.echo = MagicMock()
    ctx.prompt.wait_fn = None
    ctx.prompt.ready = None
    ctx.script_manager = None
    ctx.writer = MagicMock()
    return ctx


class TestScriptContextGmcpGet:
    """ScriptContext.gmcp_get traverses nested GMCP data."""

    def test_top_level_key(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        vitals = ctx.gmcp_get("Char.Vitals")
        assert isinstance(vitals, dict)

    def test_nested_key(self):
        session_ctx = make_ctx()
        session_ctx.gmcp_data = {"Char": {"Vitals": {"hp": 50}}}
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        assert ctx.gmcp_get("Char.Vitals.hp") == 50

    def test_missing_key(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        assert ctx.gmcp_get("No.Such.Key") is None


class TestScriptContextNeighbors:
    """ScriptContext.neighbors returns exit dict."""

    def test_has_exits(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        exits = ctx.neighbors()
        assert exits == {"north": "43", "south": "41"}

    def test_no_room_graph(self):
        session_ctx = make_ctx()
        session_ctx.room.graph = None
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        assert ctx.neighbors() == {}


class TestScriptContextPrint:
    """ScriptContext.print calls echo_command."""

    def test_delegates_to_echo_command(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        ctx.print("hello world")
        session_ctx.prompt.echo.assert_called_once_with("hello world")

    def test_no_echo_command_does_not_crash(self):
        session_ctx = make_ctx()
        session_ctx.prompt.echo = None
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        ctx.print("silent")

    def test_non_string_argument_is_converted(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        ctx.print(42)
        session_ctx.prompt.echo.assert_called_once_with("42")

    def test_list_argument_is_converted(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        ctx.print([1, 2, 3])
        session_ctx.prompt.echo.assert_called_once_with("[1, 2, 3]")

    def test_multiple_args_joined_with_space(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        ctx.print("hp:", 100)
        session_ctx.prompt.echo.assert_called_once_with("hp: 100")

    def test_multiple_args_custom_sep(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        ctx.print("a", "b", "c", sep=", ")
        session_ctx.prompt.echo.assert_called_once_with("a, b, c")


class TestScriptContextProperties:
    """ScriptContext property accessors."""

    def test_gmcp_property(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        assert ctx.gmcp is session_ctx.gmcp_data

    def test_room_id(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        assert ctx.room_id == "42"

    def test_captures(self):
        session_ctx = make_ctx()
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        assert ctx.captures["Kills"] == 5


class TestScriptContextSend:
    """ScriptContext.send sends commands through dispatch."""

    @pytest.mark.asyncio
    async def test_single_command(self):
        session_ctx = make_ctx()
        session_ctx.prompt.wait_fn = None
        session_ctx.prompt.ready = None
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        sent = []
        session_ctx.writer.write.side_effect = sent.append
        await ctx.send("look")
        assert any("look" in s for s in sent)

    @pytest.mark.asyncio
    async def test_chained_commands(self):
        session_ctx = make_ctx()
        session_ctx.prompt.wait_fn = None
        session_ctx.prompt.ready = None
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        sent = []
        session_ctx.writer.write.side_effect = sent.append
        await ctx.send("north|south")
        assert len([s for s in sent if s.strip()]) == 2


# ---------------------------------------------------------------------------
# ScriptManager tests
# ---------------------------------------------------------------------------


def make_fake_module(fn_name: str, body: str) -> types.ModuleType:
    """Create an in-memory module with a named async function."""
    mod = types.ModuleType("fake_script")
    code = textwrap.dedent(f"""
        import asyncio
        async def {fn_name}(ctx, *args):
            {body}
    """)
    exec(compile(code, "<fake_script>", "exec"), mod.__dict__)
    return mod


class TestScriptManagerStartStop:
    """ScriptManager start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_runs_function(self):
        mod = make_fake_module("run", "ctx.print('hi')")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"demo": mod}):
            task = mgr.start_script(session_ctx, "demo")

        assert isinstance(task, asyncio.Task)
        await asyncio.sleep(0.05)
        session_ctx.prompt.echo.assert_called()

    @pytest.mark.asyncio
    async def test_start_with_args(self):
        received = []
        mod = make_fake_module("hunt", "received.append(args)")
        mod.__dict__["received"] = received
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"combat": mod}):
            mgr.start_script(session_ctx, "combat.hunt goblin")

        await asyncio.sleep(0.05)
        assert received and received[0] == ("goblin",)

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        mod = make_fake_module("run", "await asyncio.sleep(10)")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"long_script": mod}):
            mgr.start_script(session_ctx, "long_script")

        assert "long_script" in mgr.active_scripts()
        stopped = mgr.stop_script("long_script")
        assert stopped == ["long_script"]
        await asyncio.sleep(0.05)
        assert "long_script" not in mgr.active_scripts()

    @pytest.mark.asyncio
    async def test_stop_returns_empty_when_not_running(self):
        mgr = scripts.ScriptManager()
        stopped = mgr.stop_script("nonexistent")
        assert stopped == []

    @pytest.mark.asyncio
    async def test_stop_all(self):
        mod1 = make_fake_module("run", "await asyncio.sleep(10)")
        mod2 = make_fake_module("run", "await asyncio.sleep(10)")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"script_a": mod1, "script_b": mod2}):
            mgr.start_script(session_ctx, "script_a")
            mgr.start_script(session_ctx, "script_b")

        stopped = mgr.stop_script(None)
        assert set(stopped) == {"script_a", "script_b"}
        await asyncio.sleep(0.05)
        assert mgr.active_scripts() == []

    def test_active_scripts_empty_initially(self):
        mgr = scripts.ScriptManager()
        assert mgr.active_scripts() == []

    def test_missing_function_raises(self):
        mod = types.ModuleType("bare_mod")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"bare_mod": mod}):
            with pytest.raises(ValueError, match="no function"):
                mgr.start_script(session_ctx, "bare_mod.nonexistent")


class TestScriptContextNewProperties:
    """ScriptContext new simple property accessors."""

    def test_session_key(self):
        session_ctx = make_ctx()
        session_ctx.session_key = "mud.example.com:4000"
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.session_key == "mud.example.com:4000"

    def test_previous_room_id(self):
        session_ctx = make_ctx()
        session_ctx.room.previous = "41"
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.previous_room_id == "41"

    def test_capture_log(self):
        session_ctx = make_ctx()
        log_data = {"Kills": [{"value": 5, "time": 1234}]}
        session_ctx.highlights.capture_log = log_data
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.capture_log is log_data

    def test_chat_messages(self):
        session_ctx = make_ctx()
        msgs = [{"channel": "tells", "text": "hello"}]
        session_ctx.chat.messages = msgs
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.chat_messages is msgs

    def test_chat_unread(self):
        session_ctx = make_ctx()
        session_ctx.chat.unread = 3
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.chat_unread == 3

    def test_chat_channels(self):
        session_ctx = make_ctx()
        channels = [{"name": "tells"}, {"name": "clan"}]
        session_ctx.chat.channels = channels
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.chat_channels is channels


class TestScriptContextRoomChanged:
    """ScriptContext.room_changed awaitable."""

    @pytest.mark.asyncio
    async def test_fires_when_event_set(self):
        session_ctx = make_ctx()
        session_ctx.room.changed = asyncio.Event()
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))

        async def pulse():
            await asyncio.sleep(0.05)
            session_ctx.room.changed.set()
            session_ctx.room.changed.clear()

        asyncio.ensure_future(pulse())
        assert await ctx.room_changed(timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        session_ctx = make_ctx()
        session_ctx.room.changed = asyncio.Event()
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert await ctx.room_changed(timeout=0.05) is False


class TestScriptContextWalk:
    """ScriptContext.walk_active and stop_walk."""

    def test_walk_active_all_none(self):
        session_ctx = make_ctx()
        session_ctx.walk.discover_task = None
        session_ctx.walk.randomwalk_task = None
        session_ctx.walk.travel_task = None
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.walk_active is False

    def test_walk_active_discover_running(self):
        session_ctx = make_ctx()
        task = MagicMock()
        task.done.return_value = False
        session_ctx.walk.discover_task = task
        session_ctx.walk.randomwalk_task = None
        session_ctx.walk.travel_task = None
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.walk_active is True

    def test_walk_active_all_done(self):
        session_ctx = make_ctx()
        task = MagicMock()
        task.done.return_value = True
        session_ctx.walk.discover_task = task
        session_ctx.walk.randomwalk_task = task
        session_ctx.walk.travel_task = task
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.walk_active is False

    def test_stop_walk_cancels_running_tasks(self):
        session_ctx = make_ctx()
        task = MagicMock()
        task.done.return_value = False
        session_ctx.walk.discover_task = task
        session_ctx.walk.randomwalk_task = None
        session_ctx.walk.travel_task = None
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        ctx.stop_walk()
        task.cancel.assert_called_once()

    def test_stop_walk_skips_done_tasks(self):
        session_ctx = make_ctx()
        task = MagicMock()
        task.done.return_value = True
        session_ctx.walk.discover_task = task
        session_ctx.walk.randomwalk_task = None
        session_ctx.walk.travel_task = None
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        ctx.stop_walk()
        task.cancel.assert_not_called()


def make_dispatch_hooks(mgr, echoed):
    """Return a minimal DispatchHooks wired to *mgr* and an echo recorder."""
    ctx = make_ctx()
    ctx.scripts.manager = mgr
    return DispatchHooks(
        ctx=ctx,
        log=logging.getLogger("test"),
        wait_fn=None,
        send_fn=lambda s: None,
        echo_fn=echoed.append,
    )


class TestScriptsDispatch:
    """Backtick `scripts` command echoes active script names."""

    @pytest.mark.asyncio
    async def test_lists_running_scripts(self):
        mod = make_fake_module("run", "await asyncio.sleep(10)")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()
        with patch.dict(sys.modules, {"bg_script": mod}):
            mgr.start_script(session_ctx, "bg_script")
        echoed = []
        hooks = make_dispatch_hooks(mgr, echoed)
        result = await dispatch_one("`scripts`", 0, 0, frozenset(), hooks)
        assert result is StepResult.HANDLED
        assert any("bg_script" in line for line in echoed)
        mgr.stop_script(None)

    @pytest.mark.asyncio
    async def test_no_scripts_running(self):
        mgr = scripts.ScriptManager()
        echoed = []
        hooks = make_dispatch_hooks(mgr, echoed)
        result = await dispatch_one("`scripts`", 0, 0, frozenset(), hooks)
        assert result is StepResult.HANDLED
        assert any("no scripts" in line.lower() for line in echoed)

    @pytest.mark.asyncio
    async def test_no_manager(self):
        echoed = []
        hooks = make_dispatch_hooks(None, echoed)
        result = await dispatch_one("`scripts`", 0, 0, frozenset(), hooks)
        assert result is StepResult.HANDLED


class TestAwaitDispatch:
    """Backtick `await` command blocks until the script task completes."""

    @pytest.mark.asyncio
    async def test_await_blocks_until_done(self):
        """dispatch_one with `await` waits for the script to finish before returning."""
        completed = []
        mod = make_fake_module("run", "completed.append(1)")
        mod.__dict__["completed"] = completed
        mgr = scripts.ScriptManager()
        echoed = []
        hooks = make_dispatch_hooks(mgr, echoed)

        with patch.dict(sys.modules, {"await_script": mod}):
            result = await dispatch_one("`await await_script`", 0, 0, frozenset(), hooks)

        assert result is StepResult.HANDLED
        assert completed == [1]

    @pytest.mark.asyncio
    async def test_await_no_manager(self):
        """dispatch_one with `await` and no manager returns HANDLED without error."""
        echoed = []
        hooks = make_dispatch_hooks(None, echoed)
        result = await dispatch_one("`await some_script`", 0, 0, frozenset(), hooks)
        assert result is StepResult.HANDLED


class TestScriptManagerExceptionReporting:
    """ScriptManager reports script exceptions via ctx.print."""

    @pytest.mark.asyncio
    async def test_assert_error_printed(self):
        mod = make_fake_module("run", "assert False, 'boom'")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"fail_script": mod}):
            mgr.start_script(session_ctx, "fail_script")

        await asyncio.sleep(0.1)
        calls = session_ctx.prompt.echo.call_args_list
        assert calls, "ctx.print was never called"
        printed = "\n".join(str(c) for c in calls)
        assert "AssertionError" in printed

    @pytest.mark.asyncio
    async def test_exception_message_included(self):
        mod = make_fake_module("run", "raise ValueError('bad value')")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"fail_script2": mod}):
            mgr.start_script(session_ctx, "fail_script2")

        await asyncio.sleep(0.1)
        calls = session_ctx.prompt.echo.call_args_list
        assert calls
        printed = "\n".join(str(c) for c in calls)
        assert "bad value" in printed

    @pytest.mark.asyncio
    async def test_cancelled_not_printed(self):
        mod = make_fake_module("run", "await asyncio.sleep(10)")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"cancel_script": mod}):
            mgr.start_script(session_ctx, "cancel_script")

        mgr.stop_script("cancel_script")
        await asyncio.sleep(0.1)
        session_ctx.prompt.echo.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_error_printed(self):
        """Import-time errors are printed via ctx.print, not silently swallowed."""
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch("importlib.import_module", side_effect=SyntaxError("unexpected EOF")):
            mgr.start_script(session_ctx, "bad_script")

        await asyncio.sleep(0.1)
        calls = session_ctx.prompt.echo.call_args_list
        assert calls, "ctx.print was never called for import error"
        printed = "\n".join(str(c) for c in calls)
        assert "SyntaxError" in printed

    @pytest.mark.asyncio
    async def test_import_error_returns_task(self):
        """start_script returns a task even when the module fails to import."""
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch("importlib.import_module", side_effect=ImportError("no module named 'missing'")):
            task = mgr.start_script(session_ctx, "missing_script")

        assert task is not None
        await asyncio.sleep(0.1)
        assert task.done()


class TestScriptManagerFeed:
    """ScriptManager.feed and on_prompt fan out to buffers."""

    @pytest.mark.asyncio
    async def test_feed_reaches_buffer(self):
        received = []

        async def run(ctx, *args):
            m = await ctx.wait_for("hello", timeout=1.0)
            if m:
                received.append(m.group(0))

        mod = types.ModuleType("feed_test")
        mod.run = run
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"feed_test": mod}):
            mgr.start_script(session_ctx, "feed_test")

        mgr.feed("hello world\n")
        await asyncio.sleep(0.1)
        assert received == ["hello"]

    @pytest.mark.asyncio
    async def test_on_prompt_reaches_buffer(self):
        prompted = []

        async def run(ctx, *args):
            result = await ctx.prompt(timeout=1.0)
            prompted.append(result)

        mod = types.ModuleType("prompt_test")
        mod.run = run
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"prompt_test": mod}):
            mgr.start_script(session_ctx, "prompt_test")

        await asyncio.sleep(0.02)
        mgr.on_prompt()
        await asyncio.sleep(0.1)
        assert prompted == [True]
