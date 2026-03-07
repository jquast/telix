"""Movement and pathfinding: travel, autodiscover, randomwalk."""

# std imports
import time
import random
import asyncio
import logging
import collections
from typing import TYPE_CHECKING, Optional

# 3rd party
import telnetlib3.stream_writer

# local
from .client_repl_commands import TRAVEL_RE, COMMAND_DELAY

if TYPE_CHECKING:
    from .rooms import RoomGraph
    from .autoreply import AutoreplyEngine
    from .session_context import TelixSessionContext

DEFAULT_WALK_LIMIT = 999
STANDARD_DIRS = frozenset(
    {"north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest", "ne", "nw", "se", "sw"}
)
BOUNCE_THRESHOLD = 3
MAX_STUCK_RETRIES = 3
STUCK_RETRY_DELAY = 5.0
# Delay after wait_fn() in settle loops to allow read_server to process
# the prompt text and call on_prompt() before checking autoreply flags.
SETTLE_YIELD_DELAY = 0.05


async def settle_autoreplies(engine, wait_fn, noreply):
    """
    Wait for exclusive autoreplies to finish before moving.

    When *noreply* is ``True`` or *engine* has nothing pending, returns
    immediately.  Otherwise loops until both ``exclusive_active`` and
    ``reply_pending`` clear, waiting for a fresh prompt between iterations.

    :param engine: The autoreply engine (may be ``None``).
    :param wait_fn: Coroutine to wait for a server prompt (may be ``None``).
    :param noreply: Skip waiting entirely when ``True``.
    """
    if noreply or engine is None:
        return
    if not engine.exclusive_active and not engine.reply_pending:
        return

    max_settle = 20
    for _pass in range(max_settle):
        if engine.exclusive_active:
            while engine.exclusive_active:
                engine.check_timeout()
                await asyncio.sleep(0.05)
        while engine.reply_pending:
            await asyncio.sleep(0.05)
        if wait_fn is not None:
            await wait_fn()
        await asyncio.sleep(SETTLE_YIELD_DELAY)
        if not engine.exclusive_active and not engine.reply_pending:
            break


def correct_room_edge(graph, prev_num, old_target, new_target, direction):
    """
    Rewrite a graph exit so *direction* from *prev_num* points at *new_target*.

    Called when a same-name room is reached under a different ID than expected.
    Only updates the exit if it currently points at *old_target*.  Updates
    the in-memory adjacency cache so subsequent pathfinding sees the change.

    :param graph: The room graph (``RoomStore``).
    :param prev_num: Room from which the exit originates.
    :param old_target: Expected target room ID.
    :param new_target: Actual target room ID.
    :param direction: Exit direction label.
    """
    if graph is None:
        return
    adj_exits = graph.adj.get(prev_num)
    if adj_exits is not None and adj_exits.get(direction) == old_target:
        adj_exits[direction] = new_target


def repath(room_graph, destination, current, log_fn):
    """
    Re-pathfind from *current* to *destination* using *room_graph*.

    :param room_graph: The room graph (``RoomStore``).
    :param destination: Target room ID.
    :param current: Current room ID.
    :param log_fn: Callable accepting a status message string.
    :returns: New list of ``(direction, room_id)`` steps, or empty list.
    """
    if room_graph is None or current == destination:
        return []
    blocked = room_graph.blocked_rooms()
    new_steps = room_graph.find_path_with_rooms(current, destination, blocked=blocked)
    if new_steps is not None:
        return new_steps
    return []


async def fast_travel(
    steps: list[tuple[str, str]],
    ctx: "TelixSessionContext",
    log: logging.Logger,
    destination: str = "",
    correct_names: bool = True,
    noreply: bool = False,
) -> None:
    """
    Execute travel by sending movement commands with GA/EOR pacing.

    Uses the same ``wait_for_prompt`` / ``echo_command`` functions that
    the autoreply engine and manual input use, so commands are paced by
    the server's GA/EOR prompt signal and echoed visibly.

    By default all autoreplies fire.  Travel waits for exclusive rules
    (e.g. combat triggers) to finish in each room before moving.
    With ``noreply=True`` the engine is disabled entirely and the settle
    loop naturally does nothing.

    When the player arrives at an unexpected room, instead of aborting
    the function re-pathfinds from the actual position to *destination*
    and continues with the new route (up to 3 re-routes).

    :param steps: List of (direction, expected_room_num) pairs.
    :param ctx: Session context for sending commands.
    :param log: Logger.
    :param destination: Final target room ID for re-pathfinding on detour.
    :param correct_names: If ``True`` (default), rewrite graph edges when
        arriving at a same-name room with a different ID.  Set to ``False``
        when distinct room IDs must be preserved.
    :param noreply: Completely disable the autoreply engine during travel.
    """
    wait_fn = ctx.wait_for_prompt
    echo_fn = ctx.echo_command

    def get_engine() -> Optional["AutoreplyEngine"]:
        """Find the active autoreply engine, if any."""
        return ctx.autoreply_engine

    engine = get_engine()
    engine_was_enabled = True
    if noreply and engine is not None:
        engine_was_enabled = engine.enabled
        engine.enabled = False

    mode = "travel"

    def get_graph() -> "RoomGraph | None":
        graph: RoomGraph | None = ctx.room_graph
        return graph

    def room_name(num: str) -> str:
        """Look up a human-readable room name from the session's graph."""
        graph = get_graph()
        if graph is not None:
            room = graph.rooms.get(num)
            if room is not None:
                return f"{room.name} ({num[:8]}...)"
        return num

    # Track room IDs the graph already knew about before this travel
    # started, so we can distinguish "ID rotation" (new hash for same
    # room) from "different room with the same name" (cave grids).
    pre_existing_rooms: set[str] = set()
    graph = get_graph()
    if graph is not None:
        pre_existing_rooms = set(graph.rooms.keys())

    def names_match(expected_num: str, actual_num: str) -> bool:
        """
        Check whether two room IDs likely refer to the same physical room.

        This handles MUDs that rotate room IDs (same physical room, new hash
        each visit).  Returns ``True`` only when:

        1. Both rooms share the same name.
        2. The *actual* room ID was **not** already in the graph before this
           travel began.  Pre-existing rooms are distinct locations that
           happen to share a name (e.g. a grid of "A cave" rooms).  A
           rotated ID produces a hash the graph has never seen.
        """
        if actual_num in pre_existing_rooms:
            return False
        graph = get_graph()
        if graph is None:
            return False
        expected = graph.rooms.get(expected_num)
        actual = graph.rooms.get(actual_num)
        if expected is None or actual is None:
            return False
        return expected.name == actual.name and bool(expected.name)

    def correct_edge(
        prev_num: str,
        direction: str,
        old_target: str,
        new_target: str,
        step_idx: int,
        steps_list: list[tuple[str, str]],
    ) -> None:
        """
        Update the graph edge and rewrite only the current step.

        Earlier versions rewrote *all* remaining steps matching *old_target*, which corrupted paths through grids of
        same-named rooms (e.g. a cave system where many rooms share the name "A cave" but are distinct locations with
        different IDs).  Now only the step at *step_idx* is updated.
        """
        graph = get_graph()
        correct_room_edge(graph, prev_num, old_target, new_target, direction)
        if graph is not None:
            adj_exits = graph.adj.get(prev_num)
            if adj_exits is not None and adj_exits.get(direction) == new_target:
                log.info(
                    "%s: corrected exit %s of %s: %s -> %s",
                    mode,
                    direction,
                    prev_num[:8],
                    old_target[:8],
                    new_target[:8],
                )
        if step_idx < len(steps_list):
            d, r = steps_list[step_idx]
            if r == old_target:
                steps_list[step_idx] = (d, new_target)

    room_changed = ctx.room_changed
    max_retries = 3
    max_reroutes = 3

    if not destination and steps:
        destination = steps[-1][1]

    blocked_exits: list[tuple[str, str, str]] = []
    try:
        step_idx = 0
        reroute_count = 0
        while step_idx < len(steps):
            direction, expected_room = steps[step_idx]
            prev_room = ctx.current_room_num

            for attempt in range(max_retries + 1):
                # Delay between steps (and retries) for server rate limits.
                if step_idx > 0 or attempt > 0:
                    await asyncio.sleep(COMMAND_DELAY)

                if room_changed is not None:
                    room_changed.clear()

                tag = f" [{step_idx + 1}/{len(steps)}]"
                prefix = ""
                if ctx.discover_active:
                    prefix = f"AUTODISCOVER [{ctx.discover_current}]: "
                elif ctx.randomwalk_active:
                    prefix = f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: "
                if attempt == 0:
                    log.info("%s [%d/%d] %s", mode, step_idx + 1, len(steps), direction)
                    if echo_fn is not None:
                        echo_fn(prefix + direction + tag)
                else:
                    log.info("%s [%d/%d] %s (retry %d)", mode, step_idx + 1, len(steps), direction, attempt)
                # Clear prompt_ready before sending so wait_fn waits
                # for a FRESH GA/EOR from this step's response.  The
                # server sends multiple GA/EORs per response (room
                # prompt + GMCP vitals updates), and stale signals
                # from the previous step cause wait_fn to return
                # before the current room output has been received.
                prompt_ready = ctx.prompt_ready
                if prompt_ready is not None:
                    prompt_ready.clear()

                ctx.active_command = direction
                ctx.active_command_time = time.monotonic()
                if ctx.cx_dot is not None:
                    ctx.cx_dot.trigger()
                if ctx.tx_dot is not None:
                    ctx.tx_dot.trigger()
                ctx.writer.write(direction + "\r\n")

                if wait_fn is not None:
                    await wait_fn()

                # Yield to let read_server feed the room output to the
                # autoreply engine before we check reply_pending.
                await asyncio.sleep(0)

                engine = get_engine()
                cond_cancelled = False
                if engine is not None:
                    while engine.reply_pending:
                        await asyncio.sleep(0.05)
                    failed = engine.pop_condition_failed()
                    if failed is not None:
                        rule_idx, desc = failed
                        msg = f"Travel mode cancelled - failed conditional in AUTOREPLY #{rule_idx} [{desc}]"
                        log.warning("%s", msg)
                        if echo_fn is not None:
                            echo_fn(msg)
                        cond_cancelled = True
                    await settle_autoreplies(engine, wait_fn, noreply=False)
                if cond_cancelled:
                    break

                # GMCP Room.Info may arrive after the EOR.  Wait for it.
                actual = ctx.current_room_num
                if expected_room and actual != expected_room and room_changed is not None:
                    try:
                        await asyncio.wait_for(room_changed.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    actual = ctx.current_room_num

                if actual == expected_room:
                    break
                # Same-name room with different ID -- correct the edge
                # and continue as if we arrived at the expected room.
                # Skipped when correct_names=False to preserve distinct
                # room IDs in grids of same-named rooms.
                if (
                    correct_names
                    and expected_room
                    and actual
                    and actual != expected_room
                    and names_match(expected_room, actual)
                ):
                    log.info(
                        "%s: room ID changed for %s (%s -> %s), correcting",
                        mode,
                        room_name(actual),
                        expected_room[:8],
                        actual[:8],
                    )
                    correct_edge(prev_room, direction, expected_room, actual, step_idx, steps)
                    expected_room = actual
                    break
                # Room didn't change -- server likely rejected move (rate limit).
                # Retry unless we've exhausted attempts.
                if actual == prev_room and attempt < max_retries:
                    continue
                # Arrived at wrong room -- try to re-route.
                break

            if cond_cancelled:
                break
            if expected_room and actual and actual != expected_room:
                move_blocked = actual == prev_room
                if move_blocked:
                    # Exit is impassable (server rejected the move after
                    # all retries).  Temporarily remove it from both the
                    # Room.exits dict and the BFS adjacency cache so
                    # re-routing won't try it again.
                    graph = get_graph()
                    if graph is not None:
                        prev = graph.rooms.get(prev_room)
                        if prev is not None and direction in prev.exits:
                            blocked_exits.append((prev_room, direction, prev.exits[direction]))
                            del prev.exits[direction]
                            adj_exits = graph.adj.get(prev_room)
                            if adj_exits is not None:
                                adj_exits.pop(direction, None)
                            log.info("%s: blocked exit %s of %s (impassable)", mode, direction, prev_room[:8])
                else:
                    # Update graph edge to reflect actual connection.
                    graph = get_graph()
                    if graph is not None:
                        prev = graph.rooms.get(prev_room)
                        if prev is not None:
                            prev.exits[direction] = actual
                            log.info("%s: updated edge %s of %s: -> %s", mode, direction, prev_room[:8], actual[:8])

                if destination and actual and actual != destination and reroute_count < max_reroutes:

                    def log_reroute(msg):
                        log.info("%s", msg)
                        if echo_fn is not None:
                            echo_fn(msg)

                    new_steps = repath(get_graph(), destination, actual, log_reroute)
                    if new_steps:
                        reroute_count += 1
                        log_reroute(f"{mode}: re-routing from {room_name(actual)} ({reroute_count}/{max_reroutes})")
                        steps = new_steps
                        step_idx = 0
                        continue

                expected_name = room_name(expected_room)
                actual_name = room_name(actual)
                msg = f"{mode} stopped: expected {expected_name} after '{direction}', got {actual_name}"
                log.warning("%s", msg)
                if echo_fn is not None:
                    echo_fn(msg)
                break
            step_idx += 1
    finally:
        # Restore temporarily blocked exits so the graph stays accurate
        # for future pathfinding (the block may be transient, e.g. a
        # quest gate that opens later).
        if blocked_exits:
            graph = get_graph()
            if graph is not None:
                for room_num, exit_dir, target in blocked_exits:
                    prev = graph.rooms.get(room_num)
                    if prev is not None and exit_dir not in prev.exits:
                        prev.exits[exit_dir] = target
                    graph.adj.setdefault(room_num, {})[exit_dir] = target
        ctx.active_command = None
        if noreply and engine is not None:
            engine.enabled = engine_was_enabled


async def autodiscover(
    ctx: "TelixSessionContext",
    log: logging.Logger,
    limit: int = DEFAULT_WALK_LIMIT,
    resume: bool = False,
    strategy: str = "bfs",
    noreply: bool = False,
    auto_search: bool = False,
    auto_evaluate: bool = False,
    auto_survey: bool = False,
) -> None:
    """
    Explore unvisited exits reachable from the current room.

    BFS-discovers frontier exits (leading to unvisited or unknown rooms),
    travels to each, then returns to the starting room before trying the
    next.  Maintains an in-memory ``tried`` set to avoid retrying exits
    that failed or led to unexpected rooms.  Stops after *limit* exits
    or when no more branches remain.

    :param ctx: Session context with room graph and session attributes.
    :param log: Logger.
    :param limit: Maximum number of exits to explore.
    :param strategy: ``"bfs"`` for nearest-first, ``"dfs"`` for
        deepest-first ordering.
    :param noreply: Completely disable the autoreply engine during the walk.
    :param auto_search: Send ``search`` in each newly discovered room.
    :param auto_evaluate: Enable consider-before-kill autoreply logic.
    :param auto_survey: Send ``survey`` in each newly discovered room.
    """
    if ctx.discover_active:
        return

    current = ctx.current_room_num
    graph = ctx.room_graph
    echo_fn = ctx.echo_command
    wait_fn = ctx.wait_for_prompt
    if not current or graph is None:
        if echo_fn is not None:
            echo_fn("AUTODISCOVER: no room data")
        return

    tried: set[tuple[str, str]] = set(ctx.blocked_exits)
    if resume and ctx.last_walk_mode == "autodiscover" and ctx.last_walk_tried:
        tried |= ctx.last_walk_tried
    inaccessible: set[str] = set()
    blocked_edges: dict[tuple[str, str], str] = {}
    blocked_rooms = graph.blocked_rooms()

    branches = graph.find_branches(current, blocked=blocked_rooms, strategy=strategy)
    if not branches:
        if echo_fn is not None:
            echo_fn("AUTODISCOVER: no unvisited exits nearby")
        return

    engine = ctx.autoreply_engine
    engine_was_enabled = True
    if noreply and engine is not None:
        engine_was_enabled = engine.enabled
        engine.enabled = False

    prev_auto_evaluate = ctx.randomwalk_auto_evaluate
    if auto_evaluate:
        ctx.randomwalk_auto_evaluate = True

    ctx.discover_active = True
    ctx.discover_total = len(branches)
    ctx.discover_current = 0
    step_count = 0
    last_stuck_room = ""
    stuck_retries = 0
    try:
        while step_count < limit:
            pos = ctx.current_room_num
            # Re-discover from current position each iteration -- picks up
            # newly revealed exits from rooms we just visited, nearest-first.
            branches = [
                (gw, d, t)
                for gw, d, t in graph.find_branches(pos, blocked=blocked_rooms, strategy=strategy)
                if (gw, d) not in tried and t not in inaccessible
            ]
            if not branches:
                break

            ctx.discover_total = step_count + len(branches)
            gw_room, direction, target_num = branches[0]
            step_count += 1
            ctx.discover_current = step_count

            # Travel to the gateway room (nearest-first, so usually short).
            if pos != gw_room:
                steps = graph.find_path_with_rooms(pos, gw_room, blocked=blocked_rooms)
                if steps is None:
                    tried.add((gw_room, direction))
                    if target_num:
                        inaccessible.add(target_num)
                    if echo_fn is not None:
                        echo_fn(f"AUTODISCOVER [{step_count}]: no path to gateway {gw_room[:8]}")
                    continue
                if echo_fn is not None:
                    echo_fn(f"AUTODISCOVER [{step_count}]: heading to gateway {gw_room[:8]}")
                pre_travel = ctx.current_room_num
                await fast_travel(steps, ctx, log, destination=gw_room)
                actual = ctx.current_room_num
                if actual != gw_room:
                    tried.add((gw_room, direction))
                    if target_num:
                        inaccessible.add(target_num)
                    # Identify the edge that blocked us: if the player
                    # didn't move at all, the first step of the path is
                    # impassable.  Remove it from the BFS adjacency
                    # cache so subsequent pathfinding avoids it.
                    if actual == pre_travel and steps:
                        fail_dir, fail_target = steps[0]
                        edge = (pre_travel, fail_dir)
                        if edge not in blocked_edges:
                            blocked_edges[edge] = fail_target
                            adj_exits = graph.adj.get(pre_travel)
                            if adj_exits is not None:
                                adj_exits.pop(fail_dir, None)
                            log.info("AUTODISCOVER: blocked edge %s from %s", fail_dir, pre_travel[:8])
                    log.info("AUTODISCOVER: failed to reach gateway %s", gw_room[:8])
                    if echo_fn is not None:
                        echo_fn(f"AUTODISCOVER [{step_count}]: gateway {gw_room[:8]} inaccessible, skipping")
                    if actual == last_stuck_room:
                        stuck_retries += 1
                    else:
                        last_stuck_room = actual
                        stuck_retries = 1
                    if stuck_retries >= 3:
                        if echo_fn is not None:
                            echo_fn(f"AUTODISCOVER [{step_count}]: stuck at {actual[:8]}, all routes blocked, stopping")
                        break
                    continue

            # Step through the frontier exit.
            if echo_fn is not None:
                echo_fn(f"AUTODISCOVER [{step_count}]: exploring {direction} from {gw_room[:8]}")
            await asyncio.sleep(COMMAND_DELAY)
            ctx.active_command = direction
            ctx.active_command_time = time.monotonic()
            send = ctx.send_line
            if ctx.cx_dot is not None:
                ctx.cx_dot.trigger()
            if ctx.tx_dot is not None:
                ctx.tx_dot.trigger()
            if send is not None:
                send(direction)
            elif isinstance(ctx.writer, telnetlib3.stream_writer.TelnetWriterUnicode):
                ctx.writer.write(direction + "\r\n")
            else:
                ctx.writer.write((direction + "\r\n").encode("utf-8"))
            # Wait for room arrival using the event instead of polling.
            room_changed = ctx.room_changed
            arrived = False
            if room_changed is not None:
                room_changed.clear()
                try:
                    await asyncio.wait_for(room_changed.wait(), timeout=ctx.room_arrival_timeout)
                except asyncio.TimeoutError:
                    pass
                arrived = ctx.current_room_num != gw_room
            else:
                for wait in range(30):
                    await asyncio.sleep(0.3)
                    if ctx.current_room_num != gw_room:
                        arrived = True
                        break
            if not arrived:
                ctx.active_command = None
                tried.add((gw_room, direction))
                ctx.blocked_exits.add((gw_room, direction))
                if target_num:
                    inaccessible.add(target_num)
                if echo_fn is not None:
                    echo_fn(f"AUTODISCOVER [{step_count}]: no room change after {direction}")
                continue
            ctx.active_command = None

            tried.add((gw_room, direction))
            actual = ctx.current_room_num
            if target_num and actual != target_num and target_num in graph.rooms:
                if echo_fn is not None:
                    echo_fn(f"AUTODISCOVER [{step_count}]: unexpected room {actual[:8]} (expected {target_num[:8]})")

            # Wait for any autoreply to settle.
            ar = ctx.autoreply_engine
            ar_fired = ar is not None and (ar.exclusive_active or ar.reply_pending)
            if ar is not None:
                settle = 0
                while settle < 60:
                    if ar.exclusive_active or ar.reply_pending:
                        ar_fired = True
                    if ar.exclusive_active:
                        while ar.exclusive_active:
                            ar.check_timeout()
                            await asyncio.sleep(0.1)
                    while ar.reply_pending:
                        await asyncio.sleep(0.05)
                    await asyncio.sleep(0.1)
                    if not ar.exclusive_active and not ar.reply_pending:
                        break
                    settle += 1

            if ar_fired and wait_fn is not None:
                await wait_fn()

            if auto_search:
                if echo_fn is not None:
                    echo_fn("search")
                ctx.active_command = "search"
                ctx.active_command_time = time.monotonic()
                if ctx.tx_dot is not None:
                    ctx.tx_dot.trigger()
                if isinstance(ctx.writer, telnetlib3.stream_writer.TelnetWriterUnicode):
                    ctx.writer.write("search\r\n")
                else:
                    ctx.writer.write(b"search\r\n")
                if wait_fn is not None:
                    await wait_fn()
                ctx.active_command = None

            if auto_survey:
                if echo_fn is not None:
                    echo_fn("survey")
                ctx.active_command = "survey"
                ctx.active_command_time = time.monotonic()
                if ctx.tx_dot is not None:
                    ctx.tx_dot.trigger()
                if isinstance(ctx.writer, telnetlib3.stream_writer.TelnetWriterUnicode):
                    ctx.writer.write("survey\r\n")
                else:
                    ctx.writer.write(b"survey\r\n")
                if wait_fn is not None:
                    await wait_fn()
                ctx.active_command = None

            # Stay where we are -- next iteration re-discovers branches
            # from current position, so nearby clusters get swept without
            # backtracking.
    except asyncio.CancelledError:
        pass
    finally:
        if noreply and engine is not None:
            engine.enabled = engine_was_enabled
        ctx.randomwalk_auto_evaluate = prev_auto_evaluate
        ctx.last_walk_mode = "autodiscover"
        ctx.last_walk_room = ctx.current_room_num
        ctx.last_walk_strategy = strategy
        ctx.last_walk_noreply = noreply
        ctx.last_walk_tried = tried
        ctx.discover_active = False
        ctx.discover_current = 0
        ctx.discover_total = 0
        ctx.discover_task = None
        ctx.active_command = None
        # Restore blocked edges so the graph stays accurate for future
        # pathfinding (the block may be transient, e.g. a level gate).
        for (room_num, exit_dir), target in blocked_edges.items():
            graph.adj.setdefault(room_num, {})[exit_dir] = target


async def randomwalk(
    ctx: "TelixSessionContext",
    log: logging.Logger,
    limit: int = DEFAULT_WALK_LIMIT,
    resume: bool = False,
    visit_level: int = 2,
    noreply: bool = False,
) -> None:
    """
    Random walk up to *limit* rooms, preferring unvisited exits.

    At each room the walker picks a random exit from those with the
    lowest walk visit count.  A per-walk ``walk_counts`` dict tracks
    how many times we have arrived at each room during this walk.  The
    room the player was in *before* triggering the walk (the
    "entrance") is seeded with an infinite count so it is never
    chosen -- the walker will never leave through the direction it
    came from.

    Stops early when every reachable room (excluding the entrance)
    has been visited at least *visit_level* times.

    :param ctx: Session context with room graph and session attributes.
    :param log: Logger.
    :param limit: Maximum number of steps.
    :param visit_level: Minimum visits per reachable room before stopping.
    :param noreply: Completely disable the autoreply engine during the walk.
    """
    if ctx.randomwalk_active:
        return

    current = ctx.current_room_num
    graph = ctx.room_graph
    echo_fn = ctx.echo_command
    wait_fn = ctx.wait_for_prompt
    if not current or graph is None:
        if echo_fn is not None:
            echo_fn("RANDOMWALK: no room data")
        return

    adj = graph.adj
    exits = adj.get(current, {})
    if not exits:
        if echo_fn is not None:
            echo_fn("RANDOMWALK: no exits from current room")
        return

    engine = ctx.autoreply_engine
    engine_was_enabled = True
    if noreply and engine is not None:
        engine_was_enabled = engine.enabled
        engine.enabled = False

    # Per-walk visit counter.  The entrance room (the room we were in
    # before triggering the walk) is seeded at infinity so the walker
    # never prefers going back through it.
    entrance_room = ctx.previous_room_num
    walk_counts: dict[str, float] = {current: 1}
    if entrance_room:
        walk_counts[entrance_room] = float("inf")

    # blocked_exits is consulted per-room at scoring time rather than
    # seeding walk_counts globally -- a blocked exit (A, east) should
    # only penalize that specific exit, not the destination room from
    # every other direction.
    # Clear stale blocked exits from previous walks so dead-end rooms
    # with a single exit aren't permanently stuck.  Resume keeps them.
    if not resume:
        ctx.blocked_exits.clear()
    db_blocked = graph.blocked_rooms()

    def flood_reachable() -> set[str]:
        """BFS flood from current room, excluding entrance and blocked rooms."""
        result: set[str] = set()
        q: collections.deque[str] = collections.deque([current])
        seen: set[str] = {current}
        if entrance_room:
            seen.add(entrance_room)
        seen |= db_blocked
        while q:
            node = q.popleft()
            for dst in adj.get(node, {}).values():
                if dst not in seen:
                    seen.add(dst)
                    result.add(dst)
                    q.append(dst)
        return result

    reachable = flood_reachable()

    ctx.randomwalk_active = True
    expected_total = visit_level * len(reachable) if reachable else limit
    ctx.randomwalk_total = min(limit, expected_total)
    ctx.randomwalk_current = 0
    visited: set[str] = {current}
    if resume and ctx.last_walk_mode == "randomwalk" and ctx.last_walk_visited:
        visited |= ctx.last_walk_visited

    def count_filled() -> int:
        """Sum visits across reachable rooms, capped at visit_level per room."""
        if not reachable:
            return sum(min(int(v), visit_level) for v in walk_counts.values() if v != float("inf"))
        return sum(min(int(walk_counts.get(r, 0)), visit_level) for r in reachable)

    try:
        stuck_count = 0
        retry_count = 0
        bounce_count = 0
        prev_room: str | None = None
        for step in range(limit):
            current = ctx.current_room_num
            exits = dict(adj.get(current, {}))
            if not exits:
                if echo_fn is not None:
                    echo_fn(f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: dead end, stopping")
                break

            # Check if all reachable rooms have been visited enough times.
            if reachable and all(walk_counts.get(r, 0) >= visit_level for r in reachable):
                if echo_fn is not None:
                    echo_fn(
                        f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: "
                        f"all {len(reachable)} reachable rooms visited"
                        f" {visit_level}x"
                    )
                break

            # Score each exit by walk visit count (lower is better).
            # Skip exits known to be blocked from this room.
            # Non-cardinal directions get a 0.1 penalty so they are
            # tried after cardinal exits at the same visit count.
            scored: list[tuple[float, str, str]] = []
            for d, dst in exits.items():
                if (current, d) in ctx.blocked_exits:
                    continue
                if dst in db_blocked:
                    continue
                penalty = 0.0 if d in STANDARD_DIRS else 0.1
                scored.append((walk_counts.get(dst, 0) + penalty, d, dst))

            if not scored:
                if echo_fn is not None:
                    echo_fn(
                        f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: all exits blocked, stopping"
                    )
                break

            min_count = min(s[0] for s in scored)
            best = [(d, dst) for cnt, d, dst in scored if cnt == min_count]
            direction, dst_num = random.choice(best)

            room = graph.get_room(dst_num)
            dst_label = room.name if room else dst_num[:8]
            if echo_fn is not None:
                echo_fn(f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: {direction} -> {dst_label}")

            ctx.active_command = direction
            ctx.active_command_time = time.monotonic()
            if wait_fn is not None:
                await wait_fn()
            if ctx.cx_dot is not None:
                ctx.cx_dot.trigger()
            if ctx.tx_dot is not None:
                ctx.tx_dot.trigger()
            if isinstance(ctx.writer, telnetlib3.stream_writer.TelnetWriterUnicode):
                ctx.writer.write(direction + "\r\n")
            else:
                ctx.writer.write((direction + "\r\n").encode("utf-8"))

            # Wait for room change using event instead of polling.
            room_changed = ctx.room_changed
            arrived = False
            if room_changed is not None:
                room_changed.clear()
                try:
                    await asyncio.wait_for(room_changed.wait(), timeout=ctx.room_arrival_timeout)
                except asyncio.TimeoutError:
                    pass
                arrived = ctx.current_room_num != current
            else:
                for tick in range(30):
                    await asyncio.sleep(0.3)
                    if ctx.current_room_num != current:
                        arrived = True
                        break
            if not arrived:
                ctx.active_command = None
                stuck_count += 1
                if echo_fn is not None:
                    echo_fn(
                        f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: "
                        f"no room change after {direction}"
                    )
                # Mark only this specific exit as blocked.
                ctx.blocked_exits.add((current, direction))
                # Check if ALL exits from current room are now blocked.
                all_blocked = all((current, d) in ctx.blocked_exits for d in adj.get(current, {}))
                if all_blocked:
                    retry_count += 1
                    if retry_count > MAX_STUCK_RETRIES:
                        if echo_fn is not None:
                            echo_fn(
                                f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: "
                                f"all exits blocked, stopping"
                            )
                        break
                    for d in list(adj.get(current, {})):
                        ctx.blocked_exits.discard((current, d))
                    if echo_fn is not None:
                        echo_fn(
                            f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: "
                            f"all exits temporarily blocked, retrying "
                            f"({retry_count}/{MAX_STUCK_RETRIES})"
                        )
                    await asyncio.sleep(STUCK_RETRY_DELAY)
                    continue
                continue

            ctx.active_command = None
            stuck_count = 0
            retry_count = 0
            actual = ctx.current_room_num
            walk_counts[actual] = walk_counts.get(actual, 0) + 1
            visited.add(actual)
            ctx.randomwalk_current = count_filled()

            if ctx.randomwalk_auto_search:
                if echo_fn is not None:
                    echo_fn("search")
                ctx.active_command = "search"
                ctx.active_command_time = time.monotonic()
                if ctx.tx_dot is not None:
                    ctx.tx_dot.trigger()
                if isinstance(ctx.writer, telnetlib3.stream_writer.TelnetWriterUnicode):
                    ctx.writer.write("search\r\n")
                else:
                    ctx.writer.write(b"search\r\n")
                if wait_fn is not None:
                    await wait_fn()
                ctx.active_command = None

            if ctx.randomwalk_auto_survey:
                if echo_fn is not None:
                    echo_fn("survey")
                ctx.active_command = "survey"
                ctx.active_command_time = time.monotonic()
                if ctx.tx_dot is not None:
                    ctx.tx_dot.trigger()
                if isinstance(ctx.writer, telnetlib3.stream_writer.TelnetWriterUnicode):
                    ctx.writer.write("survey\r\n")
                else:
                    ctx.writer.write(b"survey\r\n")
                if wait_fn is not None:
                    await wait_fn()
                ctx.active_command = None

            # Bounce detection: if we returned to the room we were in
            # 2 steps ago, we are ping-ponging between two rooms.
            # Only block the direction if the intermediate room
            # (``current``) is a dead-end corridor -- i.e. it has no
            # unblocked exits other than the one leading back here.
            if prev_room is not None and actual == prev_room:
                bounce_count += 1
                if bounce_count >= BOUNCE_THRESHOLD:
                    other_exits = [
                        d
                        for d, dst in adj.get(current, {}).items()
                        if dst != actual and (current, d) not in ctx.blocked_exits
                    ]
                    if not other_exits:
                        ctx.blocked_exits.add((current, direction))
                        for rev_d, rev_dst in adj.get(actual, {}).items():
                            if rev_dst == current:
                                ctx.blocked_exits.add((actual, rev_d))
                        if echo_fn is not None:
                            echo_fn(
                                f"RANDOMWALK [{ctx.randomwalk_current}/{ctx.randomwalk_total}]: "
                                f"bounce detected on {direction}, blocking"
                            )
                        all_blocked = all((actual, d) in ctx.blocked_exits for d in adj.get(actual, {}))
                        if all_blocked:
                            if echo_fn is not None:
                                step = ctx.randomwalk_current
                                total = ctx.randomwalk_total
                                echo_fn(f"RANDOMWALK [{step}/{total}]: all exits blocked after bounce, stopping")
                            break
                    bounce_count = 0
            else:
                bounce_count = 0
            prev_room = current

            await asyncio.sleep(COMMAND_DELAY)

            # Re-flood: the room graph's adjacency is updated live by
            # GMCP Room.Info, so newly discovered exits expand the
            # reachable set dynamically.
            new_reachable = flood_reachable()
            if len(new_reachable) > len(reachable):
                reachable = new_reachable
                expected_total = visit_level * len(reachable)
                ctx.randomwalk_total = min(limit, expected_total)

            # Yield so on_prompt() (driven by GA/EOR already received
            # with the room output) can queue autoreplies.
            await asyncio.sleep(0)

            # Wait for autoreplies to settle.  Mirrors the slow-travel
            # settle loop: after exclusive/reply_pending clear, wait for
            # a fresh prompt so the server response to the last autoreply
            # command is processed -- it may trigger cascading matches.
            ar = ctx.autoreply_engine
            ar_fired = False
            if ar is not None:
                settle = 0
                max_settle = 60
                while settle < max_settle:
                    if ar.exclusive_active or ar.reply_pending:
                        ar_fired = True
                    if ar.exclusive_active:
                        while ar.exclusive_active:
                            ar.check_timeout()
                            await asyncio.sleep(0.1)
                    while ar.reply_pending:
                        await asyncio.sleep(0.05)
                    if ar_fired and wait_fn is not None:
                        await wait_fn()
                    # Allow enough time for read_server to process the
                    # prompt text and call on_prompt() / match_rules(),
                    # which may set exclusive_active for a cascading match
                    # (e.g. a second rule matching on the response to the
                    # first rule's last command).
                    await asyncio.sleep(SETTLE_YIELD_DELAY)
                    if not ar.exclusive_active and not ar.reply_pending:
                        break
                    settle += 1
    except asyncio.CancelledError:
        pass
    finally:
        if noreply and engine is not None:
            engine.enabled = engine_was_enabled
        ctx.last_walk_mode = "randomwalk"
        ctx.last_walk_room = ctx.current_room_num
        ctx.last_walk_noreply = noreply
        ctx.last_walk_visited = visited
        ctx.randomwalk_active = False
        ctx.randomwalk_auto_search = False
        ctx.randomwalk_auto_evaluate = False
        ctx.randomwalk_auto_survey = False
        ctx.randomwalk_current = 0
        ctx.randomwalk_total = 0
        ctx.randomwalk_task = None
        ctx.active_command = None


async def handle_travel_commands(parts: list[str], ctx: "TelixSessionContext", log: logging.Logger) -> list[str]:
    """
    Scan *parts* for travel commands, execute them, and return remaining parts.

    Recognised commands (case-insensitive, enclosed in backticks):

    - ```travel <id>``` -- travel to room *id*
    - ```travel <id> noreply``` -- travel with autoreplies disabled
    - ```return``` -- travel back to the macro's starting room
    - ```return noreply``` -- return with autoreplies disabled
    - ```autodiscover``` -- explore unvisited exits from nearby rooms
    - ```randomwalk``` -- random walk preferring unvisited rooms

    Only the **first** travel command in the list is handled; everything
    before it is returned as-is (already sent by the caller), and everything
    after it is returned for the caller to send as chained commands once
    travel finishes.

    :param parts: Expanded command list from :func:`expand_commands`.
    :param ctx: Session context with room graph attributes.
    :param log: Logger.
    :returns: Commands that still need to be sent to the server.
    """
    for idx, cmd in enumerate(parts):
        m = TRAVEL_RE.match(cmd)
        if not m:
            continue
        verb = m.group(1).lower()
        arg = m.group(2).strip()

        if verb == "home":
            echo_fn = ctx.echo_command
            current = ctx.current_room_num
            graph = ctx.room_graph
            if not current or graph is None:
                if echo_fn is not None:
                    echo_fn("HOME: no room data")
                return parts[idx + 1 :]
            area = graph.room_area(current)
            if not area:
                if echo_fn is not None:
                    echo_fn("HOME: current room has no area")
                return parts[idx + 1 :]
            home_num = graph.get_home_for_area(area)
            if home_num is None:
                if echo_fn is not None:
                    echo_fn(f"HOME: no home set in area '{area}'")
                return parts[idx + 1 :]
            if home_num == current:
                if echo_fn is not None:
                    echo_fn("HOME: already at home room")
                return parts[idx + 1 :]
            blocked = graph.blocked_rooms()
            path = graph.find_path_with_rooms(current, home_num, blocked=blocked)
            if path is None:
                if echo_fn is not None:
                    echo_fn(f"HOME: no path to home room {home_num}")
                return parts[idx + 1 :]
            await fast_travel(path, ctx, log, destination=home_num)
            return parts[idx + 1 :]

        if verb in ("autodiscover", "randomwalk", "resume"):
            walk_limit = DEFAULT_WALK_LIMIT
            walk_visit_level = 2
            auto_search = False
            auto_evaluate = False
            auto_survey = False
            walk_strategy = "bfs"
            noreply = False
            if arg:
                arg_parts = arg.split()
                numeric_idx = 0
                for ap in arg_parts:
                    low = ap.lower()
                    if low == "autosearch":
                        auto_search = True
                    elif low == "autoevaluate":
                        auto_evaluate = True
                    elif low == "autosurvey":
                        auto_survey = True
                    elif low == "noreply":
                        noreply = True
                    elif low in ("bfs", "dfs"):
                        walk_strategy = low
                    elif numeric_idx == 0:
                        try:
                            walk_limit = int(ap)
                            numeric_idx += 1
                        except ValueError:
                            pass
                    elif numeric_idx == 1:
                        try:
                            walk_visit_level = max(1, int(ap))
                            numeric_idx += 1
                        except ValueError:
                            pass

            echo_fn = ctx.echo_command
            if verb == "resume":
                if not ctx.last_walk_mode:
                    if echo_fn is not None:
                        echo_fn("RESUME: no previous walk to resume")
                    return parts[idx + 1 :]
                if ctx.last_walk_room != ctx.current_room_num:
                    if echo_fn is not None:
                        echo_fn("RESUME: room changed since last walk, cannot resume")
                    return parts[idx + 1 :]
                verb = ctx.last_walk_mode
                noreply = noreply or ctx.last_walk_noreply
                do_resume = True
            else:
                # Auto-resume: if re-running the same mode from the
                # same room, carry over visited/tried state.
                do_resume = ctx.last_walk_mode == verb and ctx.last_walk_room == ctx.current_room_num

            if verb == "autodiscover":
                await autodiscover(
                    ctx,
                    log,
                    limit=walk_limit,
                    resume=do_resume,
                    strategy=walk_strategy,
                    noreply=noreply,
                    auto_search=auto_search,
                    auto_evaluate=auto_evaluate,
                    auto_survey=auto_survey,
                )

            else:
                ctx.randomwalk_auto_search = auto_search
                ctx.randomwalk_auto_evaluate = auto_evaluate
                ctx.randomwalk_auto_survey = auto_survey
                await randomwalk(
                    ctx, log, limit=walk_limit, resume=do_resume, visit_level=walk_visit_level, noreply=noreply
                )
            return parts[idx + 1 :]

        is_return = verb == "return"
        noreply = False
        if arg:
            arg_parts = arg.split()
            remaining: list[str] = []
            for ap in arg_parts:
                if ap.lower() == "noreply":
                    noreply = True
                else:
                    remaining.append(ap)
            arg = " ".join(remaining)

        if is_return:
            room_id = ctx.macro_start_room or ctx.current_room_num
        else:
            room_id = arg

        if not room_id:
            log.warning("travel command with no room id: %r", cmd)
            break

        current = ctx.current_room_num
        if not current:
            log.warning("no current room -- cannot travel")
            break

        graph = ctx.room_graph
        if graph is None:
            log.warning("no room graph -- cannot travel")
            break

        blocked = graph.blocked_rooms()
        path = graph.find_path_with_rooms(current, room_id, blocked=blocked)
        if path is None:
            log.warning("no path from %s to %s", current, room_id)
            break

        await fast_travel(path, ctx, log, destination=room_id, noreply=noreply)
        return parts[idx + 1 :]

    return parts
