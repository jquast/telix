"""Tests for :mod:`telix.rooms` room graph, pathfinding, and persistence."""

from __future__ import annotations

# std imports
import os
from typing import Any

# 3rd party
import pytest

# local
from telix.rooms import (
    RoomGraph,
    RoomStore,
    room_id,
    load_prefs,
    prefs_path,
    rooms_path,
    save_prefs,
    xdg_data_dir,
    fasttravel_path,
    read_fasttravel,
    strip_exit_dirs,
    write_fasttravel,
    current_room_path,
    read_current_room,
    session_file_path,
    write_current_room,
)


@pytest.fixture()
def store(tmp_path: Any) -> RoomStore:
    """Return a fresh RoomStore backed by a temporary database."""
    db = str(tmp_path / "rooms.db")
    s = RoomStore(db)
    yield s
    s.close()


@pytest.mark.parametrize(
    "info, expected",
    [
        ({"num": "42"}, "42"),
        ({"vnum": 100}, "100"),
        ({"id": "abc"}, "abc"),
        ({"pk": "LPK"}, "LPK"),
        ({"num": "1", "vnum": "2"}, "1"),
        ({"name": "no id"}, None),
        ({}, None),
    ],
)
def test_room_id(info, expected) -> None:
    assert room_id(info) == expected


def build_linear(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C", "west": "A"}})
    store.update_room({"num": "C", "exits": {"west": "B"}})


def build_graph(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B", "north": "X"}})
    store.update_room({"num": "B", "exits": {"east": "C", "west": "A"}})
    store.update_room({"num": "C", "exits": {"west": "B"}})


def test_update_room_new(store: RoomStore) -> None:
    store.update_room(
        {
            "num": "100",
            "name": "Town Square",
            "area": "midgaard",
            "environment": "outdoors",
            "exits": {"north": "101", "south": "102"},
        }
    )
    r = store.get_room("100")
    assert r is not None
    assert r.name == "Town Square"
    assert r.area == "midgaard"
    assert r.environment == "outdoors"
    assert r.exits == {"north": "101", "south": "102"}
    assert r.visit_count == 1
    assert r.last_visited


def test_update_room_existing(store: RoomStore) -> None:
    store.update_room({"num": "100", "name": "Town Square", "area": "midgaard", "exits": {"north": "101"}})
    store.update_room(
        {"num": "100", "name": "Town Square (rebuilt)", "area": "midgaard", "exits": {"north": "101", "east": "103"}}
    )
    r = store.get_room("100")
    assert r is not None
    assert r.name == "Town Square (rebuilt)"
    assert r.exits == {"north": "101", "east": "103"}
    assert r.visit_count == 2


def test_update_room_numeric_id(store: RoomStore) -> None:
    store.update_room({"num": 42, "name": "Numeric Room"})
    assert store.get_room("42") is not None


@pytest.mark.parametrize("key, value", [("num", "100"), ("vnum", "200"), ("id", "300"), ("pk", "LPK")])
def test_update_room_id_key_fallbacks(store: RoomStore, key, value) -> None:
    store.update_room({key: value, "name": "Test Room"})
    r = store.get_room(value)
    assert r is not None
    assert r.name == "Test Room"


def test_update_room_missing_optional_fields(store: RoomStore) -> None:
    store.update_room({"num": "1"})
    r = store.get_room("1")
    assert r is not None
    assert not r.name
    assert not r.area
    assert r.exits == {}


def test_update_room_invalid_exits_ignored(store: RoomStore) -> None:
    store.update_room({"num": "1", "exits": "not-a-dict"})
    r = store.get_room("1")
    assert r is not None
    assert r.exits == {}


def test_rooms_property(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "A"})
    store.update_room({"num": "2", "name": "B"})
    all_rooms = store.rooms
    assert len(all_rooms) == 2
    assert "1" in all_rooms
    assert "2" in all_rooms


def test_get_room_missing(store: RoomStore) -> None:
    assert store.get_room("999") is None


def test_close(tmp_path: Any) -> None:
    db = str(tmp_path / "close_test.db")
    s = RoomStore(db)
    s.update_room({"num": "1", "name": "Test"})
    s.close()
    s2 = RoomStore(db, read_only=True)
    assert s2.get_room("1") is not None
    s2.close()


def test_direct_neighbor(store: RoomStore) -> None:
    build_linear(store)
    assert store.find_path("A", "B") == ["east"]


def test_multi_hop(store: RoomStore) -> None:
    build_linear(store)
    assert store.find_path("A", "C") == ["east", "east"]


def test_reverse_path(store: RoomStore) -> None:
    build_linear(store)
    assert store.find_path("C", "A") == ["west", "west"]


def test_same_room(store: RoomStore) -> None:
    build_linear(store)
    assert store.find_path("A", "A") == []


def test_no_path(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {}})
    store.update_room({"num": "C", "exits": {"west": "B"}})
    assert store.find_path("A", "C") is None


def test_find_path_unknown_src(store: RoomStore) -> None:
    assert store.find_path("X", "Y") is None


def test_one_way_exits(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"down": "B"}})
    store.update_room({"num": "B", "exits": {}})
    assert store.find_path("A", "B") == ["down"]
    assert store.find_path("B", "A") is None


def test_cycle_handling(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C"}})
    store.update_room({"num": "C", "exits": {"east": "A"}})
    assert store.find_path("A", "C") == ["east", "east"]


def test_target_not_in_graph_but_reachable(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    assert store.find_path("A", "B") == ["east"]


def test_find_path_with_rooms(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"north": "C"}})
    store.update_room({"num": "C", "exits": {}})
    result = store.find_path_with_rooms("A", "C")
    assert result == [("east", "B"), ("north", "C")]


def test_find_path_with_rooms_same(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {}})
    assert store.find_path_with_rooms("A", "A") == []


def test_bfs_distances(store: RoomStore) -> None:
    build_linear(store)
    d = store.bfs_distances("A")
    assert d == {"A": 0, "B": 1, "C": 2}


def test_bfs_distances_unreachable(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {}})
    store.update_room({"num": "B", "exits": {}})
    d = store.bfs_distances("A")
    assert d == {"A": 0}


def test_bfs_distances_unknown_src(store: RoomStore) -> None:
    assert not store.bfs_distances("X")


def test_basic_same_name(store: RoomStore) -> None:
    store.conn.execute(
        "INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("1", "A dusty road", "2024-01-01")
    )
    store.conn.execute(
        "INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("2", "A dusty road", "2024-01-03")
    )
    store.conn.execute(
        "INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("3", "A dusty road", "2024-01-02")
    )
    store.conn.execute(
        "INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("4", "Town Square", "2024-01-01")
    )
    store.conn.commit()
    result = store.find_same_name("1")
    assert len(result) == 2
    assert result[0].num == "3"
    assert result[1].num == "2"


def test_excludes_self(store: RoomStore) -> None:
    store.conn.execute("INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("1", "Forest", "2024-01-01"))
    store.conn.execute("INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("2", "Forest", "2024-01-02"))
    store.conn.commit()
    result = store.find_same_name("1")
    assert all(r.num != "1" for r in result)


def test_missing_room(store: RoomStore) -> None:
    assert store.find_same_name("999") == []


def test_empty_name(store: RoomStore) -> None:
    store.conn.execute("INSERT INTO room (num, name) VALUES (?, ?)", ("1", ""))
    store.conn.execute("INSERT INTO room (num, name) VALUES (?, ?)", ("2", ""))
    store.conn.commit()
    assert store.find_same_name("1") == []


def test_no_matches(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "Unique Room"})
    store.update_room({"num": "2", "name": "Different Room"})
    assert store.find_same_name("1") == []


def test_never_visited_sort_first(store: RoomStore) -> None:
    store.conn.execute("INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("1", "Road", "2024-01-01"))
    store.conn.execute("INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("2", "Road", "2024-06-01"))
    store.conn.execute("INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("3", "Road", ""))
    store.conn.commit()
    result = store.find_same_name("1")
    assert result[0].num == "3"
    assert result[1].num == "2"


def test_limit(store: RoomStore) -> None:
    store.conn.execute("INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("0", "Road", "2024-01-01"))
    for i in range(1, 30):
        store.conn.execute(
            "INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", (str(i), "Road", f"2024-01-{i:02d}")
        )
    store.conn.commit()
    result = store.find_same_name("0", limit=5)
    assert len(result) == 5


def test_default_limit_99(store: RoomStore) -> None:
    store.conn.execute("INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", ("0", "Road", "2024-01-01"))
    for i in range(1, 120):
        store.conn.execute(
            "INSERT INTO room (num, name, last_visited) VALUES (?, ?, ?)", (str(i), "Road", f"2024-01-{i % 28 + 1:02d}")
        )
    store.conn.commit()
    result = store.find_same_name("0")
    assert len(result) == 99


@pytest.mark.parametrize(
    "method,attr", [("toggle_bookmark", "bookmarked"), ("toggle_blocked", "blocked"), ("toggle_marked", "marked")]
)
def test_toggle_marker(store: RoomStore, method, attr) -> None:
    store.update_room({"num": "1", "name": "Room"})
    assert not getattr(store.get_room("1"), attr)
    assert getattr(store, method)("1") is True
    assert getattr(store.get_room("1"), attr)
    assert getattr(store, method)("1") is False
    assert not getattr(store.get_room("1"), attr)


@pytest.mark.parametrize("method", ["toggle_bookmark", "toggle_blocked", "toggle_marked"])
def test_toggle_marker_missing(store: RoomStore, method) -> None:
    assert getattr(store, method)("999") is False


@pytest.mark.parametrize(
    "rooms,query,expected_count",
    [
        ([("1", "Dark Forest", "wild"), ("2", "Town Square", "town"), ("3", "Forest Path", "wild")], "forest", 2),
        ([("1", "Room A", "caladan"), ("2", "Room B", "arrakis")], "caladan", 1),
        ([("1", "DARK FOREST", "")], "dark", 1),
        ([("1", "Room A", ""), ("2", "Room B", "")], "", 2),
    ],
)
def test_search(store: RoomStore, rooms, query, expected_count) -> None:
    for num, name, area in rooms:
        store.update_room({"num": num, "name": name, "area": area})
    assert len(store.search(query)) == expected_count


def test_search_bookmarked_first(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "Alpha Room"})
    store.update_room({"num": "2", "name": "Beta Room"})
    store.toggle_bookmark("2")
    results = store.search("room")
    assert results[0].num == "2"
    assert results[1].num == "1"


def test_alias_is_room_store() -> None:
    assert RoomGraph is RoomStore


def test_rooms_path_format() -> None:
    p = rooms_path("example.com:4000")
    basename = os.path.basename(p)
    assert basename.startswith("rooms-")
    assert basename.endswith(".db")
    assert "telix" in p


def test_current_room_path_format() -> None:
    p = current_room_path("host:23")
    assert os.path.basename(p).startswith(".current-room-")


def test_fasttravel_path_format() -> None:
    p = fasttravel_path("host:23")
    assert os.path.basename(p).startswith(".fasttravel-")


@pytest.mark.parametrize(
    "malicious_key", ["../../etc/passwd:80", "../../../tmp/evil:23", "/absolute/path:99", "..%2f..%2fetc/shadow:22"]
)
def test_session_file_path_traversal(malicious_key: str) -> None:
    result = session_file_path("rooms-", malicious_key)
    assert ".." not in result
    assert os.path.dirname(result) == xdg_data_dir()


def test_current_room_file_write_read_roundtrip(tmp_path: Any) -> None:
    path = str(tmp_path / ".current-room")
    write_current_room(path, "abc123")
    assert read_current_room(path) == "abc123"


def test_current_room_file_read_missing_file(tmp_path: Any) -> None:
    path = str(tmp_path / "nonexistent")
    assert not read_current_room(path)


def test_fasttravel_file_write_read_roundtrip(tmp_path: Any) -> None:
    path = str(tmp_path / ".fasttravel")
    steps = [("north", "101"), ("east", "102")]
    write_fasttravel(path, steps)
    result_steps, result_noreply = read_fasttravel(path)
    assert result_steps == steps
    assert result_noreply is False
    assert not os.path.exists(path)


def test_write_read_noreply_mode(tmp_path: Any) -> None:
    path = str(tmp_path / ".fasttravel")
    steps = [("north", "101")]
    write_fasttravel(path, steps, noreply=True)
    result_steps, result_noreply = read_fasttravel(path)
    assert result_steps == steps
    assert result_noreply is True


def test_fasttravel_file_read_missing_file(tmp_path: Any) -> None:
    path = str(tmp_path / "nonexistent")
    assert read_fasttravel(path) == ([], False)


def test_finds_frontier_exit(store: RoomStore) -> None:
    build_graph(store)
    branches = store.find_branches("A")
    dirs = [(gw, d) for gw, d, _ in branches]
    assert ("A", "north") in dirs


def test_unknown_target_is_frontier(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    branches = store.find_branches("A")
    assert len(branches) == 1
    assert branches[0] == ("A", "east", "B")


def test_unvisited_target_is_frontier(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.conn.execute("INSERT INTO room (num, name, visit_count) VALUES (?, ?, ?)", ("B", "Empty", 0))
    store.conn.commit()
    branches = store.find_branches("A")
    assert len(branches) == 1
    assert branches[0][2] == "B"


def test_visited_target_not_frontier(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"west": "A"}})
    branches = store.find_branches("A")
    assert len(branches) == 0


def test_sorted_by_distance(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C", "north": "Y"}})
    store.update_room({"num": "C", "exits": {"north": "Z"}})
    branches = store.find_branches("A")
    gateways = [gw for gw, _, _ in branches]
    assert gateways.index("B") < gateways.index("C")


def test_empty_graph(store: RoomStore) -> None:
    assert store.find_branches("A") == []


def test_find_branches_unknown_src(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    assert store.find_branches("Z") == []


def test_find_branches_limit(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"n": "X1", "s": "X2", "e": "X3"}})
    branches = store.find_branches("A", limit=2)
    assert len(branches) == 2


def test_no_duplicates(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C", "west": "A"}})
    branches = store.find_branches("A")
    pairs = [(gw, d) for gw, d, _ in branches]
    assert len(pairs) == len(set(pairs))


def test_prefs_path_format() -> None:
    p = prefs_path("example.com:4000")
    basename = os.path.basename(p)
    assert basename.startswith("prefs-")
    assert basename.endswith(".json")
    assert "telix" in p


def test_save_load_roundtrip(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr("telix.rooms.xdg_data_dir", lambda: str(tmp_path))
    prefs = {"skip_randomwalk_confirm": True, "skip_autodiscover_confirm": False}
    save_prefs("host:1234", prefs)
    loaded = load_prefs("host:1234")
    assert loaded["skip_randomwalk_confirm"] is True
    assert loaded["skip_autodiscover_confirm"] is False


def test_load_missing_file() -> None:
    result = load_prefs("nonexistent:9999")
    assert result == {}


def test_save_overwrites(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr("telix.rooms.xdg_data_dir", lambda: str(tmp_path))
    save_prefs("h:1", {"skip_randomwalk_confirm": False})
    save_prefs("h:1", {"skip_randomwalk_confirm": True})
    loaded = load_prefs("h:1")
    assert loaded["skip_randomwalk_confirm"] is True


def test_prefs_string_value(tmp_path: Any, monkeypatch: Any) -> None:
    """String preference values round-trip correctly."""
    monkeypatch.setattr("telix.rooms.xdg_data_dir", lambda: str(tmp_path))
    save_prefs("h:1", {"skip_randomwalk_confirm": True, "tui_theme": "nord"})
    loaded = load_prefs("h:1")
    assert loaded["skip_randomwalk_confirm"] is True
    assert loaded["tui_theme"] == "nord"


def test_find_branches_shuffles_equal_distance(tmp_path: Any) -> None:
    db_path = str(tmp_path / "rooms.db")
    store = RoomStore(db_path)
    store.update_room({"num": "A", "name": "A", "exits": {"east": "X1", "west": "X2", "north": "X3", "south": "X4"}})

    orders: set[tuple[str, ...]] = set()
    for _ in range(50):
        branches = store.find_branches("A")
        dirs = tuple(d for _, d, _ in branches)
        orders.add(dirs)

    assert len(orders) > 1
    store.close()


def test_room_summaries_includes_last_visited(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "Room A", "area": "zone", "exits": {"n": "2"}})
    store.update_room({"num": "2", "name": "Room B", "area": "zone", "exits": {}})
    summaries = store.room_summaries()
    assert len(summaries) == 2
    for s in summaries:
        assert isinstance(s[5], str)
        assert s[5] != ""
    by_num = {s[0]: s for s in summaries}
    assert by_num["1"][3] == 1
    assert by_num["2"][3] == 0


@pytest.mark.parametrize(
    "name,expected",
    [
        ("A Large Ridge. [n,s,w,e,nw,ne,sw,se]", "A Large Ridge."),
        ("A Large Ridge. [n,e,ne]", "A Large Ridge."),
        ("A Large Ridge. [s,w,sw]", "A Large Ridge."),
        ("A Large Ridge", "A Large Ridge"),
        ("A Small Ridge {SPICE} [n,s,w,e,nw,ne,sw,se]", "A Small Ridge"),
        ("Rocky Ridge.   [n,w,e]", "Rocky Ridge."),
        ("Rocky Ridge.   [rocks,s,w]", "Rocky Ridge."),
        ("A Musty Passage", "A Musty Passage"),
        ("A Large Ridge. [w,e]", "A Large Ridge."),
        ("", ""),
    ],
)
def test_strip_exit_dirs(name: str, expected: str) -> None:
    assert strip_exit_dirs(name) == expected


def test_update_room_strips_exit_dirs(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "A Large Ridge. [n,s,w,e]", "area": "arrakis"})
    r = store.get_room("1")
    assert r is not None
    assert r.name == "A Large Ridge."


def test_room_summaries_names_stripped(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "Ridge. [n,s]", "area": "zone"})
    store.update_room({"num": "2", "name": "Ridge. [w,e]", "area": "zone"})
    summaries = store.room_summaries()
    names = {s[0]: s[1] for s in summaries}
    assert names["1"] == "Ridge."
    assert names["2"] == "Ridge."


def test_toggle_home_one_per_area(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "A", "area": "town"})
    store.update_room({"num": "2", "name": "B", "area": "town"})
    store.update_room({"num": "3", "name": "C", "area": "wild"})
    assert store.toggle_home("1") is True
    assert store.get_room("1").home  # type: ignore[union-attr]
    assert store.toggle_home("2") is True
    assert store.get_room("2").home  # type: ignore[union-attr]
    assert not store.get_room("1").home  # type: ignore[union-attr]
    assert store.get_home_for_area("town") == "2"
    assert store.get_home_for_area("wild") is None
    assert store.toggle_home("3") is True
    assert store.get_home_for_area("wild") == "3"
    assert store.toggle_home("2") is False
    assert store.get_home_for_area("town") is None


def test_markers_are_exclusive(store: RoomStore) -> None:
    """Setting one marker clears all others on the same room."""
    store.update_room({"num": "1", "name": "Room"})
    store.toggle_bookmark("1")
    r = store.get_room("1")
    assert r is not None
    assert r.bookmarked and not r.blocked and not r.home and not r.marked

    store.toggle_blocked("1")
    r = store.get_room("1")
    assert r is not None
    assert r.blocked and not r.bookmarked and not r.home and not r.marked

    store.toggle_home("1")
    r = store.get_room("1")
    assert r is not None
    assert r.home and not r.bookmarked and not r.blocked and not r.marked

    store.toggle_marked("1")
    r = store.get_room("1")
    assert r is not None
    assert r.marked and not r.bookmarked and not r.blocked and not r.home

    store.toggle_marked("1")
    r = store.get_room("1")
    assert r is not None
    assert not r.marked and not r.bookmarked and not r.blocked and not r.home


def test_blocked_rooms_set(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "A"})
    store.update_room({"num": "2", "name": "B"})
    store.update_room({"num": "3", "name": "C"})
    store.toggle_blocked("2")
    store.toggle_blocked("3")
    assert store.blocked_rooms() == frozenset({"2", "3"})


def test_bfs_skips_blocked_rooms(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C", "west": "A"}})
    store.update_room({"num": "C", "exits": {"west": "B"}})
    d = store.bfs_distances("A", blocked=frozenset({"B"}))
    assert "B" not in d
    assert "C" not in d
    assert d == {"A": 0}


def test_find_path_skips_blocked(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B", "north": "D"}})
    store.update_room({"num": "B", "exits": {"east": "C"}})
    store.update_room({"num": "C", "exits": {}})
    store.update_room({"num": "D", "exits": {"east": "C"}})
    path = store.find_path("A", "C", blocked=frozenset({"B"}))
    assert path == ["north", "east"]


def test_find_path_with_rooms_skips_blocked(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B", "north": "D"}})
    store.update_room({"num": "B", "exits": {"east": "C"}})
    store.update_room({"num": "C", "exits": {}})
    store.update_room({"num": "D", "exits": {"east": "C"}})
    path = store.find_path_with_rooms("A", "C", blocked=frozenset({"B"}))
    assert path == [("north", "D"), ("east", "C")]


def test_find_branches_skips_blocked(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C", "west": "A"}})
    branches = store.find_branches("A", blocked=frozenset({"B"}))
    assert not any(t == "B" for _, _, t in branches)


def test_find_branches_dfs_deepest_first(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C", "north": "Y"}})
    store.update_room({"num": "C", "exits": {"north": "Z"}})
    branches = store.find_branches("A", strategy="dfs")
    gateways = [gw for gw, _, _ in branches]
    assert gateways.index("C") < gateways.index("B")


def test_find_branches_bfs_nearest_first(store: RoomStore) -> None:
    store.update_room({"num": "A", "exits": {"east": "B"}})
    store.update_room({"num": "B", "exits": {"east": "C", "north": "Y"}})
    store.update_room({"num": "C", "exits": {"north": "Z"}})
    branches = store.find_branches("A", strategy="bfs")
    gateways = [gw for gw, _, _ in branches]
    assert gateways.index("B") < gateways.index("C")


def test_room_summaries_includes_new_fields(store: RoomStore) -> None:
    store.update_room({"num": "1", "name": "Room", "area": "zone"})
    store.toggle_marked("1")
    summaries = store.room_summaries()
    assert len(summaries) == 1
    s = summaries[0]
    assert len(s) == 9
    assert s[6] is False
    assert s[7] is False
    assert s[8] is True
