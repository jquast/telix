"""Tests for pure functions in client_repl_render."""

import re

import pytest

from telix import client_repl_render


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (1500, "1.5k"),
        (999999, "1000.0k"),
        (1000000, "1.0m"),
        (2500000, "2.5m"),
    ],
)
def test_fmt_value(n, expected):
    assert client_repl_render.fmt_value(n) == expected


HEX_RE = re.compile(r"^#[0-9a-f]{6}$")


@pytest.mark.parametrize(
    "fraction, kind",
    [
        (0.0, "hp"),
        (0.5, "hp"),
        (1.0, "hp"),
        (0.0, "mp"),
        (0.5, "mp"),
        (1.0, "mp"),
        (0.0, "xp"),
        (1.0, "xp"),
        (0.0, "discover"),
        (1.0, "discover"),
        (0.0, "randomwalk"),
        (1.0, "randomwalk"),
    ],
)
def test_vital_color_format(fraction, kind):
    result = client_repl_render.vital_color(fraction, kind)
    assert HEX_RE.match(result)


def test_vital_color_clamps():
    low = client_repl_render.vital_color(-0.5, "hp")
    zero = client_repl_render.vital_color(0.0, "hp")
    assert low == zero
    high = client_repl_render.vital_color(1.5, "hp")
    one = client_repl_render.vital_color(1.0, "hp")
    assert high == one


@pytest.mark.parametrize(
    "text, avail, expected",
    [
        ("hello", 10, "hello"),
        ("hello", 5, "hello"),
        ("hello world", 6, "hello\u2026"),
        ("anything", 0, ""),
        ("anything", -5, ""),
    ],
)
def test_center_truncate(text, avail, expected):
    assert client_repl_render.center_truncate(text, avail) == expected


def test_segmented_passthrough():
    assert client_repl_render.segmented("12345") == "12345"
    assert client_repl_render.segmented("") == ""


def test_wcswidth_ascii():
    assert client_repl_render.wcswidth("hello") == 5
    assert client_repl_render.wcswidth("") == 0


def test_vital_tracker_first_update():
    """First update returns negative (no flash)."""
    tracker = client_repl_render.VitalTracker()
    elapsed = tracker.update(100, 1000.0)
    assert elapsed < 0


def test_vital_tracker_value_change():
    """Value change triggers flash."""
    tracker = client_repl_render.VitalTracker()
    tracker.update(100, 1000.0)
    elapsed = tracker.update(90, 1000.01)
    assert elapsed >= 0


def test_vital_tracker_same_value():
    """Same value does not trigger new flash."""
    tracker = client_repl_render.VitalTracker()
    tracker.update(100, 1000.0)
    elapsed = tracker.update(100, 1000.01)
    assert elapsed < 0


def test_vital_tracker_invalid_raw():
    tracker = client_repl_render.VitalTracker()
    tracker.update("not_a_number", 1000.0)
    assert tracker.last_value == 0


def test_xp_tracker_eta_insufficient_history():
    tracker = client_repl_render.XPTracker()
    tracker.update(100, 1000.0)
    assert tracker.eta_fragments(500, 1000.0) is None


def test_xp_tracker_eta_with_history():
    tracker = client_repl_render.XPTracker()
    tracker.update(100, 1000.0)
    tracker.update(200, 1010.0)
    result = tracker.eta_fragments(500, 1010.0)
    assert result is not None
    assert len(result) > 0


def test_xp_tracker_history_prune():
    tracker = client_repl_render.XPTracker()
    tracker.update(100, 0.0)
    tracker.update(200, 1.0)
    tracker.update(300, 400.0)
    assert all(t >= 400.0 - tracker.HISTORY_WINDOW for t, _ in tracker.history)


def test_toolbar_slot_defaults():
    slot = client_repl_render.ToolbarSlot(
        priority=1, display_order=1, width=10,
        fragments=[("", "test")], side="left", min_width=5, label="test",
    )
    assert slot.growable is False
    assert slot.grow_params is None


def test_layout_toolbar_empty():
    left, right = client_repl_render.layout_toolbar([], 80)
    assert left == []
    assert right == []


def test_layout_toolbar_single_left():
    slot = client_repl_render.ToolbarSlot(
        priority=1, display_order=1, width=10,
        fragments=[("", "test")], side="left", min_width=0, label="test",
    )
    left, right = client_repl_render.layout_toolbar([slot], 80)
    assert len(left) == 1
    assert right == []


def test_layout_toolbar_slot_too_wide():
    slot = client_repl_render.ToolbarSlot(
        priority=1, display_order=1, width=100,
        fragments=[("", "x" * 100)], side="left", min_width=0, label="x" * 100,
    )
    left, right = client_repl_render.layout_toolbar([slot], 20)
    assert left == []


def test_layout_toolbar_display_order():
    s1 = client_repl_render.ToolbarSlot(
        priority=1, display_order=2, width=5,
        fragments=[("", "a")], side="left", min_width=0, label="a",
    )
    s2 = client_repl_render.ToolbarSlot(
        priority=2, display_order=1, width=5,
        fragments=[("", "b")], side="left", min_width=0, label="b",
    )
    left, _ = client_repl_render.layout_toolbar([s1, s2], 80)
    assert left[0].display_order < left[1].display_order


def test_left_sep_widths_empty():
    assert client_repl_render.left_sep_widths([]) == []


def test_left_sep_widths_single():
    slot = client_repl_render.ToolbarSlot(
        priority=1, display_order=1, width=10,
        fragments=[("", "x")], side="left", min_width=0, label="x",
    )
    assert client_repl_render.left_sep_widths([slot]) == []


def test_left_sep_widths_two_growable():
    slot = client_repl_render.ToolbarSlot(
        priority=1, display_order=1, width=10,
        fragments=[("", "x")], side="left", min_width=0, label="x", growable=True,
    )
    gaps = client_repl_render.left_sep_widths([slot, slot])
    assert gaps == [client_repl_render.BAR_GAP_WIDTH]


def test_left_sep_widths_mixed():
    growable = client_repl_render.ToolbarSlot(
        priority=1, display_order=1, width=10,
        fragments=[("", "x")], side="left", min_width=0, label="x", growable=True,
    )
    fixed = client_repl_render.ToolbarSlot(
        priority=2, display_order=2, width=10,
        fragments=[("", "y")], side="left", min_width=0, label="y", growable=False,
    )
    gaps = client_repl_render.left_sep_widths([growable, fixed])
    assert gaps == [client_repl_render.DISPLAY.SEPARATOR_WIDTH]


def test_fill_toolbar_no_growable():
    slot = client_repl_render.ToolbarSlot(
        priority=1, display_order=1, width=10,
        fragments=[("", "test")], side="left", min_width=0, label="test",
    )
    left, right, sep = client_repl_render.fill_toolbar([slot], [], 80)
    assert left[0].width == 10
    assert sep == client_repl_render.DISPLAY.SEPARATOR_WIDTH


def test_fill_toolbar_distributes_extra():
    slot = client_repl_render.ToolbarSlot(
        priority=1, display_order=1, width=10,
        fragments=[("", "test")], side="left", min_width=0, label="test",
        growable=True, grow_params=(100, 200, "hp", -1.0),
    )
    left, right, sep = client_repl_render.fill_toolbar([slot], [], 80)
    assert left[0].width > 10
