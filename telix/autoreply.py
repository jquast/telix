"""
Server output pattern matching and automatic reply engine.

Provides :class:`SearchBuffer` for accumulating ANSI-stripped server output
and :class:`AutoreplyEngine` for matching regex patterns and queuing replies
with delay/chaining support.
"""

from __future__ import annotations

# std imports
import os
import re
import json
import time
import asyncio
import logging
from time import monotonic as monotonic
from typing import TYPE_CHECKING, Any, Callable, Optional, Awaitable
from datetime import datetime, timezone
from dataclasses import field, dataclass

# 3rd party
from wcwidth import strip_sequences

# local
from .client_repl_render import scramble_password

if TYPE_CHECKING:
    from .session_context import SessionContext

__all__ = (
    "AutoreplyRule",
    "SearchBuffer",
    "AutoreplyEngine",
    "load_autoreplies",
    "save_autoreplies",
    "check_condition",
)

GROUP_RE = re.compile(r"\\(\d+)")
COND_RE = re.compile(r"^(>=|<=|>|<|=)(\d+)$")
KILL_RE = re.compile(r"^kill\s+(\S+)", re.IGNORECASE)

# Maps condition key to (current_keys, max_keys) for GMCP Char.Vitals lookup.
VITAL_PCT_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "HP%": (("hp", "HP"), ("maxhp", "maxHP", "max_hp")),
    "MP%": (("mp", "MP", "mana", "sp", "SP"), ("maxmp", "maxMP", "max_mp", "maxsp", "maxSP")),
}

# Maps raw-value condition key to GMCP Char.Vitals field names.
VITAL_RAW_KEYS: dict[str, tuple[str, ...]] = {
    "HP": ("hp", "HP"),
    "MP": ("mp", "MP", "mana", "sp", "SP"),
}


def get_vital_raw(key: str, vitals: dict[str, Any]) -> Optional[int]:
    """Return the raw vital value for *key*, or ``None`` if unavailable."""
    field_names = VITAL_RAW_KEYS.get(key)
    if field_names is None:
        return None
    for k in field_names:
        raw = vitals.get(k)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


def get_vital_pct(key: str, vitals: dict[str, Any]) -> Optional[int]:
    """Return the vital percentage (0-100+) for *key*, or ``None`` if unavailable."""
    spec = VITAL_PCT_KEYS.get(key)
    if spec is None:
        return None
    cur_keys, max_keys = spec
    cur_raw = None
    for k in cur_keys:
        cur_raw = vitals.get(k)
        if cur_raw is not None:
            break
    max_raw = None
    for k in max_keys:
        max_raw = vitals.get(k)
        if max_raw is not None:
            break
    if cur_raw is None or max_raw is None:
        return None
    try:
        cur = int(cur_raw)
        mx = int(max_raw)
    except (TypeError, ValueError):
        return None
    if mx <= 0:
        return None
    return int(cur * 100 / mx)


def gmcp_lookup_raw(key: str, gmcp: dict[str, Any]) -> Optional[int]:
    """Look up *key* directly in any GMCP package dict."""
    for pkg_data in gmcp.values():
        if not isinstance(pkg_data, dict):
            continue
        raw = pkg_data.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return None


def gmcp_lookup_pct(key: str, gmcp: dict[str, Any]) -> Optional[int]:
    """Look up *key* (without trailing ``%``) as a value/max pair in any GMCP package."""
    base = key[:-1] if key.endswith("%") else key
    for pkg_data in gmcp.values():
        if not isinstance(pkg_data, dict):
            continue
        cur_raw = pkg_data.get(base)
        if cur_raw is None:
            continue
        # Try MaxBase, baseMax, maxbase (case-insensitive scan).
        max_raw = pkg_data.get(f"Max{base}") or pkg_data.get(f"max{base}")
        if max_raw is None:
            lower_target = f"{base.lower()}max"
            for k, v in pkg_data.items():
                if k.lower() == f"max{base.lower()}" or k.lower() == lower_target:
                    max_raw = v
                    break
        if max_raw is None:
            continue
        try:
            cur = int(cur_raw)
            mx = int(max_raw)
        except (TypeError, ValueError):
            continue
        if mx <= 0:
            continue
        return int(cur * 100 / mx)
    return None


def compare(value: int, op: str, threshold: int) -> bool:
    """Evaluate ``value op threshold``."""
    if op == ">":
        return value > threshold
    if op == "<":
        return value < threshold
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == "=":
        return value == threshold
    raise ValueError(f"unknown operator: {op!r}")


def check_condition(when: dict[str, str], ctx: "SessionContext") -> tuple[bool, str]:
    """
    Check vital conditions against GMCP data and captured variables on *ctx*.

    :param when: Condition dict, e.g. ``{"HP%": ">50"}`` (percentage),
        ``{"HP": ">500"}`` (raw value), or ``{"Adrenaline": ">100"}``
        (captured variable).
    :param ctx: Session context with ``gmcp_data`` and ``captures`` attributes.
    :returns: ``(ok, failure_description)`` -- *ok* is ``False`` when a
        condition is not met; *failure_description* explains which.
    """
    if not when:
        return True, ""
    gmcp: Optional[dict[str, Any]] = ctx.gmcp_data if ctx is not None else None
    vitals: Optional[dict[str, Any]] = None
    if gmcp:
        v = gmcp.get("Char.Vitals")
        vitals = v if isinstance(v, dict) else None
    captures: dict[str, int] = getattr(ctx, "captures", {}) if ctx is not None else {}
    for key, expr in when.items():
        m = COND_RE.match(expr.strip())
        if not m:
            continue
        op, threshold = m.group(1), int(m.group(2))
        value: Optional[int] = None
        unit = ""
        if key.endswith("%"):
            if vitals is not None:
                value = get_vital_pct(key, vitals)
            if value is None and gmcp:
                value = gmcp_lookup_pct(key, gmcp)
            if value is None and captures:
                base = key[:-1]
                cur = captures.get(base)
                mx = captures.get(f"Max{base}")
                if cur is not None and mx is not None and mx > 0:
                    value = int(cur * 100 / mx)
            unit = "%"
        else:
            if vitals is not None:
                value = get_vital_raw(key, vitals)
            if value is None and gmcp:
                value = gmcp_lookup_raw(key, gmcp)
            if value is None and captures:
                value = captures.get(key)
        if value is None:
            continue
        if not compare(value, op, threshold):
            return False, f"{key}{op}{threshold} (actual {value}{unit})"
    return True, ""


@dataclass
class AutoreplyRule:
    r"""
    A single autoreply pattern-action rule.

    All rules are exclusive by default -- when a rule fires, no other
    non-``always`` rules fire until the reply chain completes.

    :param pattern: Compiled regex pattern.
    :param reply: Reply template with ``\1``/``\2`` group refs,
        ``;``/``|`` command separators, repeat prefixes (``3e``),
        delay segments, ``\`when HP%>=N\```, and ``\`until [T] pat\```.
    :param always: Match even while another rule's reply chain is active.
    :param when: Vital conditions that must be met for the rule to fire,
        e.g. ``{"HP%": ">50", "MP%": ">30"}``.
    :param immediate: Fire without waiting for prompt/GA/EOR.
    :param case_sensitive: Match the pattern case-sensitively.
    """

    pattern: re.Pattern[str]
    reply: str
    always: bool = False
    enabled: bool = True
    when: dict[str, str] = field(default_factory=dict)
    immediate: bool = False
    last_fired: str = ""
    case_sensitive: bool = False


def parse_entries(entries: list[dict[str, str]]) -> list[AutoreplyRule]:
    """Parse a list of autoreply entry dicts into :class:`AutoreplyRule` instances."""
    rules: list[AutoreplyRule] = []
    for entry in entries:
        pattern_str = entry.get("pattern", "")
        reply = entry.get("reply", "")
        if not pattern_str:
            continue
        always = bool(entry.get("always", False))
        enabled = bool(entry.get("enabled", True))
        when_raw: Any = entry.get("when", {})
        when = dict(when_raw) if isinstance(when_raw, dict) else {}
        immediate = bool(entry.get("immediate", False))
        last_fired = str(entry.get("last_fired", ""))
        case_sensitive = bool(entry.get("case_sensitive", False))
        flags = re.MULTILINE | re.DOTALL
        if not case_sensitive:
            flags |= re.IGNORECASE
        try:
            compiled = re.compile(pattern_str, flags)
        except re.error as exc:
            raise ValueError(f"Invalid autoreply pattern {pattern_str!r}: {exc}") from exc
        rules.append(
            AutoreplyRule(
                pattern=compiled,
                reply=reply,
                always=always,
                enabled=enabled,
                when=when,
                immediate=immediate,
                last_fired=last_fired,
                case_sensitive=case_sensitive,
            )
        )
    return rules


def load_autoreplies(path: str, session_key: str) -> list[AutoreplyRule]:
    """
    Load autoreply rules for a session from a JSON file.

    The file is keyed by session (``"host:port"``).  Each value is
    an object with an ``"autoreplies"`` list.

    :param path: Path to the autoreplies JSON file.
    :param session_key: Session identifier (``"host:port"``).
    :returns: List of :class:`AutoreplyRule` instances.
    :raises FileNotFoundError: When *path* does not exist.
    :raises ValueError: When JSON structure is invalid or regex fails.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)

    session_data: dict[str, Any] = data.get(session_key, {})
    entries: list[dict[str, str]] = session_data.get("autoreplies", [])
    return parse_entries(entries)


def save_autoreplies(path: str, rules: list[AutoreplyRule], session_key: str) -> None:
    """
    Save autoreply rules for a session to a JSON file.

    Other sessions' data in the file is preserved.

    :param path: Path to the autoreplies JSON file.
    :param rules: List of :class:`AutoreplyRule` instances to save.
    :param session_key: Session identifier (``"host:port"``).
    """
    data: dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

    data[session_key] = {
        "autoreplies": [
            {
                "pattern": r.pattern.pattern,
                "reply": r.reply,
                **({"always": True} if r.always else {}),
                **({"enabled": False} if not r.enabled else {}),
                **({"when": dict(r.when)} if r.when else {}),
                **({"immediate": True} if r.immediate else {}),
                **({"case_sensitive": True} if r.case_sensitive else {}),
                **({"last_fired": r.last_fired} if r.last_fired else {}),
            }
            for r in rules
        ]
    }
    from .paths import atomic_write

    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write(path, content)


def extract_group_source(pattern_src: str, group_num: int) -> Optional[str]:
    r"""
    Extract the source text of capture group *group_num* from *pattern_src*.

    Walks the pattern string counting only capturing groups (skipping
    ``(?:...)``, ``(?=...)``, ``(?!...)``, ``(?P<...>...)`` named groups
    count as capturing).  Returns the substring between the group's
    parentheses, or ``None`` if *group_num* is out of range.

    :param pattern_src: Raw regex source string.
    :param group_num: 1-based group number.
    :returns: Group source text or ``None``.
    """
    cap_count = 0
    i = 0
    n = len(pattern_src)
    while i < n:
        ch = pattern_src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            if i + 1 < n and pattern_src[i + 1] == "?":
                # (?:...) (?=...) (?!...) (?<=...) (?<!...) are non-capturing
                # (?P<name>...) is capturing
                if (
                    i + 2 < n
                    and pattern_src[i + 2] == "P"
                    and i + 3 < n
                    and pattern_src[i + 3] == "<"
                ):
                    cap_count += 1
                # else non-capturing, don't increment
            else:
                cap_count += 1
            if cap_count == group_num:
                # Find matching close paren
                start = i + 1
                # Skip past the '(?P<name>' prefix if present
                if i + 3 < n and pattern_src[i + 1 : i + 4] == "?P<":
                    close_angle = pattern_src.index(">", i + 4)
                    start = close_angle + 1
                inner_depth = 1
                j = start
                while j < n and inner_depth > 0:
                    c = pattern_src[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "(":
                        inner_depth += 1
                    elif c == ")":
                        inner_depth -= 1
                    if inner_depth > 0:
                        j += 1
                if inner_depth == 0:
                    return pattern_src[start:j]
                return None
        i += 1
    return None


def resolve_group_value(captured: str, pattern_src: str, group_num: int, flags: int) -> str:
    r"""
    Resolve the substitution value for a captured group.

    When the pattern uses ``re.IGNORECASE`` and the group is a pure
    alternation of literals (e.g. ``amplifier|enhancer|shield``), returns
    the pattern literal rather than the input text.  This ensures that
    ``\1`` in a reply template reflects the pattern author's intended
    casing, not whatever casing the server happened to use.

    Falls back to *captured* when the group contains regex metacharacters
    or when no literal alternative matches.

    :param captured: Text captured by the group from input.
    :param pattern_src: Raw regex source string.
    :param group_num: 1-based group number.
    :param flags: Compiled pattern flags.
    :returns: Resolved substitution value.
    """
    if not flags & re.IGNORECASE:
        return captured
    group_src = extract_group_source(pattern_src, group_num)
    if group_src is None:
        return captured
    # Split on top-level '|' only (not inside nested groups)
    alternatives: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(group_src):
        if ch == "\\" and i + 1 < len(group_src):
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            alternatives.append(group_src[start:i])
            start = i + 1
    alternatives.append(group_src[start:])
    # Check each alternative is a pure literal (no regex metacharacters)
    META = set(r"\.^$*+?{}[]|()")
    lowered = captured.lower()
    for alt in alternatives:
        if any(c in META and (j == 0 or alt[j - 1] != "\\") for j, c in enumerate(alt)):
            return captured
        if alt.lower() == lowered:
            return alt
    return captured


def substitute_groups(template: str, match: re.Match[str]) -> str:
    r"""
    Replace ``\1``, ``\2``, etc. with match group values.

    When the pattern is case-insensitive and a group is a pure alternation of literals, the
    pattern's literal is used instead of the input text.

    :param template: Reply template string.
    :param match: Regex match object.
    :returns: Template with groups substituted.
    """
    pat = match.re

    def repl(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        try:
            val = match.group(idx)
        except IndexError:
            return m.group(0)
        if val is None:
            return ""
        return resolve_group_value(val, pat.pattern, idx, pat.flags)

    return GROUP_RE.sub(repl, template)


class SearchBuffer:
    """
    Accumulates stripped server output lines for regex matching.

    Maintains a rolling window of recent lines with ANSI sequences stripped.  Tracks the last match
    position so each new line is only searched from the position after the previous match.

    :param max_lines: Maximum number of lines to retain (default 100).
    """

    def __init__(self, max_lines: int = 100) -> None:
        """Initialize SearchBuffer with given line capacity."""
        self._lines: list[str] = []
        self._partial: str = ""
        self.max_lines = max_lines
        self.last_match_line: int = 0
        self.last_match_col: int = 0
        self.new_text: Optional[asyncio.Event] = None

    @property
    def lines(self) -> list[str]:
        """Complete lines accumulated so far."""
        return self._lines

    @property
    def partial(self) -> str:
        """Incomplete trailing line (no newline yet)."""
        return self._partial

    def add_text(self, text: str, echo_filter: Optional["set[str]"] = None) -> bool:
        """
        Add server output text, stripping ANSI sequences first.

        Splits on newlines and appends complete lines to the buffer.
        Incomplete trailing text is held in ``_partial`` until the
        next newline arrives.

        Complete lines whose stripped content exactly matches an entry
        in *echo_filter* are silently dropped (and removed from the
        set) so that echoed autoreply commands are never matched.

        :param text: Raw server output (may contain ANSI sequences).
        :param echo_filter: Set of sent command strings to suppress.
        :returns: ``True`` if new complete lines were added.
        """
        stripped = strip_sequences(text)
        if not stripped:
            return False

        parts = stripped.split("\n")

        # Prepend partial to first segment.
        parts[0] = self._partial + parts[0]

        if len(parts) == 1:
            # No newline in this chunk -- accumulate partial.
            self._partial = parts[0]
            if self._partial and self.new_text is not None:
                self.new_text.set()
            return False

        # Last element is the new partial (may be empty string).
        self._partial = parts[-1]

        # Everything except the last element is a complete line.
        # Drop lines that are echoes of commands we sent.
        new_lines: list[str] = []
        for line in parts[:-1]:
            line = line.rstrip("\r")
            if echo_filter and line.strip() in echo_filter:
                echo_filter.discard(line.strip())
            else:
                new_lines.append(line)
        self._lines.extend(new_lines)
        self.cull()
        if new_lines and self.new_text is not None:
            self.new_text.set()
        return True

    def get_searchable_text(self) -> str:
        """
        Return text from last match position forward.

        Joins lines from ``last_match_line`` onward with newlines,
        including the current partial (incomplete) line so that
        prompts without trailing newlines can be matched.

        :returns: Searchable text substring.
        """
        if self.last_match_line >= len(self._lines) and not self._partial:
            return ""
        text = "\n".join(self._lines[self.last_match_line :])
        if self._partial:
            if text:
                text += "\n" + self._partial
            else:
                text = self._partial
        return text[self.last_match_col :]

    def advance_match(self, offset_in_searchable: int, length: int) -> None:
        """
        Update last match position past the given match.

        :param offset_in_searchable: Start offset of match within
            the text returned by :meth:`get_searchable_text`.
        :param length: Length of the match.
        """
        # Convert searchable-text offset back to absolute (line, col).
        abs_offset = self.last_match_col + offset_in_searchable + length
        for i in range(self.last_match_line, len(self._lines)):
            line_len = len(self._lines[i])
            if i > self.last_match_line:
                line_len += 1  # account for the \n separator
            if abs_offset <= line_len:
                self.last_match_line = i
                self.last_match_col = abs_offset
                return
            abs_offset -= line_len + (1 if i == self.last_match_line else 0)

        # Past the last line -- offset is within the partial.
        self.last_match_line = len(self._lines)
        self.last_match_col = abs_offset

    def clear(self) -> None:
        """Reset buffer for a new EOR/GA record, preserving partial line."""
        self._lines.clear()
        self.last_match_line = 0
        self.last_match_col = 0

    def reset_match_position(self) -> None:
        """Reset match position to start so retained text is re-searchable."""
        self.last_match_line = 0
        self.last_match_col = 0

    def cull(self) -> None:
        """Remove oldest lines beyond *max_lines*, adjusting match position."""
        if len(self._lines) <= self.max_lines:
            return
        excess = len(self._lines) - self.max_lines
        self._lines = self._lines[excess:]
        self.last_match_line = max(0, self.last_match_line - excess)
        if self.last_match_line == 0 and excess > 0:
            self.last_match_col = 0

    async def wait_for_pattern(
        self, pattern: re.Pattern[str], timeout: float
    ) -> Optional[re.Match[str]]:
        """
        Wait for *pattern* to appear in the buffer within *timeout* seconds.

        Creates a lazy :class:`asyncio.Event` that is signalled by
        :meth:`add_text` whenever new complete lines arrive.

        :param pattern: Compiled regex to search for.
        :param timeout: Maximum seconds to wait.
        :returns: The match object, or ``None`` on timeout.
        """
        if self.new_text is None:
            self.new_text = asyncio.Event()
        evt = self.new_text
        deadline = time.monotonic() + timeout
        while True:
            evt.clear()
            text = self.get_searchable_text()
            if text:
                m = pattern.search(text)
                if m:
                    self.advance_match(m.start(), len(m.group(0)))
                    return m
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(evt.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                text = self.get_searchable_text()
                if text:
                    m = pattern.search(text)
                    if m:
                        self.advance_match(m.start(), len(m.group(0)))
                        return m
                return None


@dataclass
class ExclusiveState:
    """
    Mutable bundle of exclusive-mode state variables.

    All rules are exclusive by default: when any rule fires, ``active``
    is set and cleared when the reply chain task completes.
    """

    active: bool = False
    rule_index: int = 0

    def clear(self) -> None:
        """Reset all fields to defaults."""
        self.active = False
        self.rule_index = 0


class AutoreplyEngine:
    """
    Matches server output against autoreply rules and queues replies.

    Replies are chained sequentially: if reply A has a 5s delay, reply B
    waits for A to complete before starting.

    :param rules: Autoreply rules to match against.
    :param ctx: Session context (provides writer for sending and GMCP data).
    :param log: Logger instance.
    :param max_lines: SearchBuffer capacity.
    """

    def __init__(
        self,
        rules: list[AutoreplyRule],
        ctx: "SessionContext",
        log: logging.Logger,
        max_lines: int = 100,
        insert_fn: Optional[Callable[[str], None]] = None,
        echo_fn: Optional[Callable[[str], None]] = None,
        wait_fn: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        """Initialize AutoreplyEngine with rules and I/O handles."""
        self.rules = rules
        self.ctx = ctx
        self.log = log
        self._buffer = SearchBuffer(max_lines=max_lines)
        self.reply_chain: Optional[asyncio.Task[None]] = None
        self.insert_fn = insert_fn
        self.echo_fn = echo_fn
        self.wait_fn = wait_fn
        self.excl = ExclusiveState()
        self.sent_commands: set[str] = set()
        self.sent_commands_max: int = int(os.environ.get("TELNETLIB3_SENT_COMMANDS_MAX", "10000"))
        self.prompt_based = False
        self._cycle_matched: set[int] = set()
        self.condition_blocked: set[int] = set()
        self.condition_retried: bool = False
        self._enabled = True
        self._last_matched_pattern: str = ""
        self.condition_failed: Optional[tuple[int, str]] = None
        self.status: str = ""
        self.until_start: float = 0.0
        self.until_deadline: float = 0.0

    def pop_condition_failed(self) -> Optional[tuple[int, str]]:
        """
        Return and clear the last condition failure.

        :returns: ``(rule_index_1based, description)`` if last match failed
            a condition, otherwise ``None``.
        """
        val = self.condition_failed
        self.condition_failed = None
        return val

    @property
    def buffer(self) -> SearchBuffer:
        """The underlying :class:`SearchBuffer`."""
        return self._buffer

    @property
    def exclusive_active(self) -> bool:
        """``True`` when an exclusive rule is suppressing normal matching."""
        return self.excl.active

    @property
    def exclusive_rule_index(self) -> int:
        """1-based index of the active exclusive rule, or 0 if none."""
        return self.excl.rule_index

    @property
    def enabled(self) -> bool:
        """When ``False``, all rule matching is suspended."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def reply_pending(self) -> bool:
        """``True`` when a reply chain is still executing."""
        return self.reply_chain is not None and not self.reply_chain.done()

    @property
    def cycle_matched(self) -> bool:
        """``True`` if any rule matched in the current prompt cycle."""
        return len(self._cycle_matched) > 0

    @property
    def last_matched_pattern(self) -> str:
        """Pattern string of the most recently matched rule, or ``""``."""
        return self._last_matched_pattern

    @property
    def status_text(self) -> str:
        """Short description of current activity, or ``""``."""
        return self.status

    @property
    def until_progress(self) -> Optional[float]:
        """Fraction of until/untils timeout elapsed, 0.0-1.0, or ``None``."""
        if self.until_deadline <= self.until_start:
            return None
        now = monotonic()
        if now >= self.until_deadline:
            return 1.0
        if now <= self.until_start:
            return 0.0
        return (now - self.until_start) / (self.until_deadline - self.until_start)

    def feed(self, text: str) -> None:
        """
        Feed server output text and check for matches.

        Called from the server output handler in both REPL and raw modes. Searches after every
        chunk, including partial lines, so that MUD prompts without trailing newlines are matched.

        :param text: Server output text.
        """
        if not self._enabled:
            return
        self._buffer.add_text(text, self.sent_commands)

        if self.excl.active:
            self.match_always_rules()
            return

        searchable = self._buffer.get_searchable_text()
        if not searchable:
            return

        # Once prompt-based mode is active (GA/EOR seen), defer normal
        # matching until on_prompt() so that replies are never fired
        # mid-output.  Rules with immediate=True still fire here so
        # that asynchronous MUD events (no trailing GA/EOR) are caught.
        if self.prompt_based:
            self.match_rules(immediate_only=True)
            return

        self.match_rules()

    def match_rules(self, immediate_only: bool = False) -> None:
        """
        Run rule matching on buffered text.

        All rules are exclusive by default: when a rule fires, no other
        non-``always`` rules fire until the reply chain completes.

        When *immediate_only* is ``True``, only rules with
        ``immediate=True`` are checked.
        """
        searchable = self._buffer.get_searchable_text()
        if not searchable:
            return

        log_prefix = "immediate rule" if immediate_only else "rule"
        max_iterations = len(self.rules) * 2
        found = True
        while found and max_iterations > 0:
            found = False
            max_iterations -= 1
            searchable = self._buffer.get_searchable_text()
            if not searchable:
                break
            for rule_idx, rule in enumerate(self.rules):
                if not rule.enabled:
                    continue
                if immediate_only and not rule.immediate:
                    continue
                if rule_idx in self._cycle_matched:
                    if immediate_only or self.prompt_based:
                        continue
                if rule_idx in self.condition_blocked:
                    continue
                match = rule.pattern.search(searchable)
                if match:
                    self._last_matched_pattern = rule.pattern.pattern
                    if rule.when:
                        ok, desc = check_condition(rule.when, self.ctx)
                        if not ok:
                            self.log.info(
                                "autoreply: %s #%d skipped, condition failed: %s",
                                log_prefix,
                                rule_idx + 1,
                                desc,
                            )
                            self.condition_failed = (rule_idx + 1, desc)
                            self.condition_blocked.add(rule_idx)
                            found = True
                            break
                    self._cycle_matched.add(rule_idx)
                    self._buffer.advance_match(match.start(), len(match.group(0)))
                    rule.last_fired = datetime.now(timezone.utc).isoformat()
                    if hasattr(self.ctx, "mark_autoreplies_dirty"):
                        self.ctx.mark_autoreplies_dirty()
                    reply = substitute_groups(rule.reply, match)
                    self.queue_reply(reply)
                    if not immediate_only:
                        self.excl.active = True
                        self.excl.rule_index = rule_idx + 1
                        return
                    found = True
                    break

    def match_always_rules(self) -> None:
        """Check rules with ``always=True`` even during exclusive suppression."""
        searchable = self._buffer.get_searchable_text()
        if not searchable:
            return
        for rule_idx, rule in enumerate(self.rules):
            if not rule.enabled or not rule.always:
                continue
            if self.prompt_based and rule_idx in self._cycle_matched:
                continue
            match = rule.pattern.search(searchable)
            if match:
                self._last_matched_pattern = rule.pattern.pattern
                self._cycle_matched.add(rule_idx)
                self._buffer.advance_match(match.start(), len(match.group(0)))
                rule.last_fired = datetime.now(timezone.utc).isoformat()
                if hasattr(self.ctx, "mark_autoreplies_dirty"):
                    self.ctx.mark_autoreplies_dirty()
                reply = substitute_groups(rule.reply, match)
                self.queue_reply(reply)

    def queue_reply(self, reply_text: str) -> None:
        """
        Queue a reply, chaining after any pending reply task.

        :param reply_text: Fully substituted reply string.
        """
        prev = self.reply_chain

        async def chained() -> None:
            if prev is not None and not prev.done():
                await prev
            await self.execute_reply(reply_text)

        task = asyncio.ensure_future(chained())
        task.add_done_callback(self.on_reply_done)
        self.reply_chain = task

    def on_reply_done(self, task: "asyncio.Task[None]") -> None:
        """Clear exclusive state when the reply chain completes."""
        if self.reply_chain is not None and self.reply_chain.done():
            self.excl.clear()
            self.status = ""

    def set_status(self, text: str) -> None:
        """Set the engine's status string."""
        self.status = text

    def set_progress(self, start: float, deadline: float) -> None:
        """Set the until/delay progress window."""
        self.until_start = start
        self.until_deadline = deadline

    def clear_progress(self) -> None:
        """Clear the until/delay progress window."""
        self.until_start = self.until_deadline = 0.0

    async def execute_reply(self, reply_text: str) -> None:
        r"""
        Execute a single reply as a command language sequence.

        Supports ``;`` (wait for GA/EOR), ``|`` (send immediately),
        ```delay Ns```, ```when HP%>=N```, ```until [T] pat```,
        ```untils [T] pat```, and repeat prefixes (``3e``).

        :param reply_text: Fully substituted reply string.
        """
        from .client_repl_commands import (
            StepResult,
            DispatchHooks,
            dispatch_one,
            expand_commands_ex,
        )

        expanded = expand_commands_ex(reply_text)
        writer = self.ctx.writer
        mask_send = writer is not None and getattr(writer, "will_echo", False)
        activity_cb = getattr(self.ctx, "on_autoreply_activity", None)

        hooks = DispatchHooks(
            ctx=self.ctx,
            log=self.log,
            wait_fn=self.wait_fn,
            send_fn=self.send_command,
            echo_fn=None,
            on_status=self.set_status,
            on_progress=self.set_progress,
            on_progress_clear=self.clear_progress,
            on_activity=activity_cb,
            prompt_ready=None,
            search_buffer=self._buffer,
        )
        sent_count = 0
        for idx, cmd in enumerate(expanded.commands):
            result = await dispatch_one(
                cmd, idx, sent_count, expanded.immediate_set, hooks, mask_send=mask_send
            )
            if result is StepResult.ABORT:
                return
            if result is StepResult.SENT:
                sent_count += 1
        self.status = ""

    def send_command(self, cmd: str) -> None:
        """
        Send a single command line to the server.

        :param cmd: Command text (without line ending).
        """
        if not cmd or not cmd.strip():
            return
        if self.ctx.randomwalk_auto_evaluate and self.ctx.randomwalk_active:
            m = KILL_RE.match(cmd)
            if m:
                consider_cmd = f"consider {m.group(1)}"
                self.log.info("autoreply: injecting %r before %r", consider_cmd, cmd)
                if self.echo_fn is not None:
                    self.echo_fn(consider_cmd)
                assert self.ctx.writer is not None
                self.ctx.writer.write(consider_cmd + "\r\n")  # type: ignore[arg-type]
        self.log.info("autoreply: sending %r", cmd)
        self.sent_commands.add(cmd.strip())
        if len(self.sent_commands) > self.sent_commands_max:
            self.sent_commands.clear()
        if self.echo_fn is not None:
            self.echo_fn(cmd)
        writer = self.ctx.writer
        if writer is not None and getattr(writer, "will_echo", False):
            self.ctx.active_command = scramble_password()
        else:
            self.ctx.active_command = cmd
        self.ctx.active_command_time = monotonic()
        if self.ctx.cx_dot is not None:
            self.ctx.cx_dot.trigger()
        if self.ctx.tx_dot is not None:
            self.ctx.tx_dot.trigger()
        assert self.ctx.writer is not None
        self.ctx.writer.write(cmd + "\r\n")  # type: ignore[arg-type]

    def on_prompt(self) -> None:
        """
        Match accumulated text and clear per-cycle state on EOR/GA.

        In prompt-based mode, :meth:`feed` defers normal rule matching
        to this method so that replies are never fired mid-output.

        Each EOR/GA resets the per-cycle deduplication set so that
        rules can match again in the next prompt cycle.
        """
        self.prompt_based = True
        if not self._enabled:
            return
        # Match on accumulated buffer before clearing -- this is where
        # deferred matches from feed() actually fire.
        if not self.excl.active:
            self.match_rules()
        # When rules matched text but their ``when`` condition failed
        # (e.g. HP too low), preserve the buffer so the text can be
        # retried on the next prompt cycle when conditions may have
        # changed (e.g. HP healed).  Only the condition-blocked rules
        # are re-eligible; rules that already fired keep their
        # _cycle_matched entry so they don't re-trigger on retained
        # text.  After one retry, clear normally to prevent loops.
        if self.condition_blocked and not self.condition_retried:
            self.condition_retried = True
            self._cycle_matched -= self.condition_blocked
            self.condition_blocked.clear()
            self._buffer.reset_match_position()
        else:
            self.condition_blocked.clear()
            self.condition_retried = False
            self._cycle_matched.clear()
            self._buffer.clear()

    def check_timeout(self) -> bool:
        """
        Check and clear exclusive mode if the deadline has passed.

        With the simplified exclusive model (all rules exclusive, cleared
        by reply-chain completion), this always returns ``False``.  Kept
        for API compatibility with travel code.

        :returns: ``True`` if exclusive was cleared by timeout.
        """
        return False

    def cancel(self) -> None:
        """Cancel any pending reply chain and clear exclusive state."""
        if self.reply_chain is not None and not self.reply_chain.done():
            self.reply_chain.cancel()
            self.reply_chain = None
        self.excl.clear()
        self.status = ""
        self.until_start = self.until_deadline = 0.0
        self.condition_blocked.clear()
        self.condition_retried = False
        self.sent_commands.clear()
