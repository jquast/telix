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
from telix.client_repl_commands import SCRIPT_CMD_RE, STOPSCRIPT_CMD_RE

# ---------------------------------------------------------------------------
# Backtick regex constants
# ---------------------------------------------------------------------------


class TestScriptCmdRe:
    """SCRIPT_CMD_RE matches backtick-enclosed script commands."""

    def test_matches_bare_module(self):
        m = SCRIPT_CMD_RE.match("`script demo`")
        assert m is not None
        assert m.group(1) == "demo"

    def test_matches_dotted_function(self):
        m = SCRIPT_CMD_RE.match("`script combat.hunt`")
        assert m is not None
        assert m.group(1) == "combat.hunt"

    def test_matches_with_args(self):
        m = SCRIPT_CMD_RE.match("`script rooms.goto 12345`")
        assert m is not None
        assert m.group(1) == "rooms.goto 12345"

    def test_does_not_match_plain_text(self):
        assert SCRIPT_CMD_RE.match("script demo") is None

    def test_case_insensitive(self):
        assert SCRIPT_CMD_RE.match("`SCRIPT demo`") is not None


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
    ctx.current_room_num = "42"
    ctx.captures = {"Kills": 5}
    ctx.room_graph = MagicMock()
    ctx.room_graph.adj = {"42": {"north": "43", "south": "41"}}
    ctx.room_graph.find_path.return_value = ["north", "north"]
    ctx.room_graph.get_room.return_value = MagicMock(name="Forest", area="Wilds")
    ctx.echo_command = MagicMock()
    ctx.wait_for_prompt = None
    ctx.prompt_ready = None
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
        session_ctx.room_graph = None
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
        session_ctx.echo_command.assert_called_once_with("hello world")

    def test_no_echo_command_does_not_crash(self):
        session_ctx = make_ctx()
        session_ctx.echo_command = None
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        ctx.print("silent")


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
        session_ctx.wait_for_prompt = None
        session_ctx.prompt_ready = None
        buf = scripts.ScriptOutputBuffer()
        ctx = scripts.ScriptContext(session_ctx, buf, logging.getLogger("test"))
        sent = []
        session_ctx.writer.write.side_effect = sent.append
        await ctx.send("look")
        assert any("look" in s for s in sent)

    @pytest.mark.asyncio
    async def test_chained_commands(self):
        session_ctx = make_ctx()
        session_ctx.wait_for_prompt = None
        session_ctx.prompt_ready = None
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
        session_ctx.echo_command.assert_called()

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
