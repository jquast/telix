"""
Output text highlighting engine for MUD client sessions.

Provides :class:`HighlightRule` for defining patterns and their terminal
formatting, :class:`HighlightEngine` for applying highlights to output lines
while preserving existing SGR sequences, and persistence via
:func:`load_highlights` / :func:`save_highlights`.
"""

# std imports
import os
import re
import json
import typing
import logging
import datetime
import dataclasses
from typing import TYPE_CHECKING

# 3rd party
import wcwidth
import wcwidth.sgr_state

if TYPE_CHECKING:
    import blessed

    from .autoreply import AutoreplyRule
    from .session_context import SessionContext

# (start, end, highlight, stop_movement, rule_idx, match)
Span = tuple[int, int, str, bool, int, re.Match[str] | None]

__all__ = ("HighlightEngine", "HighlightRule", "load_highlights", "save_highlights", "validate_highlight")

RE_FLAGS = re.IGNORECASE | re.MULTILINE | re.DOTALL
DEFAULT_AUTOREPLY_HIGHLIGHT = "black_on_beige"

log = logging.getLogger(__name__)


@dataclasses.dataclass
class HighlightRule:
    """
    A single highlight pattern-action rule.

    :param pattern: Compiled regex pattern (case-insensitive).
    :param highlight: Blessed compoundable name, e.g. ``"blink_black_on_yellow"``.
    :param enabled: Whether this rule is active.
    :param stop_movement: Cancel discover/randomwalk when matched.
    :param builtin: ``True`` for the autoreply-pattern rule (undeletable).
    """

    pattern: re.Pattern[str]
    highlight: str
    enabled: bool = True
    stop_movement: bool = False
    builtin: bool = False
    case_sensitive: bool = False
    multiline: bool = False
    captured: bool = False
    capture_name: str = "captures"
    captures: list[dict[str, str]] = dataclasses.field(default_factory=list)


def validate_highlight(term: blessed.Terminal, name: str) -> bool:
    """
    Return ``True`` if *name* is a valid blessed compoundable.

    :param term: Blessed terminal instance.
    :param name: Compoundable attribute name, e.g. ``"bold_red_on_white"``.
    """
    try:
        attr = getattr(term, name)
    except Exception:
        return False
    return callable(attr)


def parse_entries(entries: list[dict[str, typing.Any]]) -> list[HighlightRule]:
    """Parse a list of highlight entry dicts into :class:`HighlightRule` instances."""
    rules: list[HighlightRule] = []
    for entry in entries:
        pattern_str = entry.get("pattern", "")
        highlight = entry.get("highlight", "")
        if not pattern_str or not highlight:
            continue
        enabled = bool(entry.get("enabled", True))
        stop_movement = bool(entry.get("stop_movement", False))
        builtin = bool(entry.get("builtin", False))
        case_sensitive = bool(entry.get("case_sensitive", False))
        multiline = bool(entry.get("multiline", False))
        captured = bool(entry.get("captured", False))
        capture_name = str(entry.get("capture_name", "captures"))
        captures_raw = entry.get("captures", [])
        captures = list(captures_raw) if isinstance(captures_raw, list) else []
        flags = re.MULTILINE | re.DOTALL
        if not case_sensitive:
            flags |= re.IGNORECASE
        try:
            compiled = re.compile(pattern_str, flags)
        except re.error as exc:
            raise ValueError(f"Invalid highlight pattern {pattern_str!r}: {exc}") from exc
        rules.append(
            HighlightRule(
                pattern=compiled,
                highlight=highlight,
                enabled=enabled,
                stop_movement=stop_movement,
                builtin=builtin,
                case_sensitive=case_sensitive,
                multiline=multiline,
                captured=captured,
                capture_name=capture_name,
                captures=captures,
            )
        )
    return rules


def load_highlights(path: str, session_key: str) -> list[HighlightRule]:
    """
    Load highlight rules for a session from a JSON file.

    :param path: Path to the highlights JSON file.
    :param session_key: Session identifier (``"host:port"``).
    :returns: List of :class:`HighlightRule` instances.
    :raises FileNotFoundError: When *path* does not exist.
    :raises ValueError: When JSON structure is invalid or regex fails.
    """
    with open(path, encoding="utf-8") as fh:
        data: dict[str, typing.Any] = json.load(fh)
    session_data: dict[str, typing.Any] = data.get(session_key, {})
    entries: list[dict[str, typing.Any]] = session_data.get("highlights", [])
    return parse_entries(entries)


def save_highlights(path: str, rules: list[HighlightRule], session_key: str) -> None:
    """
    Save highlight rules for a session to a JSON file.

    Other sessions' data in the file is preserved.

    :param path: Path to the highlights JSON file.
    :param rules: List of :class:`HighlightRule` instances to save.
    :param session_key: Session identifier (``"host:port"``).
    """
    data: dict[str, typing.Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    data[session_key] = {
        "highlights": [
            {
                "pattern": r.pattern.pattern,
                "highlight": r.highlight,
                "enabled": r.enabled,
                "stop_movement": r.stop_movement,
                "builtin": r.builtin,
                **({"case_sensitive": True} if r.case_sensitive else {}),
                **({"multiline": True} if r.multiline else {}),
                **({"captured": True} if r.captured else {}),
                **({"capture_name": r.capture_name} if r.captured and r.capture_name != "captures" else {}),
                **({"captures": r.captures} if r.captures else {}),
            }
            for r in rules
        ]
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


class CompiledRuleSet:
    """
    A single combined regex built from all highlight + autoreply patterns.

    Each source pattern becomes a named group ``hl0``, ``hl1``, etc.
    A single :meth:`finditer` call replaces N separate passes.
    """

    __slots__ = ("combined", "group_map")

    def __init__(
        self,
        rules: list[HighlightRule],
        autoreply_rules: list[AutoreplyRule],
        autoreply_highlight: str,
        autoreply_enabled: bool,
    ) -> None:
        parts: list[str] = []
        self.group_map: list[tuple[str, bool, int]] = []

        if autoreply_enabled:
            for ar in autoreply_rules:
                if not ar.enabled:
                    continue
                gname = f"hl{len(parts)}"
                parts.append(f"(?P<{gname}>{ar.pattern.pattern})")
                self.group_map.append((autoreply_highlight, False, -1))

        for rule_i, rule in enumerate(rules):
            if not rule.enabled:
                continue
            gname = f"hl{len(parts)}"
            pat = rule.pattern.pattern
            if rule.case_sensitive:
                parts.append(f"(?P<{gname}>(?-i:{pat}))")
            else:
                parts.append(f"(?P<{gname}>{pat})")
            self.group_map.append((rule.highlight, rule.stop_movement, rule_i))

        self.combined: re.Pattern[str] | None = None
        if parts:
            try:
                self.combined = re.compile("|".join(parts), RE_FLAGS)
            except re.error:
                self.combined = None

    def finditer(self, text: str) -> list[Span]:
        """Return non-overlapping :data:`Span` tuples."""
        if self.combined is None:
            return []
        spans: list[Span] = []
        for m in self.combined.finditer(text):
            gname = m.lastgroup
            if gname is None:
                continue
            idx = int(gname[2:])
            hl, stop, rule_idx = self.group_map[idx]
            start, end = m.start(), m.end()
            if spans and start < spans[-1][1]:
                continue
            spans.append((start, end, hl, stop, rule_idx, m))
        return spans


class HighlightEngine:
    """
    Applies highlight rules to output lines.

    Builds a single combined regex from all enabled highlight rules and
    autoreply patterns at init time. Each :meth:`process_line` call runs
    one :meth:`finditer` pass, not N separate ones.

    :param rules: User-defined highlight rules.
    :param autoreply_rules: Current autoreply rules (for builtin highlight).
    :param term: Blessed terminal instance.
    :param ctx: Session context (for stop_movement cancellation).
    :param autoreply_highlight: Blessed compoundable for autoreply pattern highlight.
    :param autoreply_enabled: Whether the builtin autoreply highlight is enabled.
    """

    def __init__(
        self,
        rules: list[HighlightRule],
        autoreply_rules: list[AutoreplyRule],
        term: blessed.Terminal,
        ctx: SessionContext | None = None,
        autoreply_highlight: str = DEFAULT_AUTOREPLY_HIGHLIGHT,
        autoreply_enabled: bool = True,
    ) -> None:
        """Initialize engine with highlight and autoreply *rules*."""
        self.term = term
        self.ctx = ctx
        self.rules = list(rules)
        sl_rules = [r for r in rules if not r.multiline]
        # Map single-line ruleset indices back to full rules indices.
        self.sl_indices = [i for i, r in enumerate(rules) if not r.multiline]
        self.ruleset = CompiledRuleSet(sl_rules, autoreply_rules, autoreply_highlight, autoreply_enabled)
        self.enabled = True
        self.highlight_cache: dict[str, str] = {}

    def get_highlight_seq(self, name: str) -> str:
        """Return the SGR sequence string for a blessed compoundable name."""
        if name not in self.highlight_cache:
            try:
                attr = getattr(self.term, name)
                self.highlight_cache[name] = str(attr)
            except Exception:
                self.highlight_cache[name] = ""
        return self.highlight_cache[name]

    def process_line(self, line: str) -> tuple[str, bool]:
        """
        Apply highlight rules to a single line of output.

        :param line: A single line of terminal output (may contain SGR sequences).
        :returns:``(highlighted_line, had_matches)`` -- the original line is
            returned unchanged when no rules match.
        """
        if not self.enabled:
            return line, False

        plain = wcwidth.strip_sequences(line)
        if not plain:
            return line, False

        spans = self.collect_spans(plain)
        if not spans:
            return line, False

        self.extract_captures(spans, plain)
        stop_notice = self.handle_stop_movement(spans)
        rebuilt = self.rebuild_line(line, plain, spans)
        if stop_notice:
            rebuilt = rebuilt.rstrip("\r\n") + stop_notice + "\r\n"
        return rebuilt, True

    def collect_spans(self, plain: str) -> list[Span]:
        """
        Collect all highlight match spans from enabled rules.

        Delegates to the combined :class:`CompiledRuleSet` for a single-pass
        :meth:`finditer` over all patterns.  Remaps ``rule_idx`` from the
        single-line subset back to the full ``rules`` list.

        :returns: List of :data:`Span` tuples sorted by start position,
            with overlaps resolved (first rule wins).
        """
        spans = self.ruleset.finditer(plain)
        if not self.sl_indices:
            return spans
        return [(s, e, hl, stop, (self.sl_indices[ri] if ri >= 0 else ri), m) for s, e, hl, stop, ri, m in spans]

    def extract_captures(self, spans: list[Span], plain: str) -> None:
        r"""
        Extract capture data from matched spans.

        Populates ``ctx.captures`` and ``ctx.capture_log``.

        For each span whose rule has ``captured=True``:

        - The full matched line is always logged to
          ``ctx.capture_log[rule.capture_name]``.
        - If the rule has a ``captures`` list, each entry's ``value``
          template (e.g. ``\1``) is resolved against a re-match of the
          rule's own pattern on the matched text, and the integer result
          is stored in ``ctx.captures``.
        """
        ctx = self.ctx
        if ctx is None:
            return

        group_ref = re.compile(r"\\(\d+)")
        for _s, _e, _hl, _stop, rule_idx, match in spans:
            if rule_idx < 0 or match is None:
                continue
            if rule_idx >= len(self.rules):
                continue
            rule = self.rules[rule_idx]
            if not rule.captured:
                continue
            # Re-match with the rule's own pattern to get correct group
            # numbers.
            matched_text = match.group(0)
            rematch = rule.pattern.search(matched_text)
            # Resolve group refs in channel name (e.g. \1 -> "Bob").
            channel = rule.capture_name
            if rematch is not None and group_ref.search(channel):
                channel = group_ref.sub(lambda m2: rematch.group(int(m2.group(1))) or "", channel)
            entry = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "line": plain,
                "highlight": rule.highlight,
            }
            ctx.capture_log.setdefault(channel, []).append(entry)
            if not rule.captures or rematch is None:
                continue
            for cap in rule.captures:
                key = cap.get("key", "")
                value_tmpl = cap.get("value", "")
                if not key or not value_tmpl:
                    continue
                resolved = group_ref.sub(lambda m2: (rematch.group(int(m2.group(1))) or ""), value_tmpl)
                try:
                    ctx.captures[key] = int(resolved)
                except (ValueError, TypeError):
                    pass

    def handle_stop_movement(self, spans: list[Span]) -> str | None:
        """
        Cancel discover/randomwalk tasks if any span has stop_movement.

        :returns: Cyan-colored notice string to append, or ``None``.
        """
        ctx = self.ctx
        if ctx is None:
            return None
        cancelled: list[str] = []
        for _s, _e, _hl, stop, _ri, _m in spans:
            if not stop:
                continue
            if ctx.discover_active and ctx.discover_task is not None:
                ctx.discover_task.cancel()
                ctx.discover_active = False
                ctx.discover_current = 0
                ctx.discover_total = 0
                cancelled.append("discover")
                log.info("highlighter: stop_movement cancelled discover")
            if ctx.randomwalk_active and ctx.randomwalk_task is not None:
                ctx.randomwalk_task.cancel()
                ctx.randomwalk_active = False
                ctx.randomwalk_current = 0
                ctx.randomwalk_total = 0
                cancelled.append("random walk")
                log.info("highlighter: stop_movement cancelled randomwalk")
            break
        if not cancelled:
            return None
        cyan = str(self.term.cyan)
        normal = str(self.term.normal)
        modes = ", ".join(cancelled)
        return f" {cyan}[stop: {modes} cancelled]{normal}"

    def rebuild_line(self, line: str, plain: str, spans: list[Span]) -> str:
        """
        Rebuild *line* injecting highlight SGR at matched spans.

        Iterates through the original line using
        :func:`iter_sequences` to separate text from escape sequences.
        Tracks position in the stripped *plain* text to know when
        entering/exiting highlight spans. Preserves all original escape
        sequences and restores SGR state after each highlight span ends.
        """
        sgr_state = wcwidth.sgr_state._SGR_STATE_DEFAULT
        span_idx = 0
        plain_pos = 0
        in_highlight = False
        output: list[str] = []

        for segment, is_seq in wcwidth.iter_sequences(line):
            if is_seq:
                if wcwidth.sgr_state._SGR_PATTERN.match(segment):
                    sgr_state = wcwidth.sgr_state._sgr_state_update(sgr_state, segment)
                if not in_highlight:
                    output.append(segment)
                continue

            for grapheme in wcwidth.iter_graphemes(segment):
                if span_idx < len(spans):
                    s_start, s_end, hl_name, _stop, _ri, _m = spans[span_idx]

                    if not in_highlight and plain_pos >= s_start:
                        saved_sgr = sgr_state
                        hl_seq = self.get_highlight_seq(hl_name)
                        if hl_seq:
                            output.append(hl_seq)
                        in_highlight = True

                    if in_highlight and plain_pos >= s_end:
                        output.append("\x1b[0m")
                        restore = wcwidth.sgr_state._sgr_state_to_sequence(saved_sgr)
                        if restore:
                            output.append(restore)
                        in_highlight = False
                        span_idx += 1

                        if span_idx < len(spans):
                            s_start, s_end, hl_name, _stop, _ri, _m = spans[span_idx]
                            if plain_pos >= s_start:
                                saved_sgr = sgr_state
                                hl_seq = self.get_highlight_seq(hl_name)
                                if hl_seq:
                                    output.append(hl_seq)
                                in_highlight = True

                output.append(grapheme)
                plain_pos += len(grapheme)

        if in_highlight:
            output.append("\x1b[0m")
            restore = wcwidth.sgr_state._sgr_state_to_sequence(sgr_state)
            if restore:
                output.append(restore)

        return "".join(output)

    @staticmethod
    def normalize_plain(plain: str) -> tuple[str, list[int]]:
        r"""
        Strip ``\r``, return ``(normalized, position_map)``.

        The position map translates indices in the normalized string
        back to their positions in the original *plain* text.
        """
        pos_map = [i for i, ch in enumerate(plain) if ch != "\r"]
        return plain.replace("\r", ""), pos_map

    def process_block(self, block: str) -> tuple[str, bool]:
        """
        Apply multiline highlight rules to a full text block.

        Only multiline rules participate.  If none are enabled, returns
        the block unchanged.  After highlighting, SGR codes are
        propagated across line boundaries so each line is
        self-contained.

        :param block: Multi-line terminal output (may contain SGR sequences).
        :returns: ``(highlighted_block, had_matches)``.
        """
        if not self.enabled:
            return block, False

        ml_entries = [(i, r) for i, r in enumerate(self.rules) if r.multiline and r.enabled]
        if not ml_entries:
            return block, False

        plain = wcwidth.strip_sequences(block)
        if not plain:
            return block, False

        normalized, pos_map = self.normalize_plain(plain)

        parts: list[str] = []
        group_map: list[tuple[str, bool, int]] = []
        for full_idx, rule in ml_entries:
            gname = f"ml{len(parts)}"
            pat = rule.pattern.pattern
            if rule.case_sensitive:
                parts.append(f"(?P<{gname}>(?-i:{pat}))")
            else:
                parts.append(f"(?P<{gname}>{pat})")
            group_map.append((rule.highlight, rule.stop_movement, full_idx))

        combined = re.compile("|".join(parts), RE_FLAGS)
        spans: list[Span] = []
        for m in combined.finditer(normalized):
            gname = m.lastgroup
            if gname is None:
                continue
            idx = int(gname[2:])
            hl, stop, rule_idx = group_map[idx]
            n_start, n_end = m.start(), m.end()
            # Remap positions back through the \r-stripped pos_map.
            orig_start = pos_map[n_start] if n_start < len(pos_map) else len(plain)
            orig_end = pos_map[n_end - 1] + 1 if n_end > 0 and n_end - 1 < len(pos_map) else len(plain)
            if spans and orig_start < spans[-1][1]:
                continue
            spans.append((orig_start, orig_end, hl, stop, rule_idx, m))

        if not spans:
            return block, False

        self.extract_captures(spans, plain)
        stop_notice = self.handle_stop_movement(spans)
        rebuilt = self.rebuild_line(block, plain, spans)
        if stop_notice:
            rebuilt = rebuilt.rstrip("\r\n") + stop_notice + "\r\n"

        lines = rebuilt.split("\n")
        lines = wcwidth.propagate_sgr(lines)
        return "\n".join(lines), True
