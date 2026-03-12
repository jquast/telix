"""Tests for extracted helpers in :mod:`telix.client_repl_travel`."""

import asyncio
import types

import pytest

from telix import rooms, client_repl_travel


class FakeEngine:
    """Minimal stand-in for TriggerEngine."""

    def __init__(self):
        self.exclusive_active = False
        self.reply_pending = False

    def check_timeout(self):
        pass


@pytest.mark.asyncio()
async def test_settle_triggers_noreply_skips():
    """settle_triggers returns immediately when noreply is True."""
    engine = FakeEngine()
    engine.exclusive_active = True
    engine.reply_pending = True
    called = False

    async def wait_fn():
        nonlocal called
        called = True

    await client_repl_travel.settle_triggers(engine, wait_fn, noreply=True)
    assert not called


@pytest.mark.asyncio()
async def test_settle_triggers_no_pending():
    """settle_triggers returns immediately when nothing is pending."""
    engine = FakeEngine()

    await client_repl_travel.settle_triggers(engine, None, noreply=False)


@pytest.mark.asyncio()
async def test_settle_triggers_waits_for_exclusive():
    """settle_triggers waits until exclusive_active clears."""
    engine = FakeEngine()
    engine.exclusive_active = True

    iterations = 0

    def fake_check_timeout():
        nonlocal iterations
        iterations += 1
        if iterations >= 2:
            engine.exclusive_active = False

    engine.check_timeout = fake_check_timeout

    await client_repl_travel.settle_triggers(engine, None, noreply=False)
    assert iterations >= 2


def test_correct_room_edge_updates_exit(tmp_path):
    """correct_room_edge rewrites the adjacency cache to point at the new room ID."""
    store = rooms.RoomStore(str(tmp_path / "rooms.db"))
    store.update_room({"num": "A", "name": "Start", "exits": {"east": "B"}})
    store.update_room({"num": "B", "name": "Room B", "exits": {}})
    store.update_room({"num": "C", "name": "Room B", "exits": {}})

    client_repl_travel.correct_room_edge(store, "A", "B", "C", "east")

    assert store.adj["A"]["east"] == "C"
    store.close()


def test_correct_room_edge_no_room(tmp_path):
    """correct_room_edge is a no-op when prev room has no adj entry."""
    store = rooms.RoomStore(str(tmp_path / "rooms.db"))
    store.update_room({"num": "B", "name": "Room B", "exits": {}})

    client_repl_travel.correct_room_edge(store, "MISSING", "B", "C", "east")
    assert "MISSING" not in store.adj
    store.close()


def test_repath_finds_new_route(tmp_path):
    """Repath returns a new path when one exists."""
    store = rooms.RoomStore(str(tmp_path / "rooms.db"))
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C"}})
    store.update_room({"num": "C", "exits": {}})

    result = client_repl_travel.repath(store, "C", "A", lambda msg: None)
    assert result == [("east", "B"), ("east", "C")]
    store.close()


def test_repath_no_path(tmp_path):
    """Repath returns an empty list when no path exists."""
    store = rooms.RoomStore(str(tmp_path / "rooms.db"))
    store.update_room({"num": "A", "exits": {}})
    store.update_room({"num": "C", "exits": {}})

    result = client_repl_travel.repath(store, "C", "A", lambda msg: None)
    assert result == []
    store.close()


def test_repath_already_at_destination(tmp_path):
    """Repath returns empty list when already at destination."""
    store = rooms.RoomStore(str(tmp_path / "rooms.db"))
    store.update_room({"num": "A", "exits": {}})

    result = client_repl_travel.repath(store, "A", "A", lambda msg: None)
    assert result == []
    store.close()


def make_travel_ctx(graph=None, current=""):
    """Build a minimal context namespace for fast_travel tests."""
    walk = types.SimpleNamespace(
        active_command=None,
        active_command_time=0.0,
        discover_active=False,
        discover_current=0,
        randomwalk_active=False,
        randomwalk_current=0,
        randomwalk_total=0,
    )
    repaint_calls = []
    prompt = types.SimpleNamespace(
        wait_fn=None,
        echo=None,
        ready=None,
        repaint_input=lambda: repaint_calls.append(1),
    )
    room = types.SimpleNamespace(graph=graph, current=current, changed=asyncio.Event())
    triggers = types.SimpleNamespace(engine=None)
    writer = types.SimpleNamespace(write=lambda s: None)
    ctx = types.SimpleNamespace(prompt=prompt, room=room, walk=walk, triggers=triggers, writer=writer)
    return ctx, repaint_calls


@pytest.mark.asyncio()
async def test_fast_travel_calls_repaint_on_completion(tmp_path):
    """fast_travel calls repaint_input after clearing active_command."""
    import logging

    store = rooms.RoomStore(str(tmp_path / "rooms.db"))
    store.update_room({"num": "A", "name": "Start", "exits": {"east": "B"}})
    store.update_room({"num": "B", "name": "End", "exits": {}})
    ctx, repaint_calls = make_travel_ctx(graph=store, current="A")

    original_write = ctx.writer.write

    def fake_write(s):
        original_write(s)
        ctx.room.current = "B"
        ctx.room.changed.set()

    ctx.writer.write = fake_write

    await client_repl_travel.fast_travel(
        [("east", "B")], ctx, logging.getLogger("test"), destination="B"
    )
    store.close()
    assert repaint_calls
    assert ctx.walk.active_command is None


@pytest.mark.asyncio()
async def test_fast_travel_calls_repaint_on_empty_path():
    """fast_travel calls repaint_input even with an empty step list."""
    import logging

    ctx, repaint_calls = make_travel_ctx()
    await client_repl_travel.fast_travel([], ctx, logging.getLogger("test"))
    assert repaint_calls
    assert ctx.walk.active_command is None
