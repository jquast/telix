"""Tests for telix.client_tui_rooms pure data logic."""

from __future__ import annotations

import pytest

from telix import client_tui_rooms


class TestPriority:
    """RoomBrowserPane.priority sort ordering."""

    def test_home_sorts_first(self):
        home_room = ("1", "Home", "area", 3, False, "", False, True, False)
        plain_room = ("2", "Plain", "area", 2, False, "", False, False, False)
        assert client_tui_rooms.RoomBrowserPane.priority(home_room) < (
            client_tui_rooms.RoomBrowserPane.priority(plain_room)
        )

    def test_blocked_sorts_second(self):
        blocked = ("1", "Blocked", "area", 2, False, "", True, False, False)
        plain = ("2", "Plain", "area", 2, False, "", False, False, False)
        assert client_tui_rooms.RoomBrowserPane.priority(blocked) < (
            client_tui_rooms.RoomBrowserPane.priority(plain)
        )

    def test_bookmarked_sorts_third(self):
        bookmarked = ("1", "BM", "area", 2, True, "", False, False, False)
        plain = ("2", "Plain", "area", 2, False, "", False, False, False)
        assert client_tui_rooms.RoomBrowserPane.priority(bookmarked) < (
            client_tui_rooms.RoomBrowserPane.priority(plain)
        )

    def test_marked_sorts_fourth(self):
        marked = ("1", "Marked", "area", 2, False, "", False, False, True)
        plain = ("2", "Plain", "area", 2, False, "", False, False, False)
        assert client_tui_rooms.RoomBrowserPane.priority(marked) < (
            client_tui_rooms.RoomBrowserPane.priority(plain)
        )

    def test_plain_sorts_last(self):
        plain = ("1", "Plain", "area", 2, False, "", False, False, False)
        pri = client_tui_rooms.RoomBrowserPane.priority(plain)
        assert pri == (True, True, True, True)


class TestFitName:
    """RoomBrowserPane.fit_name truncation and padding."""

    def test_short_name_padded(self):
        result = client_tui_rooms.RoomBrowserPane.fit_name("hello", 10)
        assert result == "hello     "
        assert len(result) == 10

    def test_exact_width(self):
        result = client_tui_rooms.RoomBrowserPane.fit_name("exact", 5)
        assert result == "exact"

    def test_long_name_truncated(self):
        result = client_tui_rooms.RoomBrowserPane.fit_name("a very long room name", 10)
        assert len(result) == 10
        assert result.endswith("\u2026")


class TestShortId:
    """RoomBrowserPane.short_id truncation."""

    def test_short_unchanged(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.id_width = 20
        assert pane.short_id("abc123") == "abc123"

    def test_long_truncated(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.id_width = 8
        result = pane.short_id("abcdefghijklmnop")
        assert len(result) == 8
        assert result.endswith("\u2026")


class TestHeadingText:
    """RoomBrowserPane.heading_text column labels."""

    def test_name_sort_shows_dist(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.sort_mode = "name"
        assert "Dist" in pane.heading_text()

    def test_last_visited_shows_last(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.sort_mode = "last_visited"
        assert "[Last]" in pane.heading_text()

    def test_distance_sort_shows_dist(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.sort_mode = "distance"
        assert "Dist" in pane.heading_text()


class TestMaxAreaLen:
    """RoomBrowserPane.max_area_len from loaded rooms."""

    def test_returns_longest_area(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.all_rooms = [
            ("1", "Room A", "Short", 2, False, "", False, False, False),
            ("2", "Room B", "A Much Longer Area", 3, False, "", False, False, False),
            ("3", "Room C", "Mid", 1, False, "", False, False, False),
        ]
        assert pane.max_area_len() == len("A Much Longer Area")

    def test_empty_rooms(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.all_rooms = []
        assert pane.max_area_len() == 0


class TestEstimateButtonColWidth:
    """RoomBrowserPane.estimate_button_col_width bounds."""

    def test_small_areas(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.all_rooms = [("1", "Room", "Abc", 1, False, "", False, False, False)]
        result = pane.estimate_button_col_width()
        assert result >= client_tui_rooms.BUTTON_COL_MIN

    def test_large_areas(self):
        pane = client_tui_rooms.RoomBrowserPane(rooms_path="unused")
        pane.all_rooms = [("1", "Room", "A" * 100, 1, False, "", False, False, False)]
        result = pane.estimate_button_col_width()
        assert result <= client_tui_rooms.BUTTON_COL_MIN + client_tui_rooms.BUTTON_COL_GROW
