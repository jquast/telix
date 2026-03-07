"""Tests for extracted helpers in :mod:`telix.client_repl_travel`."""

import asyncio

import pytest

from telix import rooms, client_repl_travel


class FakeEngine:
    """Minimal stand-in for AutoreplyEngine."""

    def __init__(self):
        self.exclusive_active = False
        self.reply_pending = False

    def check_timeout(self):
        pass


@pytest.mark.asyncio()
async def test_settle_autoreplies_noreply_skips():
    """settle_autoreplies returns immediately when noreply is True."""
    engine = FakeEngine()
    engine.exclusive_active = True
    engine.reply_pending = True
    called = False

    async def wait_fn():
        nonlocal called
        called = True

    await client_repl_travel.settle_autoreplies(engine, wait_fn, noreply=True)
    assert not called


@pytest.mark.asyncio()
async def test_settle_autoreplies_no_pending():
    """settle_autoreplies returns immediately when nothing is pending."""
    engine = FakeEngine()

    await client_repl_travel.settle_autoreplies(engine, None, noreply=False)


@pytest.mark.asyncio()
async def test_settle_autoreplies_waits_for_exclusive():
    """settle_autoreplies waits until exclusive_active clears."""
    engine = FakeEngine()
    engine.exclusive_active = True

    iterations = 0

    def fake_check_timeout():
        nonlocal iterations
        iterations += 1
        if iterations >= 2:
            engine.exclusive_active = False

    engine.check_timeout = fake_check_timeout

    await client_repl_travel.settle_autoreplies(engine, None, noreply=False)
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
