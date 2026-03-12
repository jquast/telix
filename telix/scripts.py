"""
Async Python scripting engine for telix.

Provides :class:`ScriptOutputBuffer` for per-script server output buffering,
:class:`ScriptContext` for the user-facing scripting API, and
:class:`ScriptManager` for loading, running, and stopping scripts.

Scripts are Python files with ``async def run(ctx)`` (or another named
function) that receive a :class:`ScriptContext` as their sole argument.
"""

# std imports
import os
import re
import sys
import shlex
import typing
import asyncio
import logging
import warnings
import importlib
import traceback
import collections
from typing import TYPE_CHECKING

# 3rd party
import wcwidth

if TYPE_CHECKING:
    from . import rooms
    from .session_context import TelixSessionContext

log = logging.getLogger(__name__)


class ScriptOutputBuffer:
    """
    Per-script output accumulator with pattern and prompt waiting.

    Each running script has its own buffer so that concurrent scripts do not interfere with each other's output
    matching.

    :param max_lines: Maximum number of lines to retain (default 200).
    :param max_turns: Maximum number of prompt-delimited turns to retain (default 50).
    """

    def __init__(self, max_lines: int = 200, max_turns: int = 50) -> None:
        """Initialize the buffer."""
        self._lines: list[str] = []
        self._partial: str = ""
        self._turns: collections.deque[str] = collections.deque(maxlen=max_turns)
        self._prompt_count: int = 0
        self._current_turn_lines: list[str] = []
        self._output_event: asyncio.Event = asyncio.Event()
        self._prompt_event: asyncio.Event = asyncio.Event()
        self._waiters: list[tuple[re.Pattern[str], asyncio.Future[re.Match[str] | None]]] = []
        self._cursor: int = 0
        self.max_lines = max_lines

    def feed(self, text: str) -> None:
        """
        Feed server output text into the buffer.

        Strips ANSI sequences, accumulates lines, and resolves any registered pattern waiters.

        :param text: Raw server output text (may contain ANSI sequences).
        """
        stripped = wcwidth.strip_sequences(text)
        if not stripped:
            return

        parts = stripped.split("\n")
        parts[0] = self._partial + parts[0]

        if len(parts) == 1:
            self._partial = parts[0]
            self._output_event.set()
            return

        self._partial = parts[-1]
        new_lines = [line.rstrip("\r") for line in parts[:-1]]
        self._lines.extend(new_lines)
        self._current_turn_lines.extend(new_lines)
        if len(self._lines) > self.max_lines:
            excess = len(self._lines) - self.max_lines
            self._lines = self._lines[-self.max_lines :]
            self._cursor = max(0, self._cursor - excess)

        self._output_event.set()
        self._resolve_waiters()

    def on_prompt(self) -> None:
        """
        Signal end of a server output turn (GA/EOR received).

        Closes the current turn, appends it to the turns deque, increments the
        prompt counter, and sets the prompt event so that :meth:`wait_for_prompt`
        can return.
        """
        turn_text = "\n".join(self._current_turn_lines)
        if self._partial:
            turn_text = (turn_text + "\n" + self._partial).lstrip("\n")
        self._turns.append(turn_text)
        self._current_turn_lines.clear()
        self._prompt_count += 1
        self._prompt_event.set()
        self._prompt_event = asyncio.Event()
        self._resolve_waiters()

    def _resolve_waiters(self) -> None:
        """Check all pending pattern waiters against the current buffer."""
        if not self._waiters:
            return
        remaining = []
        for pattern, fut in self._waiters:
            if fut.done():
                continue
            text = self._text_from_cursor()
            m = pattern.search(text)
            if m:
                self._advance_cursor(text, m.end())
                fut.set_result(m)
            else:
                remaining.append((pattern, fut))
        self._waiters = remaining

    def _full_text(self) -> str:
        """Return all buffered text including the current partial line."""
        text = "\n".join(self._lines)
        if self._partial:
            text = text + "\n" + self._partial if text else self._partial
        return text

    def _text_from_cursor(self) -> str:
        """Return buffered text from the cursor (first unconsumed line) onward."""
        lines = self._lines[self._cursor :]
        text = "\n".join(lines)
        if self._partial:
            text = text + "\n" + self._partial if text else self._partial
        return text

    def _advance_cursor(self, text: str, match_end: int) -> None:
        """
        Advance the cursor past the content consumed by a match.

        :param text: The text that was searched (result of :meth:`_text_from_cursor`).
        :param match_end: End position of the match within *text*.
        """
        lines_from_cursor = self._lines[self._cursor :]
        committed_len = sum(len(ln) for ln in lines_from_cursor) + max(0, len(lines_from_cursor) - 1)
        newlines = text[:match_end].count("\n")
        if match_end <= committed_len:
            self._cursor = min(self._cursor + newlines + 1, len(self._lines))
        else:
            self._cursor = min(self._cursor + len(lines_from_cursor), len(self._lines))

    async def wait_for_pattern(self, pattern: re.Pattern[str], timeout: float | None) -> "re.Match[str] | None":
        """
        Wait for *pattern* to appear in the buffer within *timeout* seconds.

        Each call consumes the matched content so subsequent calls cannot re-match the same text.
        If the pattern appears multiple times between prompts it will match once per call.

        :param pattern: Compiled regex pattern to search for.
        :param timeout: Maximum seconds to wait.
        :returns: The match object, or ``None`` on timeout.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[re.Match[str] | None] = loop.create_future()

        text = self._text_from_cursor()
        if text:
            m = pattern.search(text)
            if m:
                self._advance_cursor(text, m.end())
                return m

        self._waiters.append((pattern, fut))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            if not fut.done():
                fut.cancel()
            return None

    async def wait_for_prompt(self, timeout: float | None = None) -> bool:
        """
        Wait until the next GA/EOR prompt signal.

        :param timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        :returns: ``True`` if prompt arrived, ``False`` on timeout.
        """
        target = self._prompt_count + 1
        if timeout is None:
            deadline = None
        else:
            deadline = asyncio.get_event_loop().time() + timeout
        while self._prompt_count < target:
            if deadline is not None:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return False
            else:
                remaining = None
            evt = self._prompt_event
            try:
                await asyncio.wait_for(evt.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return self._prompt_count >= target
        return True

    def output(self, clear: bool = False) -> str:
        """
        Return the accumulated output text.

        :param clear: If ``True``, clear the buffer after returning.
        :returns: Joined lines as a single string.
        """
        text = self._full_text()
        if clear:
            self._lines.clear()
            self._partial = ""
            self._current_turn_lines.clear()
            self._cursor = 0
        return text

    def turns(self, n: int = 5) -> list[str]:
        """
        Return the last *n* prompt-delimited output turns.

        :param n: Number of most recent turns to return.
        :returns: List of turn text strings (oldest first).
        """
        all_turns = list(self._turns)
        return all_turns[-n:] if n < len(all_turns) else all_turns


class ScriptContext:
    """
    User-facing API handed to scripts as their ``ctx`` argument.

    Every script receives a single ``ctx`` argument that provides access to all
    known information about the MUD session and scripting capabilities of Telix.

    :param session_ctx: The live session context.
    :param buf: Per-script output buffer.
    :param log_inst: Logger for the script.
    """

    def __init__(self, session_ctx: "TelixSessionContext", buf: ScriptOutputBuffer, log_inst: logging.Logger) -> None:
        """Initialize ScriptContext."""
        self._ctx = session_ctx
        self._buf = buf
        self._log = log_inst

    @property
    def gmcp(self) -> dict[str, typing.Any]:
        """The full GMCP data dict from the session context."""
        return self._ctx.gmcp_data

    @property
    def room_id(self) -> str:
        """Current room number string."""
        return self._ctx.room.current

    @property
    def room_graph(self) -> "rooms.RoomStore | None":
        """The :class:`~telix.rooms.RoomStore` for this session, or ``None``."""
        return self._ctx.room.graph

    @property
    def captures(self) -> dict[str, typing.Any]:
        """Highlight capture variables for this session."""
        return self._ctx.highlights.captures

    @property
    def room(self) -> "rooms.Room | None":
        """
        The current :class:`~telix.rooms.Room`, or ``None`` if unknown.

        Returns ``None`` if no GMCP room data has been received.  The room
        object has ``name``, ``area``, and ``exits`` attributes.
        """
        rg = self._ctx.room.graph
        if rg is None or not self._ctx.room.current:
            return None
        return rg.get_room(self._ctx.room.current)

    def gmcp_get(self, dotted_path: str) -> typing.Any:
        """
        Retrieve a value from the GMCP data dict by dot-separated path.

        Handles both flat dotted top-level keys (e.g. ``"Char.Vitals"`` stored
        as a single key in the dict) and nested dict hierarchies.  At each
        level the longest matching prefix is tried first, then progressively
        shorter ones, so both storage styles work transparently.

        A bare field name without dots (e.g. ``"Water"``) searches across all
        GMCP packages automatically.

        If the path ends with ``%``, the value is computed as a ratio of the
        field to its ``Max`` counterpart and returned as a float between 0.0
        and 1.0.  Both dotted and bare forms work::

            ctx.gmcp_get("Char.Guild.Stats.Water%")
            ctx.gmcp_get("Water%")

        The ``Max`` lookup is case-insensitive (``MaxWater``, ``maxwater``, etc.).

        :param dotted_path: Key path, e.g. ``"Char.Vitals.hp"`` or ``"hp"``.
        :returns: The value at that path, or ``None`` if not found.
        """
        if dotted_path.endswith("%"):
            return self._gmcp_get_pct(dotted_path[:-1])
        return self._gmcp_get_raw(dotted_path)

    def _gmcp_get_raw(self, dotted_path: str) -> typing.Any:
        """Walk the GMCP data dict by *dotted_path* and return the leaf value."""
        parts = dotted_path.split(".")
        node = self._ctx.gmcp_data
        i = 0
        while i < len(parts):
            if not isinstance(node, dict):
                return None
            found = False
            for j in range(len(parts), i, -1):
                key = ".".join(parts[i:j])
                if key in node:
                    node = node[key]
                    i = j
                    found = True
                    break
            if not found:
                if i == 0 and len(parts) == 1:
                    val, _ = self._gmcp_search_field(parts[0])
                    return val
                return None
        if node is self._ctx.gmcp_data:
            return None
        return node

    def _gmcp_search_field(self, field: str) -> tuple[typing.Any, dict[str, typing.Any] | None]:
        """
        Search all GMCP package dicts for *field*.

        :returns: ``(value, package_dict)`` or ``(None, None)`` if not found.
        """
        for pkg_data in self._ctx.gmcp_data.values():
            if not isinstance(pkg_data, dict):
                continue
            val = pkg_data.get(field)
            if val is not None:
                return val, pkg_data
        return None, None

    def _gmcp_get_pct(self, dotted_path: str) -> float | None:
        """Return the ratio of a field to its ``Max`` counterpart as 0.0--1.0."""
        parts = dotted_path.rsplit(".", 1)
        if len(parts) == 2:
            pkg_path, field = parts
            pkg = self._gmcp_get_raw(pkg_path)
            if not isinstance(pkg, dict):
                return None
            cur_raw = pkg.get(field)
        else:
            field = parts[0]
            cur_raw, pkg = self._gmcp_search_field(field)
        if cur_raw is None or pkg is None:
            return None
        max_raw = pkg.get(f"Max{field}") or pkg.get(f"max{field}")
        if max_raw is None:
            lower_target = f"max{field.lower()}"
            for k, v in pkg.items():
                if k.lower() == lower_target:
                    max_raw = v
                    break
        if max_raw is None:
            return None
        try:
            cur = float(cur_raw)
            mx = float(max_raw)
        except (TypeError, ValueError):
            return None
        if mx <= 0:
            return None
        return cur / mx

    def get_room(self, num: str) -> "rooms.Room | None":
        """
        Look up a room by number.

        :param num: Room number string.
        :returns: :class:`~telix.rooms.Room` or ``None``.
        """
        rg = self._ctx.room.graph
        if rg is None:
            return None
        return rg.get_room(str(num))

    def find_path(self, dst: str) -> "list[str] | None":
        """
        Find a path of directions from the current room to *dst*.

        :param dst: Destination room number string.
        :returns: List of direction strings, or ``None`` if no path found.
        """
        rg = self._ctx.room.graph
        if rg is None or not self._ctx.room.current:
            return None
        return rg.find_path(self._ctx.room.current, str(dst))

    async def send(self, line: str, wait_prompt: bool = True) -> None:
        """
        Send a command string to the server.

        Supports the same syntax as the REPL:

        - ``;`` between commands waits for the server prompt before sending the next
        - ``|`` sends immediately without waiting
        - A leading number repeats: ``3north`` sends ``north`` three times

        Backtick directives like `` `async` `` and `` `until` `` are handled by the
        client, not sent to the server.  See :doc:`commands` for the full list.

        By default, ``send`` waits for the server prompt (GA/EOR) after all
        commands have been dispatched.  Pass ``wait_prompt=False`` to return
        immediately::

            await ctx.send("look")                       # waits for prompt
            await ctx.send("look", wait_prompt=False)    # returns immediately

        :param line: Command line to send.
        :param wait_prompt: Wait for the server prompt (GA/EOR) after sending.
            Defaults to ``True``.
        """
        from . import client_repl_travel, client_repl_commands

        expanded = client_repl_commands.expand_commands_ex(line)

        hooks = client_repl_commands.DispatchHooks(
            ctx=self._ctx,
            log=self._log,
            wait_fn=self._ctx.prompt.wait_fn,
            send_fn=self._send,
            echo_fn=self._ctx.prompt.echo,
            on_send=self._ctx.commands.record,
            prompt_ready=self._ctx.prompt.ready,
            search_buffer=self._buf,
        )
        parts = list(expanded.commands)
        immediate_set = expanded.immediate_set
        sent_count = 0
        idx = 0
        while idx < len(parts):
            cmd = parts[idx]
            if client_repl_commands.TRAVEL_RE.match(cmd):
                remainder = await client_repl_travel.handle_travel_commands(
                    parts[idx:], self._ctx, self._log,
                )
                parts = remainder
                immediate_set = frozenset()
                idx = 0
                sent_count = 0
                continue
            result = await client_repl_commands.dispatch_one(
                cmd, idx, sent_count, immediate_set, hooks,
            )
            if result is client_repl_commands.StepResult.ABORT:
                break
            if result is client_repl_commands.StepResult.SENT:
                sent_count += 1
            idx += 1
        if wait_prompt and sent_count:
            await self.prompt()

    def _send(self, cmd: str) -> None:
        """Send a single command to the server."""
        self._log.info("script: sending %r", cmd)
        self._ctx.writer.write(cmd + "\r\n")

    async def prompt(self, timeout: float | None = None) -> bool:
        """
        Wait for the next GA/EOR signal from the server.

        :param timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        :returns: ``True`` if prompt arrived within *timeout*.
        """
        return await self._buf.wait_for_prompt(timeout)

    async def prompts(self, n: int, timeout: float | None = None) -> bool:
        """
        Wait for *n* consecutive server prompts.

        :param n: Number of prompts to wait for.
        :param timeout: Timeout in seconds for *each* prompt, or ``None`` to wait indefinitely.
        :returns: ``True`` if all prompts arrived; ``False`` if any timed out.
        """
        for _ in range(n):
            if not await self._buf.wait_for_prompt(timeout):
                return False
        return True

    def output(self, clear: bool = True) -> str:
        """
        Return accumulated server output text.

        :param clear: If ``True``, clear the buffer after returning (default ``True``).
        :returns: Output text string.
        """
        return self._buf.output(clear)

    def turns(self, n: int = 5) -> list[str]:
        """
        Return the last *n* prompt-delimited output turns.

        :param n: Number of most recent turns to return.
        :returns: List of turn text strings.
        """
        return self._buf.turns(n)

    async def wait_for(self, pattern: str, timeout: float | None = None) -> "re.Match[str] | None":
        """
        Wait for a regex pattern to appear in the server output.

        :param pattern: Regular expression string.
        :param timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        :returns: The :class:`re.Match` object, or ``None`` on timeout.
        """
        compiled = re.compile(pattern, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        return await self._buf.wait_for_pattern(compiled, timeout)

    async def _wait_for_condition(self, cond: dict[str, str], timeout: float | None) -> bool:
        """Wait until *cond* is satisfied, re-evaluating on each GMCP update."""
        from . import trigger as ar_mod

        evt = self._ctx.gmcp.any_update
        deadline = None if timeout is None else asyncio.get_event_loop().time() + timeout
        while True:
            ok, _ = ar_mod.check_condition(cond, self._ctx)
            if ok:
                return True
            if deadline is not None:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return False
            else:
                remaining = None
            try:
                await asyncio.wait_for(evt.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False

    async def condition_met(self, key: str, op: str, threshold: int | str, timeout: float | None = None) -> bool:
        """
        Wait until a GMCP/capture condition becomes true.

        Re-evaluates immediately on every GMCP update rather than polling.

        *key* uses the actual GMCP field name (case-sensitive).  A bare name
        searches across all GMCP packages; a dotted path resolves directly.
        Append ``%`` to compute a percentage from the field and its ``Max``
        counterpart::

            await ctx.condition_met("hp%", "<", 50)
            await ctx.condition_met("Char.Guild.Stats.Water%", "<", 50)

        String comparisons work with ``=`` and ``!=``::

            await ctx.condition_met("Mode", "!=", "Rage")

        If the key is not found in GMCP data, it falls back to highlight
        capture variables.

        :param key: Condition key (e.g. ``"hp%"``, ``"Mode"``).
        :param op: Comparison operator: ``">"``, ``"<"``, ``">="``, ``"<="``,
            ``"="``, ``"!="``.
        :param threshold: Numeric or string threshold.
        :param timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        :returns: ``True`` when condition is met, ``False`` on timeout.
        """
        return await self._wait_for_condition({key: f"{op}{threshold}"}, timeout)

    async def conditions_met(
        self, *conditions: tuple[str, str, int | str] | list[tuple[str, str, int | str]], timeout: float | None = None
    ) -> bool:
        """
        Wait until all conditions are true simultaneously.

        Each condition is a ``(key, op, threshold)`` tuple.  Tuples may be
        passed as separate arguments or as a single list::

            await ctx.conditions_met(("Mode", "!=", "Rage"), ("Adrenaline%", ">", 0))
            await ctx.conditions_met([("Mode", "!=", "Rage"), ("Adrenaline%", ">", 0)])

        Unlike running separate :meth:`condition_met` calls with
        :func:`asyncio.wait`, this checks all conditions atomically, so you are
        guaranteed they all hold simultaneously when it returns.

        :param conditions: ``(key, op, threshold)`` tuples, or a single list of them.
        :param timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        :returns: ``True`` when all conditions are met at the same time, ``False`` on timeout.
        """
        items: typing.Sequence[tuple[str, str, int | str]]
        if len(conditions) == 1 and isinstance(conditions[0], list):
            items = conditions[0]
        else:
            items = typing.cast(typing.Sequence[tuple[str, str, int | str]], conditions)
        cond = {key: f"{op}{threshold}" for key, op, threshold in items}
        return await self._wait_for_condition(cond, timeout)

    def print(self, *args: typing.Any, sep: str = " ") -> None:
        """
        Write args to the terminal scroll region (cyan).

        Behaves like the built-in :func:`print`: multiple positional arguments
        are joined with *sep*, and non-string values are converted via
        :func:`str`.  Uses the same echo mechanism as trigger notifications.

        :param args: Values to display.
        :param sep: Separator string inserted between values (default ``" "``).
        """
        text = sep.join(str(a) for a in args)
        echo = self._ctx.prompt.echo
        if echo is not None:
            echo(text)
        else:
            self._log.info("script print: %s", text)

    def debug(self, msg: str) -> None:
        """
        Write *msg* to the telix log at DEBUG level.

        :param msg: Message text.
        """
        self._log.debug("script: %s", msg)

    def info(self, msg: str) -> None:
        """
        Write *msg* to the telix log at INFO level.

        :param msg: Message text.
        """
        self._log.info("script: %s", msg)

    def warn(self, msg: str) -> None:
        """
        Write *msg* to the telix log at WARNING level.

        :param msg: Message text.
        """
        self._log.warning("script: %s", msg)

    def error(self, msg: str) -> None:
        """
        Write *msg* to the telix log at ERROR level.

        :param msg: Message text.
        """
        self._log.error("script: %s", msg)

    @property
    def session_key(self) -> str:
        """Session identifier string (``"host:port"``)."""
        return self._ctx.session_key

    @property
    def previous_room_id(self) -> str:
        """Room number string of the room visited before the current one."""
        return self._ctx.room.previous

    @property
    def capture_log(self) -> dict[str, list[dict[str, typing.Any]]]:
        """
        Full capture event history: ``{variable: [{value, time, ...}, ...]}``.

        Unlike :attr:`captures` (which holds only the current value), this dict
        accumulates every capture event so scripts can track trends over time.
        """
        return self._ctx.highlights.capture_log

    @property
    def chat_messages(self) -> list[dict[str, typing.Any]]:
        """List of received chat/tell message dicts for this session."""
        return self._ctx.chat.messages

    @property
    def chat_unread(self) -> int:
        """Number of unread chat messages since the last read."""
        return self._ctx.chat.unread

    @property
    def chat_channels(self) -> list[dict[str, typing.Any]]:
        """List of available chat channel dicts for this session."""
        return self._ctx.chat.channels

    async def room_changed(self, timeout: float | None = None) -> bool:
        """
        Wait until the next room transition (GMCP Room.Info received).

        Captures a reference to the current ``room.changed`` event before
        awaiting, so the caller is woken exactly once per transition even if
        another change fires immediately after::

            async def tracker(ctx: ScriptContext) -> None:
                while True:
                    if not await ctx.room_changed(timeout=60.0):
                        break
                    ctx.print(f"[tracker] {ctx.previous_room_id} -> {ctx.room_id}")

        :param timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        :returns: ``True`` if a transition occurred; ``False`` on timeout.
        """
        evt = self._ctx.room.changed
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def gmcp_changed(self, package: str | None = None, timeout: float | None = None) -> bool:
        """
        Wait until the next GMCP packet is received.

        ::

            async def watch_vitals(ctx: ScriptContext) -> None:
                while True:
                    if not await ctx.gmcp_changed("Char.Vitals", timeout=60.0):
                        break
                    hp = ctx.gmcp_get("Char.Vitals.hp")
                    ctx.print(f"[vitals] HP: {hp}")

            # wait for any GMCP update
            await ctx.gmcp_changed()

        :param package: GMCP package name, e.g. ``"Char.Vitals"``.
            Pass ``None`` (the default) to wait for any GMCP update.
        :param timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        :returns: ``True`` if a packet arrived; ``False`` on timeout.
        """
        if package is None:
            evt = self._ctx.gmcp.any_update
        else:
            events = self._ctx.gmcp.package_events
            if package not in events:
                events[package] = asyncio.Event()
            evt = events[package]
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def walk_active(self) -> bool:
        """``True`` if any automated walk (autodiscover, randomwalk, travel) is running."""
        w = self._ctx.walk
        return any(t is not None and not t.done() for t in (w.discover_task, w.randomwalk_task, w.travel_task))

    def stop_walk(self) -> None:
        """Cancel all active automated walk tasks (autodiscover, randomwalk, travel)."""
        w = self._ctx.walk
        for task in (w.discover_task, w.randomwalk_task, w.travel_task):
            if task is not None and not task.done():
                task.cancel()

    @property
    def running_scripts(self) -> list[str]:
        """
        Names of all currently running scripts.

        Each name is the first token of the spec used to start the script
        (e.g. ``"combat.hunt"``).  The calling script's own name is included.
        """
        mgr = self._ctx.scripts.manager
        if mgr is None:
            return []
        return list(mgr.active_scripts())

    @property
    def command_history(self) -> list[str]:
        """Recently sent commands (oldest first, up to 200 entries)."""
        return list(self._ctx.commands.history)

    @property
    def last_command(self) -> str | None:
        """Most recently issued command, or ``None`` if none yet."""
        h = self._ctx.commands.history
        return h[-1] if h else None

    async def command_issued(self, timeout: float | None = None) -> str | None:
        """
        Wait until the next command is sent by any source.

        Returns the command string, or ``None`` on timeout.  If a command was
        issued while no waiter was registered (e.g. during the event-loop gap
        between one call completing and the next being set up), it is buffered
        and returned immediately by the next call.

        :param timeout: Maximum seconds to wait, or ``None`` to wait indefinitely.
        """
        cs = self._ctx.commands
        cs.ever_had_waiter = True
        if cs.buf:
            return cs.buf.popleft()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        cs.waiters.append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            if not fut.done():
                fut.cancel()
            return None


class ScriptManager:
    """
    Load, run, and manage async scripts.

    Scripts are Python files on the search path. Each script run gets its own
    :class:`ScriptOutputBuffer` so output matching does not conflict.

    :param scripts_dir: Path to the user global scripts directory.
    :param log: Logger instance.
    """

    def __init__(self, scripts_dir: str = "", log: "logging.Logger | None" = None) -> None:
        """Initialize ScriptManager."""
        self.scripts_dir = scripts_dir
        self._log = log or logging.getLogger(__name__)
        self._tasks: dict[str, asyncio.Task[typing.Any]] = {}
        self._buffers: dict[str, ScriptOutputBuffer] = {}
        self._mtimes: dict[str, float] = {}

    def _load_module(self, module_path: str) -> typing.Any:
        """
        Import (or reload) a module from the scripts search path.

        The scripts directory and cwd are temporarily prepended to ``sys.path``
        during load and removed in a ``finally`` block. If the source file's
        mtime has changed since the last load, ``importlib.reload`` is called.

        :param module_path: Dotted module path (e.g. ``"combat"`` or ``"ai.bot"``).
        :returns: The loaded module object.
        """
        search_dirs = []
        cwd = os.getcwd()
        if cwd not in search_dirs:
            search_dirs.append(cwd)
        if self.scripts_dir and self.scripts_dir != cwd:
            search_dirs.append(self.scripts_dir)

        for d in reversed(search_dirs):
            if d not in sys.path:
                sys.path.insert(0, d)

        try:
            if module_path in sys.modules:
                mod = sys.modules[module_path]
                src_file = getattr(mod, "__file__", None)
                if src_file:
                    try:
                        mtime = os.path.getmtime(src_file)
                    except OSError:
                        mtime = 0.0
                    if mtime != self._mtimes.get(module_path, 0.0):
                        mod = importlib.reload(mod)
                        self._mtimes[module_path] = mtime
            else:
                mod = importlib.import_module(module_path)
                src_file = getattr(mod, "__file__", None)
                if src_file:
                    try:
                        self._mtimes[module_path] = os.path.getmtime(src_file)
                    except OSError:
                        self._mtimes[module_path] = 0.0
        finally:
            for d in search_dirs:
                if d in sys.path:
                    sys.path.remove(d)

        return mod

    def start_script(self, session_ctx: "TelixSessionContext", spec: str) -> "asyncio.Task[typing.Any]":
        """
        Load and start a script.

        *spec* is the module.function token plus optional arguments, e.g.
        ``"combat.hunt goblin"`` or ``"demo"``.

        The last dot-separated segment of the first token is the function name;
        everything before it is the module path. If no dot is present, the
        function name defaults to ``"run"``.

        :param session_ctx: Active session context.
        :param spec: Script spec string (``"module.fn arg1 arg2"``).
        :returns: The running :class:`asyncio.Task`.
        :raises ValueError: If the module or function cannot be found.
        """
        parts = shlex.split(spec)
        if not parts:
            raise ValueError("empty script spec")
        token = parts[0]
        args = parts[1:]

        if "." in token:
            dot = token.rfind(".")
            module_path = token[:dot]
            fn_name = token[dot + 1 :]
        else:
            module_path = token
            fn_name = "run"

        task_key = token

        existing = self._tasks.get(task_key)
        if existing is not None and not existing.done() and not getattr(existing, "_must_cancel", False):
            raise ValueError(f"script {task_key!r} is already running")

        buf = ScriptOutputBuffer()
        script_log = logging.getLogger(f"telix.script.{task_key}")
        ctx = ScriptContext(session_ctx, buf, script_log)

        try:
            mod = self._load_module(module_path)
        except Exception:
            script_log.exception("script %r failed to import", task_key)
            for line in traceback.format_exc().splitlines():
                ctx.print(line)

            async def _noop() -> None:
                pass

            return asyncio.ensure_future(_noop())

        fn = getattr(mod, fn_name, None)
        if fn is None:
            raise ValueError(f"script {module_path!r} has no function {fn_name!r}")

        async def run_script() -> None:
            def on_warning(
                message: typing.Any,
                category: type[Warning],
                filename: str,
                lineno: int,
                file: typing.Any = None,
                line: typing.Any = None,
            ) -> None:
                ctx.print(f"{filename}:{lineno}: {category.__name__}: {message}")

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("always")
                    warnings.showwarning = on_warning
                    await fn(ctx, *args)
            except asyncio.CancelledError:
                script_log.info("script %r cancelled", task_key)
                raise
            except Exception:
                script_log.exception("script %r raised an exception", task_key)
                for line in traceback.format_exc().splitlines():
                    ctx.print(line)

        task = asyncio.ensure_future(run_script())
        self._tasks[task_key] = task
        self._buffers[task_key] = buf

        def on_done(t: asyncio.Task[typing.Any]) -> None:
            if self._tasks.get(task_key) is t:
                self._tasks.pop(task_key, None)
                self._buffers.pop(task_key, None)
            self._log.info("script %r finished", task_key)

        task.add_done_callback(on_done)
        self._log.info("script %r started", task_key)
        return task

    def stop_script(self, name: "str | None" = None) -> list[str]:
        """
        Cancel a running script or all running scripts.

        :param name: Script name to stop, or ``None`` to stop all.
        :returns: Names of scripts that were actually cancelled.
        """
        stopped = []
        if name is None:
            for task_name, task in list(self._tasks.items()):
                if not task.done():
                    task.cancel()
                    stopped.append(task_name)
            self._tasks.clear()
            self._buffers.clear()
        else:
            t = self._tasks.get(name)
            if t is not None and not t.done():
                t.cancel()
                stopped.append(name)
        return stopped

    def feed(self, text: str) -> None:
        """
        Forward server output text to all active script buffers.

        :param text: Server output text.
        """
        for buf in list(self._buffers.values()):
            buf.feed(text)

    def on_prompt(self) -> None:
        """Signal GA/EOR to all active script buffers."""
        for buf in list(self._buffers.values()):
            buf.on_prompt()

    def active_scripts(self) -> list[str]:
        """
        Return names of currently running scripts.

        :returns: List of script name strings.
        """
        return [name for name, task in self._tasks.items() if not task.done()]
