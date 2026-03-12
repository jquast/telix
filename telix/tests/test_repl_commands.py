"""Tests for :mod:`telix.client_repl_commands` regex constants and helpers."""

from __future__ import annotations

import pytest

from telix import client_repl_commands


@pytest.mark.parametrize(
    "text,expected_groups",
    [
        ("`delay 1s`", ("1", "s")),
        ("`delay 500ms`", ("500", "ms")),
        ("`delay 1.5s`", ("1.5", "s")),
    ],
)
def test_delay_re_matches(text, expected_groups):
    """DELAY_RE matches backtick-enclosed delay commands."""
    m = client_repl_commands.DELAY_RE.match(text)
    assert m
    assert m.groups() == expected_groups


def test_delay_re_no_backticks():
    """DELAY_RE does not match without backticks."""
    assert client_repl_commands.DELAY_RE.match("delay 1s") is None


@pytest.mark.parametrize(
    "text,expected_groups",
    [
        ("`when hp%>50`", ("hp%", ">", "50")),
        ("`when mp!=0`", ("mp", "!=", "0")),
        ("`when Char.Vitals.hp>=100`", ("Char.Vitals.hp", ">=", "100")),
    ],
)
def test_when_re_matches(text, expected_groups):
    """WHEN_RE matches backtick-enclosed when conditions."""
    m = client_repl_commands.WHEN_RE.match(text)
    assert m
    assert m.groups() == expected_groups


@pytest.mark.parametrize(
    "text,expected_groups",
    [
        ("`until 4 died\\.`", ("4", "died\\.")),
        ("`until pattern`", (None, "pattern")),
    ],
)
def test_until_re_matches(text, expected_groups):
    """UNTIL_RE matches backtick-enclosed until commands."""
    m = client_repl_commands.UNTIL_RE.match(text)
    assert m
    assert m.groups() == expected_groups


@pytest.mark.parametrize(
    "text,expected_groups",
    [
        ("`untils 4 died\\.`", ("4", "died\\.")),
        ("`untils pattern`", (None, "pattern")),
    ],
)
def test_untils_re_matches(text, expected_groups):
    """UNTILS_RE matches backtick-enclosed untils commands."""
    m = client_repl_commands.UNTILS_RE.match(text)
    assert m
    assert m.groups() == expected_groups


def test_esc_restore_is_inverse_of_esc_map():
    """ESC_RESTORE is the exact inverse of ESC_MAP."""
    for k, v in client_repl_commands.ESC_MAP.items():
        assert client_repl_commands.ESC_RESTORE[v] == k


def test_esc_map_has_all_four_keys():
    """ESC_MAP contains entries for ; | ` and backslash."""
    assert set(client_repl_commands.ESC_MAP.keys()) == {";", "|", "`", "\\"}


def test_step_result_enum_values():
    """StepResult has SENT, HANDLED, and ABORT members."""
    assert client_repl_commands.StepResult.SENT.value == "sent"
    assert client_repl_commands.StepResult.HANDLED.value == "handled"
    assert client_repl_commands.StepResult.ABORT.value == "abort"


def test_expand_commands_ex_repeat():
    """Repeat prefix expands into multiple commands."""
    result = client_repl_commands.expand_commands_ex("5e")
    assert result.commands == ["e"] * 5


def test_expand_commands_ex_semicolon_split():
    """Semicolons split into separate commands."""
    result = client_repl_commands.expand_commands_ex("north;south")
    assert result.commands == ["north", "south"]


def test_expand_commands_ex_pipe_separator():
    """Pipe separator produces commands with index 1 in immediate_set."""
    result = client_repl_commands.expand_commands_ex("a|b")
    assert result.commands == ["a", "b"]
    assert 1 in result.immediate_set


def test_expand_commands_ex_backtick_preserved():
    """Backtick-enclosed commands are not split on semicolons."""
    result = client_repl_commands.expand_commands_ex("`delay 1s`;go")
    assert result.commands == ["`delay 1s`", "go"]


def test_expand_commands_ex_escaped_separator():
    r"""Backslash-escaped semicolons produce a literal semicolon."""
    result = client_repl_commands.expand_commands_ex("say hello\\;world")
    assert result.commands == ["say hello;world"]


def test_collapse_runs_basic():
    """collapse_runs groups consecutive identical commands."""
    runs = client_repl_commands.collapse_runs(["e", "e", "e", "n", "n"])
    assert len(runs) == 2
    assert runs[0] == ("3\u00d7e", 0, 2)
    assert runs[1] == ("2\u00d7n", 3, 4)


def test_collapse_runs_single_no_prefix():
    """Single commands do not get a repeat prefix."""
    runs = client_repl_commands.collapse_runs(["north", "south"])
    assert runs[0] == ("north", 0, 0)
    assert runs[1] == ("south", 1, 1)


def test_collapse_runs_start_parameter():
    """The start parameter skips earlier entries."""
    runs = client_repl_commands.collapse_runs(["e", "e", "e", "n", "n"], start=3)
    assert len(runs) == 1
    assert runs[0] == ("2\u00d7n", 3, 4)
