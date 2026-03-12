"""Tests for scripts.py: ScriptOutputBuffer, ScriptContext, ScriptManager."""

import re
import sys
import types
import asyncio
import logging
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from telix import scripts, session_context
from telix.client_repl_commands import (
    ASYNC_CMD_RE,
    AWAIT_CMD_RE,
    SCRIPTS_CMD_RE,
    STOPSCRIPT_CMD_RE,
    StepResult,
    DispatchHooks,
    dispatch_one,
)

NO_MATCH = object()
MATCH_ONLY = object()


class TestBacktickRegex:
    """Backtick command regex matching."""

    @pytest.mark.parametrize(
        "regex, text, expected",
        [
            (ASYNC_CMD_RE, "`async demo`", "demo"),
            (ASYNC_CMD_RE, "`async combat.hunt`", "combat.hunt"),
            (ASYNC_CMD_RE, "`async rooms.goto 12345`", "rooms.goto 12345"),
            (ASYNC_CMD_RE, "async demo", NO_MATCH),
            (ASYNC_CMD_RE, "`ASYNC demo`", "demo"),
            (AWAIT_CMD_RE, "`await demo`", "demo"),
            (AWAIT_CMD_RE, "`await combat.hunt`", "combat.hunt"),
            (AWAIT_CMD_RE, "`await rooms.goto 12345`", "rooms.goto 12345"),
            (AWAIT_CMD_RE, "await demo", NO_MATCH),
            (AWAIT_CMD_RE, "`AWAIT demo`", "demo"),
            (SCRIPTS_CMD_RE, "`scripts`", MATCH_ONLY),
            (SCRIPTS_CMD_RE, "`SCRIPTS`", MATCH_ONLY),
            (SCRIPTS_CMD_RE, "`scripts foo`", NO_MATCH),
            (SCRIPTS_CMD_RE, "scripts", NO_MATCH),
            (STOPSCRIPT_CMD_RE, "`stopscript`", None),
            (STOPSCRIPT_CMD_RE, "`stopscript combat.hunt`", "combat.hunt"),
            (STOPSCRIPT_CMD_RE, "stopscript", NO_MATCH),
        ],
    )
    def test_regex_match(self, regex, text, expected):
        m = regex.match(text)
        if expected is NO_MATCH:
            assert m is None
        elif expected is MATCH_ONLY:
            assert m is not None
        else:
            assert m is not None
            assert m.group(1) == expected


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

    @pytest.mark.asyncio
    async def test_match_not_reused(self):
        """Second wait_for does not re-match the already-consumed line."""
        buf = scripts.ScriptOutputBuffer()
        buf.feed("poison line\n")
        pattern = re.compile(r"poison", re.IGNORECASE)
        m1 = await buf.wait_for_pattern(pattern, timeout=1.0)
        assert m1 is not None
        m2 = await buf.wait_for_pattern(pattern, timeout=0.05)
        assert m2 is None

    @pytest.mark.asyncio
    async def test_two_occurrences_matched_separately(self):
        """Two occurrences of the pattern are consumed one at a time."""
        buf = scripts.ScriptOutputBuffer()
        buf.feed("poison\nother line\npoison\n")
        pattern = re.compile(r"poison", re.IGNORECASE)
        m1 = await buf.wait_for_pattern(pattern, timeout=1.0)
        assert m1 is not None
        m2 = await buf.wait_for_pattern(pattern, timeout=1.0)
        assert m2 is not None
        m3 = await buf.wait_for_pattern(pattern, timeout=0.05)
        assert m3 is None

    @pytest.mark.asyncio
    async def test_new_occurrence_wakes_next_wait(self):
        """A line arriving after consumption wakes the subsequent wait_for."""
        buf = scripts.ScriptOutputBuffer()
        buf.feed("poison\n")
        pattern = re.compile(r"poison", re.IGNORECASE)
        await buf.wait_for_pattern(pattern, timeout=1.0)

        async def feeder():
            await asyncio.sleep(0.05)
            buf.feed("poison\n")

        asyncio.ensure_future(feeder())
        m = await buf.wait_for_pattern(pattern, timeout=1.0)
        assert m is not None

    @pytest.mark.asyncio
    async def test_cursor_resets_on_output_clear(self):
        """Clearing output via output(clear=True) resets the cursor."""
        buf = scripts.ScriptOutputBuffer()
        buf.feed("poison\n")
        pattern = re.compile(r"poison", re.IGNORECASE)
        await buf.wait_for_pattern(pattern, timeout=1.0)
        buf.output(clear=True)
        buf.feed("poison\n")
        m = await buf.wait_for_pattern(pattern, timeout=1.0)
        assert m is not None


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
    ctx.commands = session_context.CommandState()
    ctx.gmcp.any_update = asyncio.Event()
    ctx.gmcp.package_events = {}
    return ctx


def make_script_ctx(gmcp_data=None):
    """Return a ScriptContext backed by a mock session with the given GMCP data."""
    session_ctx = make_ctx()
    if gmcp_data is not None:
        session_ctx.gmcp_data = gmcp_data
    return scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test")), session_ctx


class TestScriptContextGmcpGet:
    """ScriptContext.gmcp_get traverses nested GMCP data."""

    @pytest.mark.parametrize(
        "gmcp_data, key, expected",
        [
            ({"Char.Vitals": {"hp": 80, "maxhp": 100}}, "Char.Vitals", {"hp": 80, "maxhp": 100}),
            ({"Char": {"Vitals": {"hp": 50}}}, "Char.Vitals.hp", 50),
            ({"Char.Vitals": {"hp": 80}}, "No.Such.Key", None),
            ({"Char.Guild.Stats": {"Water": 80, "MaxWater": 200}}, "Char.Guild.Stats.Water%", pytest.approx(0.4)),
            ({"Char.Guild.Stats": {"Water": 80}}, "Char.Guild.Stats.Water%", None),
            ({"Char.Guild.Stats": {"Water": 80, "MaxWater": 0}}, "Char.Guild.Stats.Water%", None),
            ({"Char.Guild.Stats": {"Water": 150, "maxwater": 200}}, "Char.Guild.Stats.Water%", pytest.approx(0.75)),
            ({"Char.Guild.Stats": {"Water": 80, "MaxWater": 200}}, "Water", 80),
            ({"Char.Guild.Stats": {"Water": 80, "MaxWater": 200}}, "Water%", pytest.approx(0.4)),
            ({"Char.Guild.Stats": {"Water": 80}}, "Nope", None),
        ],
    )
    def test_gmcp_get(self, gmcp_data, key, expected):
        ctx, _ = make_script_ctx(gmcp_data)
        result = ctx.gmcp_get(key)
        if isinstance(expected, dict):
            assert isinstance(result, dict)
            assert result == expected
        else:
            assert result == expected


class TestScriptContextPrint:
    """ScriptContext.print calls echo_command."""

    def test_delegates_to_echo_command(self):
        ctx, sctx = make_script_ctx()
        ctx.print("hello world")
        sctx.prompt.echo.assert_called_once_with("hello world")

    def test_no_echo_command_does_not_crash(self):
        ctx, sctx = make_script_ctx()
        sctx.prompt.echo = None
        ctx.print("silent")

    def test_non_string_argument_is_converted(self):
        ctx, sctx = make_script_ctx()
        ctx.print(42)
        sctx.prompt.echo.assert_called_once_with("42")

    def test_list_argument_is_converted(self):
        ctx, sctx = make_script_ctx()
        ctx.print([1, 2, 3])
        sctx.prompt.echo.assert_called_once_with("[1, 2, 3]")

    def test_multiple_args_joined_with_space(self):
        ctx, sctx = make_script_ctx()
        ctx.print("hp:", 100)
        sctx.prompt.echo.assert_called_once_with("hp: 100")

    def test_multiple_args_custom_sep(self):
        ctx, sctx = make_script_ctx()
        ctx.print("a", "b", "c", sep=", ")
        sctx.prompt.echo.assert_called_once_with("a, b, c")


class TestScriptContextLogging:
    """ScriptContext level-specific logging methods."""

    @pytest.mark.parametrize(
        "method, logger_method", [("debug", "debug"), ("info", "info"), ("warn", "warning"), ("error", "error")]
    )
    def test_log_level(self, method, logger_method):
        session_ctx = make_ctx()
        mock_log = MagicMock()
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), mock_log)
        getattr(ctx, method)("test message")
        getattr(mock_log, logger_method).assert_called_once_with("script: %s", "test message")


class TestScriptContextProperties:
    """ScriptContext property accessors."""

    def test_gmcp_property(self):
        ctx, sctx = make_script_ctx()
        assert ctx.gmcp is sctx.gmcp_data

    def test_room_id(self):
        ctx, _ = make_script_ctx()
        assert ctx.room_id == "42"

    def test_captures(self):
        ctx, _ = make_script_ctx()
        assert ctx.captures["Kills"] == 5


class TestConditionsMet:
    """ScriptContext.conditions_met and condition_met checks."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "gmcp_data, conditions, expected",
        [
            (
                {"Char.Vitals": {"hp": "50", "maxhp": "100", "mp": "80", "maxmp": "100"}},
                (("hp%", "<", 100), ("mp%", ">", 50)),
                True,
            ),
            (
                {"Char.Vitals": {"hp": "50", "maxhp": "100", "mp": "80", "maxmp": "100"}},
                [("hp%", "<", 100), ("mp%", ">", 50)],
                True,
            ),
        ],
    )
    async def test_immediate_result(self, gmcp_data, conditions, expected):
        ctx, _ = make_script_ctx(gmcp_data)
        if isinstance(conditions, list):
            result = await ctx.conditions_met(conditions)
        else:
            result = await ctx.conditions_met(*conditions)
        assert result is expected

    @pytest.mark.asyncio
    async def test_one_false_waits_for_update(self):
        ctx, sctx = make_script_ctx(
            {"Char.Vitals": {"hp": "100", "maxhp": "100", "mp": "80", "maxmp": "100"}}
        )
        task = asyncio.ensure_future(ctx.conditions_met(("hp%", "<", 100), ("mp%", ">", 50)))
        await asyncio.sleep(0)
        assert not task.done()
        sctx.gmcp_data["Char.Vitals"]["hp"] = "50"
        evt = sctx.gmcp.any_update
        evt.set()
        evt.clear()
        await asyncio.sleep(0)
        assert task.done()
        assert task.result() is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "gmcp_data, method, args, expected",
        [
            ({"Char.Vitals": {"hp": "100", "maxhp": "100"}}, "conditions_met", (("hp%", "<", 50),), False),
            ({"Char.Vitals": {"hp": "100", "maxhp": "100"}}, "condition_met", ("hp%", "<", 50), False),
            ({"Char.Vitals": {"hp": "50", "maxhp": "100"}}, "condition_met", ("hp%", "<", 100), True),
        ],
    )
    async def test_timeout_and_immediate(self, gmcp_data, method, args, expected):
        ctx, _ = make_script_ctx(gmcp_data)
        kwargs = {"timeout": 0.05} if not expected else {}
        result = await getattr(ctx, method)(*args, **kwargs)
        assert result is expected


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
        await ctx.send("look", wait_prompt=False)
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
        await ctx.send("north|south", wait_prompt=False)
        assert len([s for s in sent if s.strip()]) == 2

    @pytest.mark.asyncio
    async def test_send_waits_for_prompt_by_default(self):
        session_ctx = make_ctx()
        session_ctx.prompt.wait_fn = None
        session_ctx.prompt.ready = None
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        sent = []
        session_ctx.writer.write.side_effect = sent.append
        asyncio.get_event_loop().call_soon(buf.on_prompt)
        await ctx.send("look")
        assert any("look" in s for s in sent)



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

    @pytest.mark.asyncio
    async def test_duplicate_start_raises(self):
        mod = make_fake_module("run", "await asyncio.sleep(10)")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"dup_script": mod}):
            mgr.start_script(session_ctx, "dup_script")
            with pytest.raises(ValueError, match="already running"):
                mgr.start_script(session_ctx, "dup_script")

        assert mgr.active_scripts().count("dup_script") == 1
        mgr.stop_script(None)

    @pytest.mark.asyncio
    async def test_restart_after_stop_succeeds(self):
        """Stopping then immediately restarting a script does not raise."""
        mod = make_fake_module("run", "await asyncio.sleep(10)")
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {"restart_script": mod}):
            mgr.start_script(session_ctx, "restart_script")
            mgr.stop_script("restart_script")
            task2 = mgr.start_script(session_ctx, "restart_script")

        assert isinstance(task2, asyncio.Task)
        await asyncio.sleep(0.05)
        assert "restart_script" in mgr.active_scripts()
        mgr.stop_script(None)
        await asyncio.sleep(0.05)
        assert mgr.active_scripts() == []


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


class TestScriptContextEventChanged:
    """ScriptContext.room_changed and gmcp_changed awaitables."""

    @pytest.mark.asyncio
    async def test_room_changed_fires(self):
        ctx, sctx = make_script_ctx()
        sctx.room.changed = asyncio.Event()

        async def pulse():
            await asyncio.sleep(0.05)
            sctx.room.changed.set()
            sctx.room.changed.clear()

        asyncio.ensure_future(pulse())
        assert await ctx.room_changed(timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_room_changed_timeout(self):
        ctx, sctx = make_script_ctx()
        sctx.room.changed = asyncio.Event()
        assert await ctx.room_changed(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_gmcp_changed_fires(self):
        ctx, sctx = make_script_ctx()
        sctx.gmcp.package_events = {}

        async def pulse():
            await asyncio.sleep(0.05)
            evt = sctx.gmcp.package_events.get("Char.Vitals")
            if evt is not None:
                evt.set()
                evt.clear()

        asyncio.ensure_future(pulse())
        assert await ctx.gmcp_changed("Char.Vitals", timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_gmcp_changed_timeout(self):
        ctx, sctx = make_script_ctx()
        sctx.gmcp.package_events = {}
        assert await ctx.gmcp_changed("Char.Vitals", timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_different_packages_independent(self):
        ctx, sctx = make_script_ctx()
        sctx.gmcp.package_events = {}

        async def pulse():
            await asyncio.sleep(0.05)
            evt = sctx.gmcp.package_events.get("Char.Vitals")
            if evt is not None:
                evt.set()
                evt.clear()

        asyncio.ensure_future(pulse())
        assert await ctx.gmcp_changed("Room.Info", timeout=0.02) is False
        assert await ctx.gmcp_changed("Char.Vitals", timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_any_update_fires(self):
        ctx, sctx = make_script_ctx()

        async def pulse():
            await asyncio.sleep(0.02)
            sctx.gmcp.any_update.set()
            sctx.gmcp.any_update.clear()

        asyncio.ensure_future(pulse())
        assert await ctx.gmcp_changed(timeout=1.0) is True

    @pytest.mark.asyncio
    async def test_any_update_timeout(self):
        ctx, sctx = make_script_ctx()
        assert await ctx.gmcp_changed(timeout=0.02) is False


class TestScriptContextWalk:
    """ScriptContext.walk_active and stop_walk."""

    @pytest.mark.parametrize(
        "discover, randomwalk, travel, expected",
        [
            (None, None, None, False),
            ("running", None, None, True),
            ("done", "done", "done", False),
        ],
    )
    def test_walk_active(self, discover, randomwalk, travel, expected):
        _, sctx = make_script_ctx()
        for attr, val in [("discover_task", discover), ("randomwalk_task", randomwalk), ("travel_task", travel)]:
            if val is None:
                setattr(sctx.walk, attr, None)
            else:
                task = MagicMock()
                task.done.return_value = val == "done"
                setattr(sctx.walk, attr, task)
        ctx = scripts.ScriptContext(sctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.walk_active is expected

    @pytest.mark.parametrize(
        "done, should_cancel",
        [
            (False, True),
            (True, False),
        ],
    )
    def test_stop_walk(self, done, should_cancel):
        _, sctx = make_script_ctx()
        task = MagicMock()
        task.done.return_value = done
        sctx.walk.discover_task = task
        sctx.walk.randomwalk_task = None
        sctx.walk.travel_task = None
        ctx = scripts.ScriptContext(sctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        ctx.stop_walk()
        if should_cancel:
            task.cancel.assert_called_once()
        else:
            task.cancel.assert_not_called()


class TestScriptContextRunningScripts:
    """ScriptContext.running_scripts."""

    def test_no_manager_returns_empty(self):
        session_ctx = make_ctx()
        session_ctx.scripts.manager = None
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.running_scripts == []

    def test_delegates_to_active_scripts(self):
        session_ctx = make_ctx()
        mgr = MagicMock()
        mgr.active_scripts.return_value = ["combat.hunt", "healer"]
        session_ctx.scripts.manager = mgr
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.running_scripts == ["combat.hunt", "healer"]


def make_dispatch_hooks(mgr, echoed):
    """Return a minimal DispatchHooks wired to *mgr* and an echo recorder."""
    ctx = make_ctx()
    ctx.scripts.manager = mgr
    return DispatchHooks(
        ctx=ctx, log=logging.getLogger("test"), wait_fn=None, send_fn=lambda s: None, echo_fn=echoed.append
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

    @pytest.mark.asyncio
    async def test_await_sets_await_script_during_run(self):
        """await_script on ctx.walk is set while the script runs and cleared after."""
        observed = []
        mod = make_fake_module("run", "observed.append(ctx._ctx.walk.await_script)")
        mod.__dict__["observed"] = observed
        mgr = scripts.ScriptManager()
        echoed = []
        hooks = make_dispatch_hooks(mgr, echoed)

        with patch.dict(sys.modules, {"obs_script": mod}):
            await dispatch_one("`await obs_script`", 0, 0, frozenset(), hooks)

        assert observed == ["obs_script"]
        assert hooks.ctx.walk.await_script == ""

    @pytest.mark.asyncio
    async def test_await_clears_await_script_on_cancel(self):
        """await_script is cleared even when the enclosing task is cancelled."""
        mod = make_fake_module("run", "await asyncio.sleep(10)")
        mgr = scripts.ScriptManager()
        echoed = []
        hooks = make_dispatch_hooks(mgr, echoed)

        with patch.dict(sys.modules, {"slow_script": mod}):
            outer = asyncio.ensure_future(dispatch_one("`await slow_script`", 0, 0, frozenset(), hooks))
            await asyncio.sleep(0)
            outer.cancel()
            try:
                await outer
            except asyncio.CancelledError:
                pass

        assert hooks.ctx.walk.await_script == ""


class TestScriptManagerExceptionReporting:
    """ScriptManager reports script exceptions via ctx.print."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body, script_name, expected_substr",
        [
            ("assert False, 'boom'", "fail_assert", "AssertionError"),
            ("raise ValueError('bad value')", "fail_value", "bad value"),
        ],
    )
    async def test_exception_printed(self, body, script_name, expected_substr):
        mod = make_fake_module("run", body)
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch.dict(sys.modules, {script_name: mod}):
            mgr.start_script(session_ctx, script_name)

        await asyncio.sleep(0.1)
        calls = session_ctx.prompt.echo.call_args_list
        assert calls
        printed = "\n".join(str(c) for c in calls)
        assert expected_substr in printed

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
    @pytest.mark.parametrize(
        "side_effect, script_name, expected_substr",
        [
            (SyntaxError("unexpected EOF"), "bad_script", "SyntaxError"),
        ],
    )
    async def test_import_error_printed(self, side_effect, script_name, expected_substr):
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch("importlib.import_module", side_effect=side_effect):
            mgr.start_script(session_ctx, script_name)

        await asyncio.sleep(0.1)
        calls = session_ctx.prompt.echo.call_args_list
        assert calls
        printed = "\n".join(str(c) for c in calls)
        assert expected_substr in printed

    @pytest.mark.asyncio
    async def test_import_error_returns_task(self):
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()

        with patch("importlib.import_module", side_effect=ImportError("no module named 'missing'")):
            task = mgr.start_script(session_ctx, "missing_script")

        assert task is not None
        await asyncio.sleep(0.1)
        assert task.done()

    @pytest.mark.asyncio
    async def test_unawaited_coroutine_warning_captured(self, capsys):
        received = []

        async def bad_script(ctx, *args):
            async def internal_coro():
                pass

            internal_coro()

        mod = types.ModuleType("warn_test_script")
        mod.run = bad_script
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()
        session_ctx.prompt.echo.side_effect = received.append

        with patch.dict(sys.modules, {"warn_test_script": mod}):
            mgr.start_script(session_ctx, "warn_test_script")

        await asyncio.sleep(0.1)
        captured = capsys.readouterr()
        assert "RuntimeWarning" not in captured.err
        assert any("RuntimeWarning" in msg for msg in received)

    @pytest.mark.asyncio
    async def test_unawaited_coroutine_shows_location(self, capsys):
        received = []

        async def locatable_script(ctx, *args):
            async def inner():
                pass

            inner()

        mod = types.ModuleType("loc_warn_script")
        mod.run = locatable_script
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()
        session_ctx.prompt.echo.side_effect = received.append

        with patch.dict(sys.modules, {"loc_warn_script": mod}):
            mgr.start_script(session_ctx, "loc_warn_script")

        await asyncio.sleep(0.1)
        assert any(":" in msg and "RuntimeWarning" in msg for msg in received)

    @pytest.mark.asyncio
    async def test_normal_exception_still_reported_with_warnings_active(self):
        received = []

        async def failing_with_warning(ctx, *args):
            async def inner():
                pass

            inner()
            raise ValueError("deliberate error")

        mod = types.ModuleType("mixed_script")
        mod.run = failing_with_warning
        mgr = scripts.ScriptManager()
        session_ctx = make_ctx()
        session_ctx.prompt.echo.side_effect = received.append

        with patch.dict(sys.modules, {"mixed_script": mod}):
            mgr.start_script(session_ctx, "mixed_script")

        await asyncio.sleep(0.1)
        assert any("ValueError" in msg for msg in received)
        assert any("deliberate error" in msg for msg in received)


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



class TestCommandState:
    """session_context.CommandState history and waiter notification."""

    def test_record_appends_to_history(self):
        cs = session_context.CommandState()
        cs.record("look")
        assert list(cs.history) == ["look"]

    def test_record_filters_blank(self):
        cs = session_context.CommandState()
        cs.record("   ")
        assert list(cs.history) == []

    @pytest.mark.asyncio
    async def test_record_notifies_single_waiter(self):
        cs = session_context.CommandState()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        cs.waiters.append(fut)
        cs.record("inventory")
        assert await fut == "inventory"

    @pytest.mark.asyncio
    async def test_record_notifies_multiple_waiters(self):
        cs = session_context.CommandState()
        loop = asyncio.get_running_loop()
        fut1 = loop.create_future()
        fut2 = loop.create_future()
        cs.waiters.extend([fut1, fut2])
        cs.record("score")
        assert await fut1 == "score"
        assert await fut2 == "score"

    @pytest.mark.asyncio
    async def test_record_clears_waiters_after_notify(self):
        cs = session_context.CommandState()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        cs.waiters.append(fut)
        cs.record("hp")
        assert cs.waiters == []

    def test_record_does_not_buffer_before_first_waiter(self):
        cs = session_context.CommandState()
        cs.record("pre_script")
        assert list(cs.buf) == []

    def test_record_buffers_when_no_waiters_after_first_waiter(self):
        cs = session_context.CommandState()
        cs.ever_had_waiter = True
        cs.record("look")
        assert list(cs.buf) == ["look"]

    def test_record_does_not_buffer_when_waiter_present(self):
        cs = session_context.CommandState()
        cs.ever_had_waiter = True
        fut = MagicMock()
        fut.done.return_value = False
        cs.waiters.append(fut)
        cs.record("look")
        assert list(cs.buf) == []

    def test_record_buffers_multiple_commands_in_order(self):
        cs = session_context.CommandState()
        cs.ever_had_waiter = True
        cs.record("north")
        cs.record("kill goblin")
        assert list(cs.buf) == ["north", "kill goblin"]



class TestScriptContextCommandHistory:
    """ScriptContext command_history and last_command properties."""

    def test_history_empty_initially(self):
        session_ctx = make_ctx()
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.command_history == []

    def test_last_command_none_initially(self):
        session_ctx = make_ctx()
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.last_command is None

    def test_last_command_after_record(self):
        session_ctx = make_ctx()
        session_ctx.commands.record("look")
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.last_command == "look"

    def test_command_history_after_records(self):
        session_ctx = make_ctx()
        session_ctx.commands.record("look")
        session_ctx.commands.record("inventory")
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert ctx.command_history == ["look", "inventory"]


class TestScriptContextCommandIssued:
    """ScriptContext.command_issued async primitive."""

    @pytest.mark.asyncio
    async def test_command_issued_times_out(self):
        session_ctx = make_ctx()
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        result = await ctx.command_issued(timeout=0.05)
        assert result is None

    @pytest.mark.asyncio
    async def test_command_issued_returns_command_from_record(self):
        session_ctx = make_ctx()
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))

        async def trigger_cmd():
            await asyncio.sleep(0.05)
            session_ctx.commands.record("kill goblin")

        asyncio.ensure_future(trigger_cmd())
        result = await ctx.command_issued(timeout=1.0)
        assert result == "kill goblin"

    @pytest.mark.asyncio
    async def test_command_issued_multiple_waiters_all_receive(self):
        session_ctx = make_ctx()
        ctx1 = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        ctx2 = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))

        async def trigger_cmd():
            await asyncio.sleep(0.05)
            session_ctx.commands.record("flee")

        asyncio.ensure_future(trigger_cmd())
        r1, r2 = await asyncio.gather(ctx1.command_issued(timeout=1.0), ctx2.command_issued(timeout=1.0))
        assert r1 == "flee"
        assert r2 == "flee"

    @pytest.mark.asyncio
    async def test_command_issued_ignores_commands_before_first_call(self):
        """Commands recorded before command_issued is ever called are not returned."""
        session_ctx = make_ctx()
        session_ctx.commands.record("login_trigger")
        session_ctx.commands.record("pre_script_l")
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))

        async def later():
            await asyncio.sleep(0.05)
            session_ctx.commands.record("new_command")

        asyncio.ensure_future(later())
        result = await ctx.command_issued(timeout=1.0)
        assert result == "new_command"

    @pytest.mark.asyncio
    async def test_command_issued_returns_buffered_command_immediately(self):
        session_ctx = make_ctx()
        session_ctx.commands.ever_had_waiter = True
        session_ctx.commands.buf.append("buffered")
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        result = await ctx.command_issued(timeout=0.0)
        assert result == "buffered"

    @pytest.mark.asyncio
    async def test_command_issued_drains_buffer_in_order(self):
        session_ctx = make_ctx()
        session_ctx.commands.ever_had_waiter = True
        session_ctx.commands.buf.extend(["first", "second"])
        ctx = scripts.ScriptContext(session_ctx, scripts.ScriptOutputBuffer(), logging.getLogger("test"))
        assert await ctx.command_issued(timeout=0.0) == "first"
        assert await ctx.command_issued(timeout=0.0) == "second"
