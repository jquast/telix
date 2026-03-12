"""Tests for telix.trigger module."""

from __future__ import annotations

# std imports
import re
import json
import types
import asyncio
import logging

# 3rd party
import pytest

from telix import session_context

# local
from telix.trigger import (
    TriggerRule,
    SearchBuffer,
    TriggerEngine,
    ExclusiveState,
    compare,
    load_triggers,
    parse_entries,
    save_triggers,
    check_condition,
    substitute_groups,
    resolve_group_value,
    extract_group_source,
)


@pytest.mark.parametrize(
    "chunks, expected_lines, expected_partial",
    [
        (["\x1b[31mhello\x1b[m\n"], ["hello"], ""),
        (["partial"], [], "partial"),
        (["partial", " more\n"], ["partial more"], ""),
        (["line1\nline2\nline3\n"], ["line1", "line2", "line3"], ""),
        (["line1\npartial"], ["line1"], "partial"),
        (["Sailor\r\nGuard\r\n"], ["Sailor", "Guard"], ""),
        ([""], [], ""),
    ],
)
def test_search_buffer_add_text(chunks, expected_lines, expected_partial):
    buf = SearchBuffer(max_lines=100)
    for chunk in chunks:
        buf.add_text(chunk)
    assert buf.lines == expected_lines
    assert buf.partial == expected_partial


def test_search_buffer_cull_old_lines():
    buf = SearchBuffer(max_lines=3)
    buf.add_text("a\nb\nc\nd\ne\n")
    assert len(buf.lines) == 3
    assert buf.lines == ["c", "d", "e"]


def test_search_buffer_cull_adjusts_match_position():
    buf = SearchBuffer(max_lines=3)
    buf.add_text("a\nb\n")
    buf.last_match_line = 1
    buf.last_match_col = 0
    buf.add_text("c\nd\ne\n")
    assert buf.last_match_line >= 0
    assert len(buf.lines) == 3


def test_search_buffer_cull_preserves_nonzero_match_line():
    buf = SearchBuffer(max_lines=3)
    buf.add_text("a\nb\nc\nd\n")
    buf.last_match_line = 3
    buf.last_match_col = 1
    buf.add_text("e\n")
    assert buf.last_match_line == 2
    assert buf.last_match_col == 1


@pytest.mark.parametrize(
    "text,match_line,match_col,expected",
    [
        ("hello\nworld\n", None, None, "hello\nworld"),
        ("aaa\nbbb\nccc\n", 1, 0, "bbb\nccc"),
        ("aaa\nbbb\n", 0, 2, "a\nbbb"),
        ("one\n", 5, 0, ""),
        ("line1\npartial prompt", None, None, "line1\npartial prompt"),
        ("just a prompt", None, None, "just a prompt"),
    ],
)
def test_search_buffer_searchable_text(text, match_line, match_col, expected):
    buf = SearchBuffer(max_lines=100)
    buf.add_text(text)
    if match_line is not None:
        buf.last_match_line = match_line
        buf.last_match_col = match_col
    assert buf.get_searchable_text() == expected


def test_search_buffer_advance_match():
    buf = SearchBuffer(max_lines=100)
    buf.add_text("hello world\n")
    searchable = buf.get_searchable_text()
    m = re.search("world", searchable)
    assert m is not None
    buf.advance_match(m.start(), len(m.group(0)))
    remaining = buf.get_searchable_text()
    assert "world" not in remaining


SK = "test.host:23"


def test_load_triggers_valid(tmp_path):
    fp = tmp_path / "triggers.json"
    fp.write_text(
        json.dumps(
            {
                SK: {
                    "triggers": [
                        {"pattern": r"\d+ gold", "reply": "get gold;"},
                        {"pattern": r"(\w+) attacks", "reply": "kill \\1;"},
                    ]
                }
            }
        )
    )
    rules = load_triggers(str(fp), SK)
    assert len(rules) == 2
    assert rules[0].pattern.pattern == r"\d+ gold"
    assert rules[0].reply == "get gold;"
    assert rules[1].reply == "kill \\1;"


def test_load_triggers_missing_file():
    with pytest.raises(FileNotFoundError):
        load_triggers("/nonexistent/path.json", SK)


def test_load_triggers_invalid_regex(tmp_path):
    fp = tmp_path / "bad.json"
    fp.write_text(json.dumps({SK: {"triggers": [{"pattern": "[invalid", "reply": "x"}]}}))
    with pytest.raises(ValueError, match="Invalid trigger pattern"):
        load_triggers(str(fp), SK)


def test_load_triggers_empty_pattern_skipped(tmp_path):
    fp = tmp_path / "empty.json"
    fp.write_text(json.dumps({SK: {"triggers": [{"pattern": "", "reply": "x"}, {"pattern": "valid", "reply": "y"}]}}))
    rules = load_triggers(str(fp), SK)
    assert len(rules) == 1


def test_load_triggers_empty_list(tmp_path):
    fp = tmp_path / "empty.json"
    fp.write_text(json.dumps({SK: {"triggers": []}}))
    assert not load_triggers(str(fp), SK)


def test_load_triggers_no_session(tmp_path):
    fp = tmp_path / "triggers.json"
    fp.write_text(json.dumps({"other:23": {"triggers": [{"pattern": "x", "reply": "y"}]}}))
    assert not load_triggers(str(fp), SK)


def test_save_triggers_roundtrip(tmp_path):
    fp = tmp_path / "triggers.json"
    original = [
        TriggerRule(pattern=re.compile(r"\d+ gold"), reply="get gold;"),
        TriggerRule(pattern=re.compile(r"(\w+) attacks", re.MULTILINE | re.DOTALL), reply="kill \\1;"),
    ]
    save_triggers(str(fp), original, SK)
    loaded = load_triggers(str(fp), SK)
    assert len(loaded) == len(original)
    for orig, restored in zip(original, loaded, strict=False):
        assert orig.pattern.pattern == restored.pattern.pattern
        assert orig.reply == restored.reply


def test_save_triggers_preserves_other_sessions(tmp_path):
    fp = tmp_path / "triggers.json"
    r1 = [TriggerRule(pattern=re.compile("a"), reply="b")]
    r2 = [TriggerRule(pattern=re.compile("c"), reply="d")]
    save_triggers(str(fp), r1, "host1:23")
    save_triggers(str(fp), r2, "host2:23")
    assert len(load_triggers(str(fp), "host1:23")) == 1
    assert len(load_triggers(str(fp), "host2:23")) == 1


def test_save_triggers_empty(tmp_path):
    fp = tmp_path / "triggers.json"
    save_triggers(str(fp), [], SK)
    assert not load_triggers(str(fp), SK)


def test_save_triggers_unicode(tmp_path):
    fp = tmp_path / "triggers.json"
    rules = [TriggerRule(pattern=re.compile("héllo"), reply="bonjour;")]
    save_triggers(str(fp), rules, SK)
    loaded = load_triggers(str(fp), SK)
    assert loaded[0].pattern.pattern == "héllo"
    assert loaded[0].reply == "bonjour;"


@pytest.mark.parametrize(
    "regex, text, template, expected",
    [
        (r"(\w+) gold", "50 gold coins", "take \\1 gold", "take 50 gold"),
        (r"(\w+) (\w+)", "hello world", "\\2 \\1", "world hello"),
        (r"hello", "hello world", "say hello", "say hello"),
        (r"(\w+)", "hello", "\\1 \\5", "hello \\5"),
    ],
)
def test_substitute_groups(regex, text, template, expected):
    m = re.search(regex, text)
    assert m is not None
    assert substitute_groups(template, m) == expected


@pytest.mark.parametrize(
    "pattern, group, expected",
    [
        (r"(amplifier|enhancer|shield)", 1, "amplifier|enhancer|shield"),
        (r"(foo)(bar|baz)", 2, "bar|baz"),
        (r"(?:prefix)(real)", 1, "real"),
        (r"(?P<item>sword|axe)", 1, "sword|axe"),
        (r"(foo)", 2, None),
        (r"((inner)outer)", 1, "(inner)outer"),
        (r"((inner)outer)", 2, "inner"),
    ],
)
def test_extract_group_source(pattern, group, expected):
    assert extract_group_source(pattern, group) == expected


@pytest.mark.parametrize(
    "captured, expected",
    [("Shield", "shield"), ("SHIELD", "shield"), ("amplifier", "amplifier"), ("ENHANCER", "enhancer")],
)
def test_resolve_group_value_case_insensitive_alternation(captured, expected):
    pat_src = r"^A level \d+.*(amplifier|enhancer|shield)"
    assert resolve_group_value(captured, pat_src, 1, re.IGNORECASE) == expected


def test_resolve_group_value_case_sensitive_passthrough():
    pat_src = r"(amplifier|enhancer|shield)"
    assert resolve_group_value("Shield", pat_src, 1, 0) == "Shield"


def test_resolve_group_value_non_literal_fallback():
    pat_src = r"level (\d+)"
    assert resolve_group_value("42", pat_src, 1, re.IGNORECASE) == "42"


def test_substitute_groups_case_insensitive_pattern():
    pat = re.compile(r"^A level \d+.*(amplifier|enhancer|shield)", re.IGNORECASE)
    m = pat.search("A level 5 Fine Shield")
    assert m is not None
    assert substitute_groups("get \\1;gl", m) == "get shield;gl"


def test_substitute_groups_case_sensitive_unchanged():
    pat = re.compile(r".*(Shield|Sword)")
    m = pat.search("a Fine Shield here")
    assert m is not None
    assert substitute_groups("get \\1", m) == "get Shield"


def test_compare_unknown_operator_raises():
    with pytest.raises(ValueError, match="unknown operator"):
        compare(50, "~", 30)


def mock_writer():
    """Create a mock ctx+writer that records write() calls."""
    written: list[str] = []
    writer = types.SimpleNamespace(write=written.append)
    ctx = types.SimpleNamespace(
        writer=writer,
        gmcp_data={},
        walk=types.SimpleNamespace(active_command=None, active_command_time=0.0, randomwalk_active=False),
        commands=session_context.CommandState(),
    )
    ctx.log = logging.getLogger("test")
    return ctx, written


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern,flags,reply,feed_text,expected",
    [
        (r"hello", 0, "world;", "hello\n", ["world\r\n"]),
        (r"hello", 0, "world;", "hello", ["world\r\n"]),
        (r"a (\w+) (pheasant|duck)", 0, "kill \\2;", "a black pheasant\n", ["kill pheasant\r\n"]),
        (r"start.*end", re.DOTALL, "matched;", "start\nmiddle\nend\n", ["matched\r\n"]),
        (r"multi", 0, "cmd1;cmd2;", "multi\n", ["cmd1\r\n", "cmd2\r\n"]),
        (r"go", 0, "cmd1;cmd2;cmd3;", "go\n", ["cmd1\r\n", "cmd2\r\n", "cmd3\r\n"]),
        (r"trigger", 0, "no-cr-reply", "trigger\n", ["no-cr-reply\r\n"]),
        (r"trigger", 0, "`delay 0ms`;fast;", "trigger\n", ["fast\r\n"]),
    ],
)
async def test_trigger_engine_feed_and_match(pattern, flags, reply, feed_text, expected):
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(pattern, flags), reply=reply)]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed(feed_text)
    await asyncio.sleep(0.05)
    for exp in expected:
        assert any(exp in w for w in written)


@pytest.mark.asyncio
async def test_trigger_engine_mud_prompt_without_newline():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"What is your name\?"), reply="dingo;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("Welcome to the Mini-MUD!\n")
    await asyncio.sleep(0.05)
    assert not any("dingo" in w for w in written)
    engine.feed("What is your name? ")
    await asyncio.sleep(0.05)
    assert any("dingo\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_trigger_engine_no_double_trigger():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"hello"), reply="world;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("hello\n")
    await asyncio.sleep(0.05)
    count1 = len(written)
    engine.feed("more text\n")
    await asyncio.sleep(0.05)
    assert len(written) == count1


@pytest.mark.asyncio
async def test_trigger_engine_delay_execution():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"trigger"), reply="`delay 10ms`;delayed;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("trigger\n")
    await asyncio.sleep(0.005)
    assert not any("delayed" in w for w in written)
    await asyncio.sleep(0.03)
    assert any("delayed\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_trigger_engine_reply_chaining():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"alpha"), reply="`delay 20ms`;first;"),
        TriggerRule(pattern=re.compile(r"beta"), reply="second;"),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("alpha\n")
    await asyncio.sleep(0.005)
    assert not any("first" in w for w in written)
    await asyncio.sleep(0.05)
    assert any("first\r\n" in w for w in written)

    engine.feed("beta\n")
    await asyncio.sleep(0.05)
    assert any("second\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_trigger_engine_cancel():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"slow"), reply="`delay 1s`;result;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("slow\n")
    await asyncio.sleep(0.01)
    engine.cancel()
    await asyncio.sleep(0.02)
    assert not any("result" in w for w in written)


@pytest.mark.asyncio
async def test_trigger_engine_repeat_expansion():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"go"), reply="3e;2n;look;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("go\n")
    await asyncio.sleep(0.05)
    e_count = sum(1 for w in written if "e\r\n" in w)
    n_count = sum(1 for w in written if "n\r\n" in w)
    assert e_count == 3
    assert n_count == 2
    assert any("look\r\n" in w for w in written)


def test_trigger_engine_cancel_when_idle():
    writer, _ = mock_writer()
    engine = TriggerEngine([], writer, writer.log)
    engine.cancel()
    assert engine.reply_chain is None


@pytest.mark.parametrize(
    "command, expected_written",
    [
        ("", []),
        ("   ", []),
        ("look", ["look\r\n"]),
    ],
)
def test_send_command(command, expected_written):
    writer, written = mock_writer()
    engine = TriggerEngine([], writer, writer.log)
    engine.send_command(command)
    assert written == expected_written


def test_sent_commands_bounded(monkeypatch):
    monkeypatch.setenv("TELNETLIB3_SENT_COMMANDS_MAX", "5")
    writer, _ = mock_writer()
    engine = TriggerEngine([], writer, writer.log)
    assert engine.sent_commands_max == 5
    for i in range(10):
        engine.send_command(f"cmd{i}")
    assert len(engine.sent_commands) <= 5


@pytest.mark.asyncio
async def test_trigger_reply_without_semicolon_still_sends():
    writer, written = mock_writer()
    inserted: list[str] = []
    rules = [TriggerRule(pattern=re.compile(r"Items here: (\w+)"), reply=r"pick up \1")]
    engine = TriggerEngine(rules, writer, writer.log, insert_fn=inserted.append)
    engine.feed("Items here: sword\n")
    await asyncio.sleep(0.05)
    assert any("pick up sword\r\n" in w for w in written)
    assert not inserted


@pytest.mark.asyncio
async def test_trigger_insert_fn_with_cr_sends():
    writer, written = mock_writer()
    inserted: list[str] = []
    rules = [TriggerRule(pattern=re.compile(r"hello"), reply="world;")]
    engine = TriggerEngine(rules, writer, writer.log, insert_fn=inserted.append)
    engine.feed("hello\n")
    await asyncio.sleep(0.05)
    assert any("world\r\n" in w for w in written)
    assert not inserted


@pytest.mark.asyncio
async def test_trigger_no_insert_fn_sends_without_cr():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"hello"), reply="world")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("hello\n")
    await asyncio.sleep(0.05)
    assert any("world\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_trigger_wait_fn_called_before_send():
    """wait_fn is awaited before second command, first sends immediately."""
    writer, written = mock_writer()
    wait_calls: list[float] = []

    async def fake_wait() -> None:
        wait_calls.append(asyncio.get_event_loop().time())

    rules = [TriggerRule(pattern=re.compile(r"go"), reply="cmd1;cmd2;")]
    engine = TriggerEngine(rules, writer, writer.log, wait_fn=fake_wait)
    engine.feed("go\n")
    await asyncio.sleep(0.02)
    assert len(wait_calls) == 1
    assert any("cmd1\r\n" in w for w in written)
    assert any("cmd2\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_trigger_wait_fn_none_no_pacing():
    """When wait_fn is None, commands send immediately without pacing."""
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"go"), reply="cmd1;cmd2;")]
    engine = TriggerEngine(rules, writer, writer.log, wait_fn=None)
    engine.feed("go\n")
    await asyncio.sleep(0.05)
    assert any("cmd1\r\n" in w for w in written)
    assert any("cmd2\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_trigger_wait_fn_timeout_does_not_hang():
    """wait_fn that blocks eventually times out and command still sends."""
    writer, written = mock_writer()
    never_set = asyncio.Event()

    async def blocking_wait() -> None:
        try:
            await asyncio.wait_for(never_set.wait(), timeout=0.02)
        except asyncio.TimeoutError:
            pass

    rules = [TriggerRule(pattern=re.compile(r"go"), reply="cmd;")]
    engine = TriggerEngine(rules, writer, writer.log, wait_fn=blocking_wait)
    engine.feed("go\n")
    await asyncio.sleep(0.05)
    assert any("cmd\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_trigger_wait_fn_with_trailing_text():
    """First command sends immediately without wait_fn, even without ;."""
    writer, written = mock_writer()
    wait_calls: list[int] = []

    async def fake_wait() -> None:
        wait_calls.append(1)

    rules = [TriggerRule(pattern=re.compile(r"go"), reply="trailing")]
    engine = TriggerEngine(rules, writer, writer.log, wait_fn=fake_wait)
    engine.feed("go\n")
    await asyncio.sleep(0.02)
    assert len(wait_calls) == 0
    assert any("trailing\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_exclusive_rule_suppresses_later_matches():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"A (\w+) pheasant"), reply=r"kill \1;"),
        TriggerRule(pattern=re.compile(r"A (\w+) rabbit"), reply=r"kill \1;"),
        TriggerRule(pattern=re.compile(r"A (\w+) mouse"), reply=r"kill \1;"),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("A green pheasant\nA red rabbit\nA orange mouse\n")
    await asyncio.sleep(0.05)
    assert any("kill green\r\n" in w for w in written)
    assert not any("kill red\r\n" in w for w in written)
    assert not any("kill orange\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_exclusive_rule_index_tracks_active_rule():
    writer, _ = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"alpha"), reply="`delay 20ms`;a;"),
        TriggerRule(pattern=re.compile(r"beta"), reply="b;"),
        TriggerRule(pattern=re.compile(r"gamma"), reply="`delay 20ms`;g;"),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    assert engine.exclusive_active is False
    assert engine.exclusive_rule_index == 0

    engine.feed("alpha\n")
    await asyncio.sleep(0.01)
    assert engine.exclusive_active is True
    assert engine.exclusive_rule_index == 1

    await asyncio.sleep(0.05)
    assert engine.exclusive_active is False
    assert engine.exclusive_rule_index == 0

    engine.feed("gamma\n")
    await asyncio.sleep(0.01)
    assert engine.exclusive_active is True
    assert engine.exclusive_rule_index == 3


@pytest.mark.asyncio
async def test_exclusive_cleared_when_chain_completes():
    """Exclusive clears when the reply chain task completes."""
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"A (\w+) pheasant"), reply=r"kill \1;"),
        TriggerRule(pattern=re.compile(r"A (\w+) rabbit"), reply=r"kill \1;"),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("A green pheasant\nA red rabbit\n")
    await asyncio.sleep(0.05)
    assert any("kill green\r\n" in w for w in written)
    assert not any("kill red\r\n" in w for w in written)

    await asyncio.sleep(0.05)
    assert engine.exclusive_active is False

    engine.on_prompt()
    engine.feed("A red rabbit\n")
    engine.on_prompt()
    await asyncio.sleep(0.05)
    assert any("kill red\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_all_rules_exclusive_only_first_fires():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"A (\w+) pheasant"), reply=r"kill \1;"),
        TriggerRule(pattern=re.compile(r"A (\w+) rabbit"), reply=r"kill \1;"),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("A green pheasant\nA red rabbit\n")
    await asyncio.sleep(0.05)
    assert any("kill green\r\n" in w for w in written)
    assert not any("kill red\r\n" in w for w in written)


@pytest.mark.parametrize(
    "entries,checks",
    [
        (
            [{"pattern": "died", "reply": "look;", "always": True}, {"pattern": "hello", "reply": "world;"}],
            [(0, "always", True), (1, "always", False)],
        ),
        (
            [{"pattern": "a", "reply": "b;", "enabled": False}, {"pattern": "c", "reply": "d;"}],
            [(0, "enabled", False), (1, "enabled", True)],
        ),
        ([{"pattern": "bear", "reply": "kill bear;", "when": {"hp%": ">50"}}], [(0, "when", {"hp%": ">50"})]),
        ([{"pattern": "bear", "reply": "kill bear;"}], [(0, "when", {})]),
    ],
)
def test_parse_entries_field(entries, checks):
    rules = parse_entries(entries)
    for idx, field, expected in checks:
        assert getattr(rules[idx], field) == expected


@pytest.mark.parametrize(
    "rule_kwargs,field,exp0,exp1,json_key,json_in_0,json_absent_1",
    [
        ({"always": True}, "always", True, False, "always", True, True),
        ({"enabled": False}, "enabled", False, True, "enabled", False, True),
    ],
)
def test_save_triggers_field_roundtrip(tmp_path, rule_kwargs, field, exp0, exp1, json_key, json_in_0, json_absent_1):
    fp = tmp_path / "triggers.json"
    original = [
        TriggerRule(pattern=re.compile(r"(\w+) attacks"), reply=r"kill \1;", **rule_kwargs),
        TriggerRule(pattern=re.compile(r"foo"), reply="bar;"),
    ]
    save_triggers(str(fp), original, SK)
    loaded = load_triggers(str(fp), SK)
    assert getattr(loaded[0], field) == exp0
    assert getattr(loaded[1], field) == exp1

    with open(str(fp), encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data[SK]["triggers"]
    assert entries[0][json_key] == json_in_0
    if json_absent_1:
        assert json_key not in entries[1]


@pytest.mark.asyncio
async def test_exclusive_suppresses_feed_while_chain_active():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"monster"), reply="`delay 20ms`;kill;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("monster\n")
    await asyncio.sleep(0.01)
    assert engine.exclusive_active is True

    engine.feed("monster again\n")
    await asyncio.sleep(0.005)
    count = sum(1 for w in written if "kill\r\n" in w)
    assert count == 0

    await asyncio.sleep(0.05)
    count2 = sum(1 for w in written if "kill\r\n" in w)
    assert count2 == 1
    assert engine.exclusive_active is False


@pytest.mark.asyncio
async def test_always_rule_fires_during_exclusive():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"A (\w+) pheasant"), reply=r"kill \1;"),
        TriggerRule(pattern=re.compile(r"died\."), reply="look;", always=True),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("A green pheasant\n")
    await asyncio.sleep(0.05)
    assert any("kill green\r\n" in w for w in written)

    engine.feed("pheasant died.\n")
    await asyncio.sleep(0.05)
    assert any("look\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_disabled_rule_skipped_by_engine():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"hello"), reply="world;", enabled=False),
        TriggerRule(pattern=re.compile(r"hello"), reply="backup;"),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("hello\n")
    await asyncio.sleep(0.05)
    assert not any("world\r\n" in w for w in written)
    assert any("backup\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_disabled_always_rule_skipped():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"monster"), reply="kill;"),
        TriggerRule(pattern=re.compile(r"died\."), reply="look;", always=True, enabled=False),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("monster\n")
    await asyncio.sleep(0.05)
    engine.feed("pheasant died.\n")
    await asyncio.sleep(0.05)
    assert not any("look\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_prompt_cycle_dedup_blocks_same_rule():
    """Once on_prompt activates cycle tracking, a rule fires at most once per cycle."""
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"(^Corpse of|^\w+ times 'Corpse)", re.MULTILINE), reply="look in corpse;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()

    engine.feed("Corpse of Goldfish\n")
    engine.feed("Two times 'Corpse of Barracuda'\n")
    engine.on_prompt()  # triggers deferred match; dedup blocks second hit
    await asyncio.sleep(0.05)
    assert sum(1 for w in written if "look in corpse\r\n" in w) == 1

    engine.feed("Corpse of Goldfish\n")
    engine.on_prompt()  # new cycle, rule can fire again
    await asyncio.sleep(0.05)
    assert sum(1 for w in written if "look in corpse\r\n" in w) == 2


@pytest.mark.asyncio
async def test_prompt_cycle_dedup_different_rules_first_fires():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"gold"), reply="get gold;"),
        TriggerRule(pattern=re.compile(r"corpse"), reply="look corpse;"),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()

    engine.feed("gold and corpse\n")
    engine.on_prompt()
    await asyncio.sleep(0.05)
    assert sum(1 for w in written if "get gold\r\n" in w) == 1
    assert sum(1 for w in written if "look corpse\r\n" in w) == 0


@pytest.mark.asyncio
async def test_prompt_cycle_dedup_inactive_without_on_prompt():
    """Without on_prompt, cycle dedup is not active -- same rule fires twice."""
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"^trigger$", re.MULTILINE), reply="reply;")]
    engine = TriggerEngine(rules, writer, writer.log)

    engine.feed("trigger\n")
    await asyncio.sleep(0.05)
    engine.feed("trigger\n")
    await asyncio.sleep(0.05)
    assert sum(1 for w in written if "reply\r\n" in w) == 2


@pytest.mark.asyncio
async def test_prompt_cycle_dedup_always_rule():
    """Cycle dedup applies to always=True rules during exclusive mode."""
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"monster"), reply="`delay 10ms`;kill;"),
        TriggerRule(pattern=re.compile(r"corpse", re.MULTILINE), reply="loot;", always=True),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()

    engine.feed("monster\n")
    engine.on_prompt()
    await asyncio.sleep(0.01)
    assert engine.exclusive_active is True

    engine.feed("corpse here\n")
    await asyncio.sleep(0.05)
    assert sum(1 for w in written if "loot\r\n" in w) == 1

    engine.feed("another corpse\n")
    await asyncio.sleep(0.01)
    assert sum(1 for w in written if "loot\r\n" in w) == 1

    engine.on_prompt()
    engine.feed("corpse again\n")
    engine.on_prompt()
    await asyncio.sleep(0.01)
    assert sum(1 for w in written if "loot\r\n" in w) == 2


def test_search_buffer_clear_resets_lines_and_position():
    buf = SearchBuffer(max_lines=100)
    buf.add_text("line1\nline2\n")
    buf.last_match_line = 1
    buf.last_match_col = 3
    buf.clear()
    assert buf.lines == []
    assert buf.last_match_line == 0
    assert buf.last_match_col == 0


def test_search_buffer_clear_preserves_partial():
    buf = SearchBuffer(max_lines=100)
    buf.add_text("line1\nprompt>> ")
    assert buf.partial == "prompt>> "
    buf.clear()
    assert buf.lines == []
    assert buf.partial == "prompt>> "
    assert buf.get_searchable_text() == "prompt>> "


@pytest.mark.asyncio
async def test_search_buffer_wait_for_pattern_immediate():
    buf = SearchBuffer(max_lines=100)
    buf.add_text("The monster died.\n")
    pattern = re.compile(r"died\.", re.IGNORECASE)
    match = await buf.wait_for_pattern(pattern, timeout=1.0)
    assert match is not None
    assert match.group(0) == "died."


@pytest.mark.asyncio
async def test_search_buffer_wait_for_pattern_delayed():
    buf = SearchBuffer(max_lines=100)
    pattern = re.compile(r"died\.", re.IGNORECASE)

    async def feed_later():
        await asyncio.sleep(0.05)
        buf.add_text("The monster died.\n")

    asyncio.ensure_future(feed_later())
    match = await buf.wait_for_pattern(pattern, timeout=2.0)
    assert match is not None
    assert match.group(0) == "died."


@pytest.mark.asyncio
async def test_search_buffer_wait_for_pattern_timeout():
    buf = SearchBuffer(max_lines=100)
    pattern = re.compile(r"never_matches")
    match = await buf.wait_for_pattern(pattern, timeout=0.05)
    assert match is None


@pytest.mark.asyncio
async def test_search_buffer_wait_for_pattern_case_sensitive():
    buf = SearchBuffer(max_lines=100)
    buf.add_text("the monster DIED.\n")
    pattern_ci = re.compile(r"died\.", re.IGNORECASE)
    match = await buf.wait_for_pattern(pattern_ci, timeout=0.1)
    assert match is not None

    buf2 = SearchBuffer(max_lines=100)
    buf2.add_text("the monster DIED.\n")
    pattern_cs = re.compile(r"died\.")
    match2 = await buf2.wait_for_pattern(pattern_cs, timeout=0.1)
    assert match2 is None


@pytest.mark.asyncio
async def test_on_prompt_clears_buffer_prevents_stale_rematch():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"^Corpse of", re.MULTILINE), reply="look in corpse;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()

    engine.feed("Corpse of Goldfish\n")
    engine.on_prompt()  # triggers deferred match
    await asyncio.sleep(0.05)
    assert sum(1 for w in written if "look in corpse\r\n" in w) == 1

    engine.feed("No corpse here\n")
    engine.on_prompt()
    await asyncio.sleep(0.05)
    assert sum(1 for w in written if "look in corpse\r\n" in w) == 1


@pytest.mark.asyncio
async def test_on_prompt_clears_buffer_dotall_no_cross_record():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"Corpse contains:.*?(\d+ solaris)", re.DOTALL), reply=r"get all solaris;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()

    engine.feed("Corpse contains:\n   A Mini-Shield\n")
    engine.on_prompt()  # triggers deferred match (no match)
    await asyncio.sleep(0.05)
    assert not any("get all solaris" in w for w in written)

    engine.feed("19 solaris on the ground\n")
    engine.on_prompt()  # triggers deferred match (no cross-record match)
    await asyncio.sleep(0.05)
    assert not any("get all solaris" in w for w in written)


@pytest.mark.asyncio
async def test_cancel_clears_exclusive():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"A (\w+) is here"), reply=r"`delay 20ms`;kill \1;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("A shark is here\n")
    await asyncio.sleep(0.01)
    assert engine.exclusive_active is True

    engine.cancel()
    assert engine.exclusive_active is False
    assert engine.reply_pending is False


@pytest.mark.asyncio
async def test_sent_commands_not_matched_as_echo():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"corpse"), reply="look in corpse;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.send_command("look in corpse")
    await asyncio.sleep(0.05)
    written.clear()

    engine.on_prompt()
    engine.feed("look in corpse\n")
    engine.on_prompt()  # triggers deferred match (echo suppressed)
    await asyncio.sleep(0.05)
    assert not any("look in corpse" in w for w in written)

    engine.feed("There is a corpse here.\n")
    engine.on_prompt()  # triggers deferred match
    await asyncio.sleep(0.05)
    assert any("look in corpse\r\n" in w for w in written)


@pytest.mark.asyncio
async def test_reply_pending_tracks_chain():
    writer, _ = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"hello"), reply="world;")]
    engine = TriggerEngine(rules, writer, writer.log)
    assert engine.reply_pending is False

    engine.feed("hello\n")
    await asyncio.sleep(0.05)
    assert engine.reply_pending is False


@pytest.mark.asyncio
async def test_cycle_matched_tracks_matches():
    writer, _ = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"hello"), reply="world;")]
    engine = TriggerEngine(rules, writer, writer.log)
    assert engine.cycle_matched is False

    engine.feed("hello\n")
    await asyncio.sleep(0.05)
    assert engine.cycle_matched is True

    engine.on_prompt()
    assert engine.cycle_matched is False


def mock_writer_with_vitals(hp: int, maxhp: int, mp: int, maxmp: int):
    """Create a mock ctx with GMCP vitals data."""
    written: list[str] = []
    writer = types.SimpleNamespace(write=written.append)
    ctx = types.SimpleNamespace(
        writer=writer,
        log=logging.getLogger("test"),
        walk=types.SimpleNamespace(active_command=None, active_command_time=0.0, randomwalk_active=False),
        gmcp_data={"Char.Vitals": {"hp": str(hp), "maxhp": str(maxhp), "mp": str(mp), "maxmp": str(maxmp)}},
        captures={},
        commands=session_context.CommandState(),
    )
    return ctx, written


@pytest.mark.parametrize(
    "when, hp, maxhp, mp, maxmp, ok",
    [
        ({}, 50, 100, 50, 100, True),
        ({"hp%": ">50"}, 60, 100, 50, 100, True),
        ({"hp%": ">50"}, 50, 100, 50, 100, False),
        ({"hp%": ">50"}, 40, 100, 50, 100, False),
        ({"hp%": ">=50"}, 50, 100, 50, 100, True),
        ({"hp%": "<50"}, 40, 100, 50, 100, True),
        ({"hp%": "<50"}, 50, 100, 50, 100, False),
        ({"hp%": "<=50"}, 50, 100, 50, 100, True),
        ({"hp%": "=50"}, 50, 100, 50, 100, True),
        ({"hp%": "=50"}, 51, 100, 50, 100, False),
        ({"mp%": ">30"}, 80, 100, 40, 100, True),
        ({"mp%": ">30"}, 80, 100, 20, 100, False),
        ({"hp%": ">50", "mp%": ">30"}, 60, 100, 40, 100, True),
        ({"hp%": ">50", "mp%": ">30"}, 60, 100, 20, 100, False),
        ({"hp%": ">50", "mp%": ">30"}, 40, 100, 40, 100, False),
        ({"hp": ">50"}, 60, 100, 50, 100, True),
        ({"hp": ">50"}, 50, 100, 50, 100, False),
        ({"hp": ">=50"}, 50, 100, 50, 100, True),
        ({"mp": ">30"}, 80, 100, 40, 100, True),
        ({"mp": ">30"}, 80, 100, 20, 100, False),
        ({"hp": ">50", "mp": ">30"}, 60, 100, 40, 100, True),
        ({"hp": ">50", "mp": ">30"}, 60, 100, 20, 100, False),
    ],
)
def test_check_condition(when, hp, maxhp, mp, maxmp, ok):
    writer, _ = mock_writer_with_vitals(hp, maxhp, mp, maxmp)
    result, desc = check_condition(when, writer)
    assert result is ok
    if not ok:
        assert desc


def test_check_condition_no_gmcp():
    ctx = types.SimpleNamespace(gmcp_data=None)
    ok, desc = check_condition({"hp%": ">50"}, ctx)
    assert ok is True


def test_check_condition_no_vitals():
    ctx = types.SimpleNamespace(gmcp_data={})
    ok, desc = check_condition({"hp%": ">50"}, ctx)
    assert ok is True


def test_check_condition_zero_max():
    writer, _ = mock_writer_with_vitals(50, 0, 50, 100)
    ok, desc = check_condition({"hp%": ">50"}, writer)
    assert ok is True


def test_check_condition_invalid_expr():
    writer, _ = mock_writer_with_vitals(50, 100, 50, 100)
    ok, desc = check_condition({"hp%": "bad"}, writer)
    assert ok is True


@pytest.mark.parametrize(
    "when, captures, ok",
    [
        ({"Adrenaline": ">50"}, {"Adrenaline": 100}, True),
        ({"Adrenaline": ">50"}, {"Adrenaline": 30}, False),
        ({"Adrenaline%": ">50"}, {"Adrenaline": 80, "MaxAdrenaline": 100}, True),
        ({"Adrenaline%": ">50"}, {"Adrenaline": 30, "MaxAdrenaline": 100}, False),
        ({"Unknown": ">50"}, {}, True),
        ({"hp%": ">50"}, {}, True),
    ],
)
def test_check_condition_captures(when, captures, ok):
    ctx = types.SimpleNamespace(gmcp_data={}, captures=captures)
    result, desc = check_condition(when, ctx)
    assert result is ok


def test_check_condition_captures_gmcp_priority():
    ctx = types.SimpleNamespace(gmcp_data={"Char.Vitals": {"hp": "80", "maxhp": "100"}}, captures={"hp": 20})
    ok, desc = check_condition({"hp": ">50"}, ctx)
    assert ok is True


@pytest.mark.parametrize(
    "when, gmcp_data, ok",
    [
        ({"Water": ">200"}, {"Char.Vitals": {"Water": 300, "MaxWater": 500}}, True),
        ({"Water": ">200"}, {"Char.Vitals": {"Water": 100, "MaxWater": 500}}, False),
        ({"Water": ">200"}, {"Room.Info": {"Water": 300}}, True),
        ({"Water%": ">50"}, {"Char.Vitals": {"Water": 400, "MaxWater": 500}}, True),
        ({"Water%": ">50"}, {"Char.Vitals": {"Water": 100, "MaxWater": 500}}, False),
    ],
)
def test_check_condition_gmcp_arbitrary_key(when, gmcp_data, ok):
    ctx = types.SimpleNamespace(gmcp_data=gmcp_data, captures={})
    result, desc = check_condition(when, ctx)
    assert result is ok


@pytest.mark.parametrize(
    "when, gmcp_data, ok",
    [
        ({"Char.Guild.Stats.Water": ">50"}, {"Char.Guild.Stats": {"Water": 80, "MaxWater": 200}}, True),
        ({"Char.Guild.Stats.Water": ">100"}, {"Char.Guild.Stats": {"Water": 80, "MaxWater": 200}}, False),
        ({"Char.Guild.Stats.Water%": ">30"}, {"Char.Guild.Stats": {"Water": 80, "MaxWater": 200}}, True),
        ({"Char.Guild.Stats.Water%": ">50"}, {"Char.Guild.Stats": {"Water": 80, "MaxWater": 200}}, False),
    ],
)
def test_check_condition_dotted_path(when, gmcp_data, ok):
    ctx = types.SimpleNamespace(gmcp_data=gmcp_data, captures={})
    result, _ = check_condition(when, ctx)
    assert result is ok


@pytest.mark.parametrize(
    "when, gmcp_data, ok",
    [
        ({"Mode": "=Rage"}, {"Char.Guild.Stats": {"Mode": "Rage"}}, True),
        ({"Mode": "=Calm"}, {"Char.Guild.Stats": {"Mode": "Rage"}}, False),
        ({"Mode": "!=Rage"}, {"Char.Guild.Stats": {"Mode": "Rage"}}, False),
        ({"Mode": "!=Calm"}, {"Char.Guild.Stats": {"Mode": "Rage"}}, True),
        ({"Char.Guild.Stats.Mode": "!=Rage"}, {"Char.Guild.Stats": {"Mode": "Rage"}}, False),
        ({"Char.Guild.Stats.Mode": "=Rage"}, {"Char.Guild.Stats": {"Mode": "Rage"}}, True),
    ],
)
def test_check_condition_string_compare(when, gmcp_data, ok):
    ctx = types.SimpleNamespace(gmcp_data=gmcp_data, captures={})
    result, _ = check_condition(when, ctx)
    assert result is ok


@pytest.mark.parametrize(
    "kwargs, attr, expected",
    [
        (dict(pattern=re.compile(r"bear"), reply="kill bear;", when={"hp%": ">50", "mp%": ">30"}),
         "when", {"hp%": ">50", "mp%": ">30"}),
        (dict(pattern=re.compile(r"ship arrived"), reply="enter ship;", immediate=True),
         "immediate", True),
        (dict(pattern=re.compile(r"DEAD", re.MULTILINE | re.DOTALL), reply="loot;", case_sensitive=True),
         "case_sensitive", True),
    ],
)
def test_save_triggers_roundtrip(tmp_path, kwargs, attr, expected):
    fp = tmp_path / "ar.json"
    save_triggers(str(fp), [TriggerRule(**kwargs)], "test:23")
    loaded = load_triggers(str(fp), "test:23")
    assert getattr(loaded[0], attr) == expected
    if attr == "case_sensitive" and expected is True:
        assert not (loaded[0].pattern.flags & re.IGNORECASE)


@pytest.mark.parametrize("key", ["when", "immediate", "case_sensitive"])
def test_save_triggers_default_not_saved(tmp_path, key):
    fp = tmp_path / "ar.json"
    save_triggers(str(fp), [TriggerRule(pattern=re.compile(r"x"), reply="y;")], "test:23")
    raw = json.loads(fp.read_text())
    assert key not in raw["test:23"]["triggers"][0]


def test_load_triggers_case_insensitive_by_default(tmp_path):
    fp = tmp_path / "ar.json"
    fp.write_text(json.dumps({"test:23": {"triggers": [{"pattern": "hello", "reply": "world;"}]}}))
    loaded = load_triggers(str(fp), "test:23")
    assert loaded[0].case_sensitive is False
    assert loaded[0].pattern.flags & re.IGNORECASE


def test_load_triggers_case_sensitive_flag(tmp_path):
    fp = tmp_path / "ar.json"
    fp.write_text(
        json.dumps({"test:23": {"triggers": [{"pattern": "DEAD", "reply": "loot;", "case_sensitive": True}]}})
    )
    loaded = load_triggers(str(fp), "test:23")
    assert loaded[0].case_sensitive is True
    assert not (loaded[0].pattern.flags & re.IGNORECASE)
    assert loaded[0].pattern.search("DEAD") is not None
    assert loaded[0].pattern.search("dead") is None


@pytest.mark.asyncio
async def test_engine_skips_rule_on_condition_fail():
    writer, written = mock_writer_with_vitals(30, 100, 50, 100)
    rules = [TriggerRule(pattern=re.compile(r"bear"), reply="kill bear;", when={"hp%": ">50"})]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("A bear appears.\n")
    await asyncio.sleep(0.02)
    assert not any("kill bear" in w for w in written)
    failed = engine.pop_condition_failed()
    assert failed is not None
    assert failed[0] == 1
    assert "hp%" in failed[1]


@pytest.mark.asyncio
async def test_engine_fires_rule_when_condition_passes():
    writer, written = mock_writer_with_vitals(80, 100, 50, 100)
    rules = [TriggerRule(pattern=re.compile(r"bear"), reply="kill bear;", when={"hp%": ">50"})]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("A bear appears.\n")
    await asyncio.sleep(0.02)
    assert any("kill bear" in w for w in written)
    assert engine.pop_condition_failed() is None


@pytest.mark.asyncio
async def test_condition_failed_clears_on_read():
    writer, _ = mock_writer_with_vitals(30, 100, 50, 100)
    rules = [TriggerRule(pattern=re.compile(r"bear"), reply="kill bear;", when={"hp%": ">50"})]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.feed("A bear appears.\n")
    await asyncio.sleep(0.02)
    assert engine.pop_condition_failed() is not None
    assert engine.pop_condition_failed() is None


@pytest.mark.asyncio
async def test_condition_blocked_preserves_buffer_for_retry():
    """Buffer is retained when condition fails so rule can fire after HP heals."""
    writer, written = mock_writer_with_vitals(30, 100, 50, 100)
    rules = [TriggerRule(pattern=re.compile(r"bear"), reply="kill bear;", when={"hp%": ">50"})]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()

    engine.feed("A bear appears.\n")
    engine.on_prompt()
    await asyncio.sleep(0.02)
    assert not any("kill bear" in w for w in written)
    assert engine.buffer.lines

    writer.gmcp_data["Char.Vitals"]["hp"] = "80"
    engine.on_prompt()
    await asyncio.sleep(0.02)
    assert any("kill bear" in w for w in written)


@pytest.mark.asyncio
async def test_condition_blocked_clears_buffer_on_repeated_failure():
    """Buffer is cleared when the same condition fails twice to prevent loops."""
    writer, written = mock_writer_with_vitals(30, 100, 50, 100)
    rules = [
        TriggerRule(pattern=re.compile(r"bear"), reply="kill bear;", when={"hp%": ">50"}),
        TriggerRule(pattern=re.compile(r"corpse"), reply="loot corpse;"),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()

    engine.feed("A bear appears.\ncorpse of rat\n")
    engine.on_prompt()
    await asyncio.sleep(0.02)
    assert not any("kill bear" in w for w in written)
    loot_count_1 = sum(1 for w in written if "loot corpse" in w)
    assert loot_count_1 == 1

    engine.feed("more server text\n")
    engine.on_prompt()
    await asyncio.sleep(0.02)
    loot_count_2 = sum(1 for w in written if "loot corpse" in w)
    assert loot_count_2 == 1


@pytest.mark.asyncio
async def test_immediate_rule_fires_in_prompt_based_mode():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"normal trigger"), reply="normal;"),
        TriggerRule(pattern=re.compile(r"ship arrived"), reply="enter ship;", immediate=True),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()
    assert engine.prompt_based is True

    engine.feed("ship arrived\n")
    await asyncio.sleep(0.02)
    assert any("enter ship" in w for w in written)


@pytest.mark.asyncio
async def test_non_immediate_rule_deferred_in_prompt_based_mode():
    writer, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"normal trigger"), reply="normal;")]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()
    assert engine.prompt_based is True

    engine.feed("normal trigger\n")
    await asyncio.sleep(0.02)
    assert not any("normal" in w for w in written)

    engine.on_prompt()
    await asyncio.sleep(0.02)
    assert any("normal" in w for w in written)


@pytest.mark.asyncio
async def test_immediate_and_normal_rules_together():
    writer, written = mock_writer()
    rules = [
        TriggerRule(pattern=re.compile(r"normal"), reply="deferred;"),
        TriggerRule(pattern=re.compile(r"urgent"), reply="now;", immediate=True),
    ]
    engine = TriggerEngine(rules, writer, writer.log)
    engine.on_prompt()

    engine.feed("urgent event\nnormal event\n")
    await asyncio.sleep(0.02)
    assert any("now" in w for w in written)
    assert not any("deferred" in w for w in written)

    engine.on_prompt()
    await asyncio.sleep(0.02)
    assert any("deferred" in w for w in written)


def test_exclusive_state_clear():
    state = ExclusiveState()
    state.active = True
    state.rule_index = 3
    state.clear()
    assert state.active is False
    assert state.rule_index == 0


def test_trigger_last_fired_round_trip(tmp_path):
    path = str(tmp_path / "triggers.json")
    rules = [
        TriggerRule(pattern=re.compile("hello"), reply="world", last_fired="2025-06-01T12:00:00+00:00"),
        TriggerRule(pattern=re.compile("foo"), reply="bar"),
    ]
    save_triggers(path, rules, "localhost:23")
    loaded = load_triggers(path, "localhost:23")
    assert loaded[0].last_fired == "2025-06-01T12:00:00+00:00"
    assert loaded[1].last_fired == ""


@pytest.mark.asyncio
async def test_trigger_engine_stamps_last_fired():
    ctx = types.SimpleNamespace(
        writer=types.SimpleNamespace(write=lambda s: None),
        gmcp_data={},
        walk=types.SimpleNamespace(active_command=None, active_command_time=0.0, randomwalk_active=False),
    )
    rule = TriggerRule(pattern=re.compile("hello"), reply="world;")
    engine = TriggerEngine(rules=[rule], ctx=ctx, log=logging.getLogger("test"))
    engine.feed("hello\n")
    assert rule.last_fired != ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hp, mp, when_expr, should_fire",
    [
        (80, 50, "hp%>50", True),
        (30, 50, "hp%>50", False),
        (80, 50, "hp>50", True),
        (30, 50, "hp>50", False),
        (80, 50, "mp>30", True),
    ],
)
async def test_inline_when(hp, mp, when_expr, should_fire):
    ctx, written = mock_writer_with_vitals(hp, 100, mp, 100)
    rules = [TriggerRule(pattern=re.compile(r"bear"), reply=f"`when {when_expr}`;kill bear;")]
    engine = TriggerEngine(rules, ctx, ctx.log)
    engine.feed("A bear appears.\n")
    await asyncio.sleep(0.02)
    assert any("kill bear" in w for w in written) == should_fire


@pytest.mark.asyncio
async def test_inline_until_waits_for_match():
    ctx, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"bear"), reply="kill bear;`until 2 died\\.`;glance;")]
    engine = TriggerEngine(rules, ctx, ctx.log)
    engine.feed("A bear appears.\n")
    await asyncio.sleep(0.05)
    assert any("kill bear" in w for w in written)
    assert not any("glance" in w for w in written)

    engine.buffer.add_text("The bear died.\n")
    await asyncio.sleep(0.02)
    assert any("glance" in w for w in written)


@pytest.mark.asyncio
async def test_inline_until_matches_partial_line():
    ctx, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"bear"), reply="kill bear;`until 2 Password:`;secret;")]
    engine = TriggerEngine(rules, ctx, ctx.log)
    engine.feed("A bear appears.\n")
    await asyncio.sleep(0.05)
    assert any("kill bear" in w for w in written)
    assert not any("secret" in w for w in written)

    engine.buffer.add_text("Password: ")
    await asyncio.sleep(0.02)
    assert any("secret" in w for w in written)


@pytest.mark.asyncio
async def test_inline_until_timeout_aborts():
    ctx, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"bear"), reply="kill bear;`until 0.02 died\\.`;glance;")]
    engine = TriggerEngine(rules, ctx, ctx.log)
    engine.feed("A bear appears.\n")
    await asyncio.sleep(0.05)
    assert any("kill bear" in w for w in written)
    assert not any("glance" in w for w in written)


@pytest.mark.asyncio
async def test_inline_untils_case_sensitive():
    ctx, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"mob"), reply="attack;`untils 0.02 DEAD`;loot;")]
    engine = TriggerEngine(rules, ctx, ctx.log)
    engine.feed("A mob appears.\n")
    await asyncio.sleep(0.01)
    assert any("attack" in w for w in written)

    engine.buffer.add_text("the mob is dead\n")
    await asyncio.sleep(0.05)
    assert not any("loot" in w for w in written)


@pytest.mark.asyncio
async def test_inline_untils_case_sensitive_matches():
    ctx, written = mock_writer()
    rules = [TriggerRule(pattern=re.compile(r"mob"), reply="attack;`untils 2 DEAD`;loot;")]
    engine = TriggerEngine(rules, ctx, ctx.log)
    engine.feed("A mob appears.\n")
    await asyncio.sleep(0.05)

    engine.buffer.add_text("the mob is DEAD\n")
    await asyncio.sleep(0.02)
    assert any("loot" in w for w in written)


@pytest.mark.asyncio
async def test_pipe_immediate_send_skips_wait_fn():
    ctx, written = mock_writer()
    wait_calls: list[float] = []

    async def fake_wait() -> None:
        wait_calls.append(asyncio.get_event_loop().time())

    rules = [TriggerRule(pattern=re.compile(r"go"), reply="cmd1|cmd2;cmd3;")]
    engine = TriggerEngine(rules, ctx, ctx.log, wait_fn=fake_wait)
    engine.feed("go\n")
    await asyncio.sleep(0.02)
    assert any("cmd1" in w for w in written)
    assert any("cmd2" in w for w in written)
    assert any("cmd3" in w for w in written)
    assert len(wait_calls) == 1


def make_status_ctx(with_commands=False, **writer_kwargs):
    """Build a minimal ctx namespace for status/progress tests."""
    writer_kwargs.setdefault("write", lambda s: None)
    writer = types.SimpleNamespace(**writer_kwargs)
    ctx = types.SimpleNamespace(
        writer=writer,
        gmcp_data={},
        walk=types.SimpleNamespace(active_command=None, active_command_time=0.0, randomwalk_active=False),
    )
    if with_commands:
        ctx.commands = session_context.CommandState()
    return ctx


def test_status_text_initially_empty():
    ctx = make_status_ctx()
    engine = TriggerEngine(rules=[], ctx=ctx, log=logging.getLogger("test"))
    assert engine.status_text == ""


@pytest.mark.asyncio
async def test_status_text_during_until():
    ctx = make_status_ctx(with_commands=True)
    rules = [TriggerRule(pattern=re.compile(r"go"), reply="cmd1;`until 0.1 done`")]
    engine = TriggerEngine(rules, ctx, logging.getLogger("test"))
    engine.feed("go\n")
    await asyncio.sleep(0.02)
    assert "until" in engine.status_text
    assert "done" in engine.status_text
    engine.buffer.add_text("done\n")
    await asyncio.sleep(0.02)
    assert engine.status_text == ""


@pytest.mark.asyncio
async def test_status_text_during_delay():
    ctx = make_status_ctx(with_commands=True)
    rules = [TriggerRule(pattern=re.compile(r"go"), reply="`delay 50ms`;cmd1;")]
    engine = TriggerEngine(rules, ctx, logging.getLogger("test"))
    engine.feed("go\n")
    await asyncio.sleep(0.02)
    assert "delay" in engine.status_text
    assert engine.until_progress is not None
    assert 0.0 <= engine.until_progress <= 1.0
    engine.cancel()
    assert engine.status_text == ""


@pytest.mark.asyncio
async def test_status_text_cleared_on_cancel():
    ctx = make_status_ctx(with_commands=True)
    rules = [TriggerRule(pattern=re.compile(r"go"), reply="`until 0.1 nope`")]
    engine = TriggerEngine(rules, ctx, logging.getLogger("test"))
    engine.feed("go\n")
    await asyncio.sleep(0.05)
    assert engine.status_text != ""
    engine.cancel()
    assert engine.status_text == ""


@pytest.mark.asyncio
async def test_until_progress_tracks_elapsed():
    ctx = make_status_ctx(with_commands=True)
    rules = [TriggerRule(pattern=re.compile(r"go"), reply="cmd1;`until 0.2 done`")]
    engine = TriggerEngine(rules, ctx, logging.getLogger("test"))
    assert engine.until_progress is None

    engine.feed("go\n")
    await asyncio.sleep(0.02)
    prog = engine.until_progress
    assert prog is not None
    assert 0.0 <= prog <= 0.5

    engine.buffer.add_text("done\n")
    await asyncio.sleep(0.02)
    assert engine.until_progress is None


@pytest.mark.asyncio
async def test_until_progress_cleared_on_timeout():
    ctx = make_status_ctx(with_commands=True)
    rules = [TriggerRule(pattern=re.compile(r"go"), reply="cmd1;`until 0.02 nomatch`")]
    engine = TriggerEngine(rules, ctx, logging.getLogger("test"))
    engine.feed("go\n")
    await asyncio.sleep(0.01)
    assert engine.until_progress is not None
    await asyncio.sleep(0.05)
    assert engine.until_progress is None


@pytest.mark.asyncio
async def test_status_text_masks_send_when_will_echo():
    """status_text shows '(masked)' instead of the command when will_echo is True."""
    sent: list[str] = []
    ctx = make_status_ctx(write=sent.append, will_echo=True)
    rules = [TriggerRule(pattern=re.compile(r"Password:"), reply="`delay 20ms`;secret")]
    engine = TriggerEngine(rules, ctx, logging.getLogger("test"))
    engine.feed("Password:\n")
    await asyncio.sleep(0.05)
    assert "secret" not in engine.status_text
    engine.cancel()
