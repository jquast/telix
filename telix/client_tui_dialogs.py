"""
Dialogs, room browser, captures viewer, tabbed editor, and main app.

Contains the room browser (tree, panes, picker), captures/chat viewer,
tabbed editor screen combining all panes, confirmation and walk dialogs,
and the top-level ``TelnetSessionApp`` / ``tui_main``.

Imports from ``client_tui_base`` and ``client_tui_editors``.
"""

# std imports
import os
import sys
import json
import typing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rooms import RoomStore

# 3rd party
import wcwidth
import rich.text
import rich.style
import textual.app
import textual.events
import textual.screen
import textual.binding
import textual.widgets
import textual.css.query
import textual.containers
import textual.widgets.tree

# local
import telix.rooms
from . import client_tui_base, client_tui_editors

# ---------------------------------------------------------------------------
# Room browser constants
# ---------------------------------------------------------------------------

NAME_COL_BASE = 17
ID_COL_BASE = 10
BUTTON_COL_MIN = 20
BUTTON_COL_GROW = 15

# Colors for room tree decorations -- use Textual CSS variable names.
BOOKMARK_STYLE = "$accent"
ARROW_STYLE = "$primary"
HOME_STYLE = "$accent"
BLOCKED_STYLE = "$error"
MARKED_STYLE = "$accent"


# ---------------------------------------------------------------------------
# Room tree widget
# ---------------------------------------------------------------------------


class RoomTree(textual.widgets.Tree[str]):
    """Room tree with aligned icon+arrow prefix columns."""

    ICON_NODE = "\u25c2 "  # ◂
    ICON_NODE_EXPANDED = "\u25be "  # ▾

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self.bookmarked: set[str] = set()
        self.blocked: set[str] = set()
        self.home: set[str] = set()
        self.marked: set[str] = set()

    def render_label(
        self, node: textual.widgets.tree.TreeNode[str], base_style: rich.style.Style, style: rich.style.Style
    ) -> rich.text.Text:
        """Render label with fixed icon+arrow prefix columns."""
        room_num = node.data
        is_child = node.parent is not None and node.parent.parent is not None

        # Icon column (2 chars): priority -- home > blocked > marked > bookmark
        if room_num and room_num in self.home:
            icon = rich.text.Text("\u2302 ", style=HOME_STYLE)
        elif room_num and room_num in self.blocked:
            icon = rich.text.Text("\u2300 ", style=BLOCKED_STYLE)
        elif room_num and room_num in self.marked:
            icon = rich.text.Text("\u27bd ", style=MARKED_STYLE)
        elif room_num and room_num in self.bookmarked:
            icon = rich.text.Text("\u2021 ", style=BOOKMARK_STYLE)
        else:
            icon = rich.text.Text("  ")

        # Arrow column (2 chars: arrow + space) -- only for expandable nodes
        if node.allow_expand:
            arrow_char = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            arrow = rich.text.Text(arrow_char, style=ARROW_STYLE)
        elif not is_child:
            arrow = rich.text.Text("  ")
        else:
            arrow = rich.text.Text("")

        node_label = node.label.copy()
        node_label.stylize(style)

        text = rich.text.Text.assemble(icon, arrow, node_label)
        return text


# ---------------------------------------------------------------------------
# Room browser pane
# ---------------------------------------------------------------------------


class RoomBrowserPane(textual.containers.Vertical):
    """Pane widget for GMCP room graph browsing."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("escape", "close", "Close", priority=True),
        textual.binding.Binding("f1", "show_help", "Help", show=True),
        textual.binding.Binding("enter", "fast_travel", "Travel", show=True),
        textual.binding.Binding("asterisk", "toggle_bookmark", "Bookmark", key_display="*", show=True, priority=True),
        textual.binding.Binding("b", "toggle_block", "Block", show=True),
        textual.binding.Binding("h", "toggle_home", "Home", show=True),
        textual.binding.Binding("m", "toggle_mark", "Mark", show=True),
        textual.binding.Binding("n", "sort_name", "Name sort", show=True),
        textual.binding.Binding("i", "sort_id", "ID sort", show=True),
        textual.binding.Binding("d", "sort_distance", "Dist sort", show=True),
        textual.binding.Binding("l", "sort_last", "Recent", show=True),
    ]

    DEFAULT_CSS = """
    RoomBrowserPane {
        width: 100%; height: 100%;
    }
    #room-panel {
        width: 100%; height: 100%;
        border: round $surface-lighten-2; background: $surface; padding: 1 1;
    }
    #room-search { height: auto; }
    #room-body { height: 1fr; }
    #room-button-col {
        width: 20; height: auto; padding-right: 1;
    }
    #room-button-col Button {
        width: 100%; min-width: 0; margin-bottom: 0;
    }
    #room-area-frame {
        width: 100%; height: auto; margin-top: 0; margin-bottom: 0;
        border: round $surface-lighten-2; padding: 0 0;
    }
    #room-area-frame Static { height: 1; }
    #room-area-select { width: 100%; }
    #room-right { width: 1fr; height: 100%; }
    #room-tree { height: 1fr; min-height: 4; overflow-x: hidden; }
    #room-tree > .tree--guides { color: $primary; }
    #room-tree > .tree--guides-hover { color: $primary; }
    #room-tree > .tree--guides-selected { color: $primary; }
    #room-heading { height: 1; text-style: bold; }
    #room-status { height: 1; margin-top: 0; }
    #room-count { width: auto; }
    #room-exits { height: 1; width: 100%; }
    #room-distance { width: 1fr; text-align: right; }
    #room-marker-bar { height: auto; }
    #room-marker-bar Button { width: 13; min-width: 0; margin-right: 1; }
    #room-total { height: 1; margin-top: 0; }
    Footer FooterLabel { margin: 0; }
    """

    def __init__(
        self, rooms_path: str, session_key: str = "", current_room_file: str = "", fasttravel_file: str = ""
    ) -> None:
        """Initialize room browser."""
        super().__init__()
        self.rooms_path = rooms_path
        self.session_key = session_key
        self.current_room_file = current_room_file
        self.fasttravel_file = fasttravel_file
        self.all_rooms: list[tuple[str, str, str, int, bool, str, bool, bool, bool]] = []
        self.current_area: str = ""
        self.graph: RoomStore | None = None
        self.mounted = False
        self.sort_mode: str = "name"
        self.distances: dict[str, int] = {}
        self.last_visited: dict[str, str] = {}
        self.name_col: int = NAME_COL_BASE
        self.id_width: int = ID_COL_BASE + 20
        self.muted_style: str = "dim"
        self.cursor_just_moved: bool = False

    def heading_text(self) -> str:
        """Return the column heading string for the tree."""
        if self.sort_mode == "last_visited":
            info_col = "[Last]".rjust(8)
        else:
            info_col = "Dist".rjust(5)
        nc = self.name_col
        return f"    {'Name'.ljust(nc)} {'(N)'.rjust(6)} {info_col} #ID"

    def compose(self) -> textual.app.ComposeResult:
        """Build the room browser layout."""
        with textual.containers.Vertical(id="room-panel"), textual.containers.Horizontal(id="room-body"):
            with textual.containers.Vertical(id="room-button-col"):
                travel_btn = textual.widgets.Button("Travel", variant="success", id="room-travel")
                travel_btn.tooltip = "Travel to the selected room"
                yield travel_btn
                yield textual.widgets.Button("Help", variant="success", id="room-help")
                yield textual.widgets.Button("Close", id="room-close")
                with textual.containers.Vertical(id="room-area-frame"):
                    yield textual.widgets.Static("Area:")
                    yield textual.widgets.Select[str]([], id="room-area-select", allow_blank=True, prompt="All Areas")
                    yield textual.widgets.Static("", id="room-total")
            with textual.containers.Vertical(id="room-right"):
                yield textual.widgets.Input(placeholder="Search rooms\u2026", id="room-search")
                yield textual.widgets.Static(self.heading_text(), id="room-heading")
                yield RoomTree("Rooms", id="room-tree")
                with textual.containers.Horizontal(id="room-status"):
                    yield textual.widgets.Static("", id="room-count")
                    yield textual.widgets.Static("", id="room-distance")
                yield textual.widgets.Static("", id="room-exits")
                with textual.containers.Horizontal(id="room-marker-bar"):
                    yield textual.widgets.Button("Bookmark \u2021", variant="warning", id="room-bookmark")
                    yield textual.widgets.Button("Block \u2300", variant="error", id="room-block")
                    yield textual.widgets.Button("Home \u2302", variant="primary", id="room-home")
                    yield textual.widgets.Button("Mark \u27bd", variant="default", id="room-mark")

    def request_close(self, result: bool | None = None) -> None:
        """Dismiss the parent screen or exit the app."""
        try:
            self.screen.dismiss(result)
        except textual.app.ScreenStackError:
            self.app.exit()

    def max_area_len(self) -> int:
        """Return the length of the longest area name in loaded rooms."""
        best = 0
        for _, _, area, _, _, _, _, _, _ in self.all_rooms:
            best = max(best, len(area))
        return best

    def estimate_button_col_width(self) -> int:
        """Return the computed button column width (mirrors on_mount sizing)."""
        need = self.max_area_len() + 4
        return min(BUTTON_COL_MIN + BUTTON_COL_GROW, max(BUTTON_COL_MIN, need))

    def reflow_columns(self) -> None:
        """Recompute column widths for the current terminal size."""
        col_w = self.estimate_button_col_width()
        self.query_one("#room-button-col").styles.width = col_w
        term_w = self.app.size.width
        # Panel chrome: border (2) + padding (2) + button-col padding-right (1).
        # Row fixed parts: icon (2) + arrow (2) + gaps/count/dist (14) + " #" (2).
        self.id_width = ID_COL_BASE + 5
        chrome = 5
        row_fixed = 19 + self.id_width
        available = term_w - col_w - chrome - row_fixed
        self.name_col = max(NAME_COL_BASE, available)
        try:
            self.query_one("#room-heading", textual.widgets.Static).update(self.heading_text())
        except textual.css.query.NoMatches:
            pass

    def on_mount(self) -> None:
        """Load rooms from file and populate tree."""
        css_vars = self.app.get_css_variables()
        fg_muted = css_vars.get("foreground-muted", "")
        if fg_muted:
            self.muted_style = fg_muted[:7] if len(fg_muted) >= 7 else fg_muted
        tree = self.query_one("#room-tree", textual.widgets.Tree)
        tree.show_root = False
        tree.guide_depth = 3
        self.load_rooms()
        self.reflow_columns()
        self.compute_distances()
        self.populate_area_dropdown()
        self.sort_rooms()
        self.refresh_tree()
        self.mounted = True
        self.call_after_refresh(self.select_current_room)

    def on_resize(self, event: textual.events.Resize) -> None:
        """Reflow columns and rebuild the tree on terminal resize."""
        if not self.mounted:
            return
        self.reflow_columns()
        search_val = self.query_one("#room-search", textual.widgets.Input).value
        self.refresh_tree(search_val)

    def select_room_node(self, room_num: str) -> bool:
        """
        Select the tree node matching *room_num*.

        Expands parent groups as needed and forces a tree rebuild so that line numbers are valid before scrolling.

        Return True on success.
        """
        tree = self.query_one("#room-tree", textual.widgets.Tree)
        target = None
        for node in tree.root.children:
            if node.data == room_num:
                target = node
                break
            for child in node.children:
                if child.data == room_num:
                    node.expand()
                    target = child
                    break
            if target is not None:
                break
        if target is None:
            return False
        # Force tree line rebuild so node.line values are current.
        _ = tree._tree_lines
        # Suppress on_tree_node_selected travel triggered by programmatic selection.
        self.cursor_just_moved = True
        tree.select_node(target)
        tree.focus()
        return True

    def select_current_room(self) -> None:
        """Move cursor to the current room node, if known."""
        if not self.current_room_file:
            return
        current = telix.rooms.read_current_room(self.current_room_file)
        if not current:
            return
        self.select_room_node(current)

    def load_rooms(self) -> None:
        """Load room data from SQLite database."""
        graph = telix.rooms.RoomStore(self.rooms_path, read_only=True)
        self.graph = graph
        self.all_rooms = graph.room_summaries()
        self.last_visited = {num: lv for num, _, _, _, _, lv, _, _, _ in self.all_rooms if lv}
        if self.current_room_file:
            current = telix.rooms.read_current_room(self.current_room_file)
            if current:
                self.current_area = graph.room_area(current)

    def populate_area_dropdown(self) -> None:
        """Populate the area dropdown from loaded rooms."""
        areas: set[str] = set()
        for _, _, area, _, _, _, _, _, _ in self.all_rooms:
            if area:
                areas.add(area)
        sorted_areas = sorted(areas, key=str.lower)
        options = [(a, a) for a in sorted_areas]
        select = self.query_one("#room-area-select", textual.widgets.Select)
        select.set_options(options)
        if self.current_area and self.current_area in areas:
            select.value = self.current_area

    def compute_distances(self) -> None:
        """Compute BFS distances from the current room."""
        self.distances = {}
        if not self.current_room_file or self.graph is None:
            return
        current = telix.rooms.read_current_room(self.current_room_file)
        if current:
            self.distances = self.graph.bfs_distances(current)

    @staticmethod
    def priority(r: tuple[str, str, str, int, bool, str, bool, bool, bool]) -> tuple[bool, bool, bool, bool]:
        """Return sort priority: home, blocked, bookmarked, marked first."""
        # r[7]=home, r[6]=blocked, r[4]=bookmarked, r[8]=marked
        return (not r[7], not r[6], not r[4], not r[8])

    def sort_rooms(self) -> None:
        """Sort ``all_rooms`` according to ``sort_mode``."""
        if self.sort_mode == "distance":
            self.all_rooms.sort(
                key=lambda r: (*self.priority(r), self.distances.get(r[0], float("inf")), r[2].lower(), r[1].lower())
            )
        elif self.sort_mode == "last_visited":
            self.all_rooms.sort(key=lambda r: (*self.priority(r), client_tui_editors.invert_ts(r[5]), r[1].lower()))
        elif self.sort_mode == "id":
            self.all_rooms.sort(key=lambda r: (*self.priority(r), r[0].lower()))
        else:
            self.all_rooms.sort(key=lambda r: (*self.priority(r), r[2].lower(), r[1].lower()))

    def short_id(self, num: str) -> str:
        """Truncate room ID to the configured width with ellipsis."""
        width = self.id_width
        if len(num) <= width:
            return num
        return num[: width - 1] + "\u2026"

    @staticmethod
    def fit_name(name: str, width: int) -> str:
        r"""Left-justify *name* to *width*, adding ``\u2026`` if truncated."""
        if len(name) <= width:
            return name.ljust(width)
        return name[: width - 1] + "\u2026"

    def room_label(self, num: str, name: str = "") -> rich.text.Text:
        """Format a child leaf label with muted name, aligned dist/time + id."""
        if self.sort_mode == "last_visited":
            lv = self.last_visited.get(num, "")
            info_part = client_tui_base.relative_time(lv).rjust(8) if lv else "".rjust(8)
        else:
            dist = self.distances.get(num)
            info_part = f"[{dist}]".rjust(5) if dist is not None else "     "
        id_part = f" #{self.short_id(num)}"
        child_col = self.name_col - 1
        name_part = self.fit_name(name, child_col)
        label = rich.text.Text(f"{name_part} {''.rjust(6)} {info_part}{id_part}")
        if name:
            label.stylize(self.muted_style, 0, len(name_part))
        return label

    def refresh_tree(self, query: str = "") -> None:
        """Rebuild tree nodes, grouping rooms with the same name."""
        tree = self.query_one("#room-tree", textual.widgets.Tree)
        tree.clear()
        q = query.lower()
        select = self.query_one("#room-area-select", textual.widgets.Select)
        area_filter = select.value if isinstance(select.value, str) else None

        groups: dict[str, list[tuple[str, str, int, bool, str]]] = {}
        group_order: list[str] = []
        for num, name, area, exits, bookmarked, lv, bl, hm, mk in self.all_rooms:
            if area_filter and area != area_filter:
                continue
            display_name = telix.rooms.strip_exit_dirs(name)
            if q and (q not in display_name.lower() and q not in area.lower() and q not in num.lower()):
                continue
            if display_name not in groups:
                groups[display_name] = []
                group_order.append(display_name)
            groups[display_name].append((num, area, exits, bookmarked, lv))

        # Populate icon sets for the RoomTree prefix renderer.
        if isinstance(tree, RoomTree):
            tree.bookmarked = {num for num, _, _, _, bm, _, _, _, _ in self.all_rooms if bm}
            tree.blocked = {num for num, _, _, _, _, _, bl, _, _ in self.all_rooms if bl}
            tree.home = {num for num, _, _, _, _, _, _, hm, _ in self.all_rooms if hm}
            tree.marked = {num for num, _, _, _, _, _, _, _, mk in self.all_rooms if mk}

        n_shown = 0
        with self.app.batch_update():
            for name in group_order:
                members = groups[name]
                n_shown += len(members)
                show_time = self.sort_mode == "last_visited"
                if len(members) == 1:
                    num, area, exits, bookmarked, lv = members[0]
                    name_part = self.fit_name(name, self.name_col)
                    count_part = "(1)".rjust(6)
                    if show_time:
                        info_part = client_tui_base.relative_time(lv).rjust(8) if lv else "".rjust(8)
                    else:
                        dist = self.distances.get(num)
                        info_part = f"[{dist}]".rjust(5) if dist is not None else "     "
                    id_part = f" #{self.short_id(num)}"
                    label = f"{name_part} {count_part} {info_part}{id_part}"
                    tree.root.add_leaf(rich.text.Text(label), data=num)
                else:
                    name_part = self.fit_name(name, self.name_col)
                    count_part = f"({len(members)})".rjust(6)
                    if show_time:
                        newest = max((m[4] for m in members if m[4]), default="")
                        info_part = client_tui_base.relative_time(newest).rjust(8) if newest else "".rjust(8)
                    else:
                        nearest = min((self.distances.get(m[0], float("inf")) for m in members), default=float("inf"))
                        info_part = f"[{int(nearest)}]".rjust(5) if nearest != float("inf") else "     "
                    label = f"{name_part} {count_part} {info_part}"
                    parent = tree.root.add(rich.text.Text(label), data=None)
                    if show_time:
                        members.sort(key=lambda m: m[4] or "", reverse=True)
                    else:
                        members.sort(key=lambda m: self.distances.get(m[0], float("inf")))
                    for num, area, exits, bookmarked, lv in members:
                        parent.add_leaf(self.room_label(num, name), data=num)

        count_label = self.query_one("#room-count", textual.widgets.Static)
        n_total = len(self.all_rooms)
        count_label.update(f"{n_shown:,} Rooms")
        total_label = self.query_one("#room-total", textual.widgets.Static)
        total_label.update(f"{n_total:,} Rooms Total")

    def get_selected_room_num(self) -> str | None:
        """Return the room number of the currently highlighted tree node."""
        tree = self.query_one("#room-tree", textual.widgets.Tree)
        node = tree.cursor_node
        if node is None:
            return None
        if node.data is not None:
            return str(node.data)
        if node.children:
            first = node.children[0]
            if first.data is not None:
                return str(first.data)
        return None

    def on_select_changed(self, event: textual.widgets.Select.Changed) -> None:
        """Re-filter tree when area dropdown changes."""
        if event.select.id == "room-area-select" and self.mounted:
            search_val = self.query_one("#room-search", textual.widgets.Input).value
            self.refresh_tree(search_val)

    def on_input_changed(self, event: textual.widgets.Input.Changed) -> None:
        """Filter tree when search input changes."""
        if event.input.id == "room-search":
            self.refresh_tree(event.value)

    def exits_text(self, room_num: str) -> str:
        """Build an exits summary like ``Exits: north[Town Square], east[Shop]``."""
        if self.graph is None:
            return ""
        adj = self.graph.adj.get(room_num, {})
        if not adj:
            return ""
        parts: list[str] = []
        for direction, dst_num in adj.items():
            room = self.graph.get_room(dst_num)
            name = room.name if room else dst_num[:8]
            parts.append(f"{direction}[{name}]")
        return "Exits: " + ", ".join(parts)

    def set_travel_buttons_disabled(self, disabled: bool) -> None:
        """Enable or disable the Travel button."""
        try:
            self.query_one("#room-travel", textual.widgets.Button).disabled = disabled
        except textual.css.query.NoMatches:
            pass

    def on_tree_node_highlighted(self, event: textual.widgets.Tree.NodeHighlighted[str]) -> None:
        """Update distance and exits labels when tree cursor moves."""
        self.cursor_just_moved = True
        dist_label = self.query_one("#room-distance", textual.widgets.Static)
        exits_label = self.query_one("#room-exits", textual.widgets.Static)
        node = event.node
        room_num = node.data if node.data is not None else None
        if room_num is None and node.children:
            first = node.children[0]
            if first.data is not None:
                room_num = first.data
        if room_num is None:
            dist_label.update("")
            exits_label.update("")
            self.set_travel_buttons_disabled(True)
            return
        exits_label.update(self.exits_text(room_num))
        if not self.current_room_file or self.graph is None:
            dist_label.update("")
            self.set_travel_buttons_disabled(True)
            return
        current = telix.rooms.read_current_room(self.current_room_file)
        if not current:
            dist_label.update("")
            self.set_travel_buttons_disabled(True)
            return
        if current == room_num:
            dist_label.update("Distance: 0 turns")
            self.set_travel_buttons_disabled(False)
            return
        path = self.graph.find_path(current, room_num)
        if path is None:
            dist_label.update("Distance: \u2014")
            self.set_travel_buttons_disabled(True)
        else:
            n = len(path)
            dist_label.update(f"Distance: {n} turn{'s' if n != 1 else ''}")
            self.set_travel_buttons_disabled(False)

    def on_tree_node_selected(self, event: textual.widgets.Tree.NodeSelected[str]) -> None:
        """Travel when clicking an already-selected node or pressing Enter."""
        if self.cursor_just_moved:
            self.cursor_just_moved = False
            return
        self.do_travel()

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle button presses."""
        handlers: dict[str, typing.Any] = {
            "room-close": lambda: self.request_close(None),
            "room-travel": self.do_travel,
            "room-bookmark": self.do_toggle_bookmark,
            "room-block": self.do_toggle_block,
            "room-home": self.do_toggle_home,
            "room-mark": self.do_toggle_mark,
            "room-help": self.action_show_help,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    def action_show_help(self) -> None:
        """Open the room browser help screen."""
        self.app.push_screen(client_tui_base.CommandHelpScreen(topic="room"))

    def on_key(self, event: textual.events.Key) -> None:
        """Arrow keys navigate between search, buttons, and the room tree."""
        if event.key not in ("up", "down", "left", "right"):
            return
        focused = self.screen.focused
        search = self.query_one("#room-search", textual.widgets.Input)
        tree = self.query_one("#room-tree", textual.widgets.Tree)
        buttons = list(self.query("#room-button-col Button"))
        if focused is search:
            if event.key == "down":
                tree.focus()
                event.stop()
            elif event.key == "left" and buttons:
                buttons[0].focus()
                event.prevent_default()
            return
        area_select = self.query_one("#room-area-select", textual.widgets.Select)
        if isinstance(focused, textual.widgets.Button) and focused in buttons:
            idx = buttons.index(focused)
            if event.key == "up" and idx > 0:
                buttons[idx - 1].focus()
            elif event.key == "down":
                (buttons[idx + 1] if idx < len(buttons) - 1 else area_select).focus()
            elif event.key == "right":
                search.focus()
            else:
                return
            event.prevent_default()
            return
        if focused is area_select:
            if event.key == "up" and buttons:
                buttons[-1].focus()
                event.prevent_default()
            elif event.key == "right":
                tree.focus()
                event.prevent_default()
            return
        if focused is tree:
            node = tree.cursor_node
            if event.key == "up" and tree.cursor_line == 0:
                search.focus()
                event.prevent_default()
            elif event.key == "left":
                if node is not None and node.allow_expand and node.is_expanded:
                    node.collapse()
                elif buttons:
                    buttons[0].focus()
                event.prevent_default()
            elif event.key == "right":
                if node is not None and node.allow_expand and node.is_collapsed:
                    node.expand()
                    event.prevent_default()
            return

    def action_close(self) -> None:
        """Close the room browser."""
        self.request_close(None)

    def action_fast_travel(self) -> None:
        """Initiate travel to the selected room."""
        self.do_travel()

    def action_sort_name(self) -> None:
        """Sort rooms by name."""
        self.sort_mode = "name"
        self.apply_sort()

    def action_sort_id(self) -> None:
        """Sort rooms by ID."""
        self.sort_mode = "id"
        self.apply_sort()

    def action_sort_distance(self) -> None:
        """Sort rooms by distance from current room."""
        self.sort_mode = "distance"
        self.compute_distances()
        self.apply_sort()

    def action_sort_last(self) -> None:
        """Sort rooms by last visited time, most recent first."""
        self.sort_mode = "last_visited"
        self.apply_sort()

    def apply_sort(self, select_num: str | None = None) -> None:
        """Re-sort rooms and refresh the tree, preserving selection."""
        if select_num is None:
            select_num = self.get_selected_room_num()
        self.sort_rooms()
        try:
            heading = self.query_one("#room-heading", textual.widgets.Static)
            heading.update(self.heading_text())
        except textual.css.query.NoMatches:
            pass
        search_val = self.query_one("#room-search", textual.widgets.Input).value
        self.refresh_tree(search_val)
        if select_num:
            self.select_room_node(select_num)

    def action_toggle_bookmark(self) -> None:
        """Toggle bookmark on the selected room."""
        self.do_toggle_bookmark()

    def action_toggle_block(self) -> None:
        """Toggle block on the selected room."""
        self.do_toggle_block()

    def action_toggle_home(self) -> None:
        """Toggle home on the selected room."""
        self.do_toggle_home()

    def action_toggle_mark(self) -> None:
        """Toggle mark on the selected room."""
        self.do_toggle_mark()

    def do_toggle_marker(self, marker: str) -> None:
        """Toggle an exclusive marker on the currently selected room."""
        num = self.get_selected_room_num()
        if num is None:
            return

        store = telix.rooms.RoomStore(self.rooms_path)
        store.set_marker(num, marker)
        self.all_rooms = store.room_summaries()
        store.close()
        self.apply_sort(select_num=num)

    def do_toggle_bookmark(self) -> None:
        """Toggle bookmark on the currently selected room."""
        self.do_toggle_marker("bookmarked")

    def do_toggle_block(self) -> None:
        """Toggle blocked on the currently selected room."""
        self.do_toggle_marker("blocked")

    def do_toggle_home(self) -> None:
        """Toggle home on the currently selected room."""
        self.do_toggle_marker("home")

    def do_toggle_mark(self) -> None:
        """Toggle mark on the currently selected room."""
        self.do_toggle_marker("marked")

    def do_travel(self) -> None:
        """Calculate path and write fast travel file."""
        dst_num = self.get_selected_room_num()
        if dst_num is None:
            return

        current = telix.rooms.read_current_room(self.current_room_file)
        if not current:
            count = self.query_one("#room-count", textual.widgets.Static)
            count.update("No current room \u2014 move first")
            return

        if current == dst_num:
            count = self.query_one("#room-count", textual.widgets.Static)
            count.update("Already in this room")
            return

        graph = telix.rooms.RoomStore(self.rooms_path, read_only=True)
        try:
            path = graph.find_path_with_rooms(current, dst_num)
        finally:
            graph.close()
        if path is None:
            dst_name = ""
            for rnum, name, *_ in self.all_rooms:
                if rnum == dst_num:
                    dst_name = name
                    break
            count = self.query_one("#room-count", textual.widgets.Static)
            count.update(f"No path found to {dst_name or dst_num}")
            return

        telix.rooms.write_fasttravel(self.fasttravel_file, path)
        self.request_close(True)


class RoomBrowserScreen(textual.screen.Screen["bool | None"]):
    """Thin screen wrapper for the room browser."""

    def __init__(
        self, rooms_path: str, session_key: str = "", current_room_file: str = "", fasttravel_file: str = ""
    ) -> None:
        super().__init__()
        self.pane = RoomBrowserPane(
            rooms_path=rooms_path,
            session_key=session_key,
            current_room_file=current_room_file,
            fasttravel_file=fasttravel_file,
        )

    def compose(self) -> textual.app.ComposeResult:
        yield self.pane
        yield textual.widgets.Footer()


class RoomPickerPane(RoomBrowserPane):
    """Pane variant for room picking with Select/Cancel buttons."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [
        textual.binding.Binding("escape", "close", "Close", priority=True),
        textual.binding.Binding("enter", "select_room", "Select", show=True),
        textual.binding.Binding("n", "sort_name", "Name sort", show=True),
        textual.binding.Binding("i", "sort_id", "ID sort", show=True),
        textual.binding.Binding("d", "sort_distance", "Dist sort", show=True),
        textual.binding.Binding("l", "sort_last", "Recent", show=True),
    ]

    def compose(self) -> textual.app.ComposeResult:
        """Build the room picker layout with Select/Cancel buttons only."""
        with textual.containers.Vertical(id="room-panel"), textual.containers.Horizontal(id="room-body"):
            with textual.containers.Vertical(id="room-button-col"):
                yield textual.widgets.Button("Select", variant="success", id="room-select")
                yield textual.widgets.Button("Cancel", id="room-close")
                with textual.containers.Vertical(id="room-area-frame"):
                    yield textual.widgets.Static("Area:")
                    yield textual.widgets.Select[str]([], id="room-area-select", allow_blank=True, prompt="All Areas")
            with textual.containers.Vertical(id="room-right"):
                yield textual.widgets.Input(placeholder="Search rooms\u2026", id="room-search")
                yield RoomTree("Rooms", id="room-tree")
                with textual.containers.Horizontal(id="room-status"):
                    yield textual.widgets.Static("", id="room-count")
                    yield textual.widgets.Static("", id="room-distance")
                yield textual.widgets.Static("", id="room-exits")

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle Select/Cancel button presses."""
        if event.button.id == "room-close":
            self.request_close(None)
        elif event.button.id == "room-select":
            self.do_select()

    def action_select_room(self) -> None:
        """Select the highlighted room."""
        self.do_select()

    def do_select(self) -> None:
        """Dismiss with the selected room ID string."""
        num = self.get_selected_room_num()
        if num is None:
            return
        self.request_close(num)  # type: ignore[arg-type]


class RoomPickerScreen(textual.screen.Screen["str | None"]):
    """Thin screen wrapper for the room picker."""

    def __init__(self, rooms_path: str, session_key: str = "", current_room_file: str = "") -> None:
        super().__init__()
        self.pane = RoomPickerPane(rooms_path=rooms_path, session_key=session_key, current_room_file=current_room_file)

    def compose(self) -> textual.app.ComposeResult:
        yield self.pane
        yield textual.widgets.Footer()


# ---------------------------------------------------------------------------
# Captures and chats viewer
# ---------------------------------------------------------------------------


class CapsPane(textual.containers.Vertical):
    """Pane widget for captures and chats viewing."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("escape", "close", "Close", show=True),
        textual.binding.Binding("f10", "close", "Close", show=False),
        textual.binding.Binding("q", "close", "Close", show=False),
        textual.binding.Binding("f1", "toggle_keys", "Keys", show=True),
        textual.binding.Binding("up", "prev_channel", "Prev", show=False),
        textual.binding.Binding("down", "next_channel", "Next", show=False),
        textual.binding.Binding("tab", "next_channel", "Next Channel", show=True),
        textual.binding.Binding("shift+tab", "prev_channel", "Prev Channel", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    CapsPane {
        width: 100%; height: 100%;
    }
    #chat-sidebar {
        width: 16;
        height: 100%;
        background: $surface;
    }
    #chat-sidebar Button {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0 1;
        border: none;
        background: $surface-lighten-1;
        color: $text-muted;
    }
    #chat-sidebar Button.active-channel {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    #chat-log {
        height: 100%;
        width: 1fr;
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(
        self, chat_file: str, session_key: str = "", initial_channel: str = "", capture_file: str = ""
    ) -> None:
        """
        Initialize with path to chat history file.

        :param chat_file: Path to the chat JSON file.
        :param session_key: Session identifier.
        :param initial_channel: Channel to select on open (most recent activity).
        :param capture_file: Path to a JSON file with capture data.
        """
        super().__init__()
        self.chat_file = chat_file
        self.session_key = session_key
        self.initial_channel = initial_channel
        self.capture_file = capture_file
        self.messages: list[dict[str, typing.Any]] = []
        self.channels: list[str] = []
        self.filter_idx: int = 0
        self.captures: dict[str, int] = {}
        self.capture_log: dict[str, list[dict[str, typing.Any]]] = {}

    def compose(self) -> textual.app.ComposeResult:
        """Build the chat viewer layout with a vertical channel sidebar."""
        with textual.containers.Horizontal():
            with textual.containers.Vertical(id="chat-sidebar"):
                pass
            yield textual.widgets.RichLog(highlight=False, markup=False, wrap=True, id="chat-log")

    def on_mount(self) -> None:
        """Load chat file, select initial channel, and populate the log."""
        self.load_messages()
        if self.initial_channel and self.initial_channel in self.channels:
            self.filter_idx = self.channels.index(self.initial_channel) + 1
        self.rebuild_sidebar()
        self.populate_log()
        self.call_after_refresh(self.focus_active_channel)

    def focus_active_channel(self) -> None:
        """Focus the active channel button in the sidebar."""
        buttons = list(self.query("#chat-sidebar Button"))
        if buttons:
            idx = min(self.filter_idx, len(buttons) - 1)
            buttons[idx].focus()

    def load_messages(self) -> None:
        """Read messages from chat JSON and capture data files."""
        if self.chat_file and os.path.exists(self.chat_file):
            with open(self.chat_file, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self.messages = data
        if self.capture_file and os.path.exists(self.capture_file):
            with open(self.capture_file, encoding="utf-8") as fh:
                cap_data = json.load(fh)
            if isinstance(cap_data, dict):
                self.captures = cap_data.get("captures", {})
                self.capture_log = cap_data.get("capture_log", {})
        channels: set[str] = set()
        for msg in self.messages:
            ch = msg.get("channel", "")
            if ch:
                channels.add(ch)
        for ch in self.capture_log:
            channels.add(ch)
        self.channels = sorted(channels)

    def channel_labels(self) -> list[str]:
        """Return ``["all", ...channels]`` list used for cycling."""
        return ["all"] + self.channels

    def active_filter(self) -> str:
        """Return the current channel filter string, or ``""`` for all."""
        labels = self.channel_labels()
        if self.filter_idx == 0 or self.filter_idx >= len(labels):
            return ""
        return labels[self.filter_idx]

    def rebuild_sidebar(self) -> None:
        """Rebuild the channel sidebar buttons to match current channel list."""
        sidebar = self.query_one("#chat-sidebar", textual.containers.Vertical)
        sidebar.remove_children()
        for i, name in enumerate(self.channel_labels()):
            display = name if len(name) <= 14 else name[:14] + "\u2026"
            btn = textual.widgets.Button(display, id=f"ch-btn-{i}")
            if i == self.filter_idx:
                btn.add_class("active-channel")
            sidebar.mount(btn)

    def update_channel_sidebar(self) -> None:
        """Update active-channel class on sidebar buttons without rebuilding."""
        for btn in self.query("#chat-sidebar Button"):
            btn_idx = int((btn.id or "ch-btn-0").split("-")[-1])
            if btn_idx == self.filter_idx:
                btn.add_class("active-channel")
            else:
                btn.remove_class("active-channel")

    def populate_log(self, channel_filter: str = "") -> None:
        """Fill the RichLog with chat and capture messages, optionally filtered."""
        if not channel_filter:
            channel_filter = self.active_filter()
        log_widget: textual.widgets.RichLog = self.query_one("#chat-log", textual.widgets.RichLog)
        log_widget.clear()

        # Show captures key/value table when viewing the "captures" channel
        if channel_filter == "captures" and self.captures:
            header = rich.text.Text("Current Captures:", style="bold underline")
            log_widget.write(header)
            for k, v in sorted(self.captures.items()):
                log_widget.write(rich.text.Text(f"  {k}: {v}"))
            log_widget.write(rich.text.Text(""))

        # Merge GMCP chat messages and capture log entries by timestamp
        all_entries: list[tuple[str, str, dict[str, typing.Any]]] = []
        for msg in self.messages:
            ch = msg.get("channel", "")
            if channel_filter and ch != channel_filter:
                continue
            all_entries.append((msg.get("ts", ""), "chat", msg))
        for ch, entries in self.capture_log.items():
            if channel_filter and ch != channel_filter:
                continue
            for entry in entries:
                all_entries.append((entry.get("ts", ""), "capture", {**entry, "channel": ch}))
        all_entries.sort(key=lambda e: e[0])

        log_width = (log_widget.size.width or 80) - 2

        for ts_val, source, msg in all_entries:
            ch = msg.get("channel", "")

            # Build prefix rich.text and a plain-text copy for width measurement.
            prefix = rich.text.Text()
            prefix_plain = ""

            if ts_val:
                rel = f"{client_tui_base.relative_time(ts_val)} "
                prefix.append(rel, style="dim")
                prefix_plain += rel
            if not channel_filter:
                channel_ansi = msg.get("channel_ansi", "")
                if channel_ansi:
                    ch_rich = rich.text.Text.from_ansi(channel_ansi.rstrip())
                    prefix.append_text(ch_rich)
                    prefix.append(" ")
                    prefix_plain += ch_rich.plain + " "
                else:
                    ch_label = f"[{ch}] "
                    prefix.append(ch_label, style="bold cyan")
                    prefix_plain += ch_label

            if source == "chat":
                talker = msg.get("talker", "")
                body_text = msg.get("text", "").rstrip("\n")
                if talker:
                    for pfx_str in (f"{talker} : ", f"{talker}: ", f"{talker} "):
                        if body_text.startswith(pfx_str):
                            body_text = body_text[len(pfx_str) :]
                            break
                    talker_label = f"{talker}: "
                    prefix.append(talker_label, style="bold")
                    prefix_plain += talker_label
                hl_style = ""
            else:
                body_text = msg.get("line", "")
                hl_style = msg.get("highlight", "")

            indent_width = wcwidth.wcswidth(prefix_plain)
            if indent_width < 0:
                indent_width = len(prefix_plain)
            indent = " " * indent_width
            body_width = max(log_width - indent_width, 10)

            wrapped = wcwidth.wrap(body_text, width=body_width, subsequent_indent="", propagate_sgr=True)
            if not wrapped:
                wrapped = [""]

            for i, wline in enumerate(wrapped):
                out = rich.text.Text()
                out.append_text(prefix) if i == 0 else out.append(indent)
                if hl_style:
                    out.append(wline, style=hl_style.replace("_", " "))
                else:
                    out.append_text(rich.text.Text.from_ansi(wline))
                log_widget.write(out)
        log_widget.scroll_end(animate=False)

    def on_resize(self, event: textual.events.Resize) -> None:
        """Re-wrap messages when the pane is resized."""
        self.populate_log()

    def action_close(self) -> None:
        """Dismiss the chat viewer."""
        self.app.exit()

    def action_toggle_keys(self) -> None:
        """Toggle the Textual keys help panel."""
        if self.app.help_panel:  # type: ignore[attr-defined]
            self.app.action_hide_help_panel()
        else:
            self.app.action_show_help_panel()

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle channel sidebar button clicks."""
        btn_id = event.button.id or ""
        if btn_id.startswith("ch-btn-"):
            self.filter_idx = int(btn_id.split("-")[-1])
            self.update_channel_sidebar()
            self.populate_log()

    def action_next_channel(self) -> None:
        """Cycle forward through channel filters."""
        if not self.channels:
            return
        labels = self.channel_labels()
        self.filter_idx = (self.filter_idx + 1) % len(labels)
        self.update_channel_sidebar()
        self.populate_log()

    def action_prev_channel(self) -> None:
        """Cycle backward through channel filters."""
        if not self.channels:
            return
        labels = self.channel_labels()
        self.filter_idx = (self.filter_idx - 1) % len(labels)
        self.update_channel_sidebar()
        self.populate_log()


class CapsScreen(textual.screen.Screen[None]):
    """Thin screen wrapper for the captures and chats viewer."""

    def __init__(
        self, chat_file: str, session_key: str = "", initial_channel: str = "", capture_file: str = ""
    ) -> None:
        super().__init__()
        self.pane = CapsPane(
            chat_file=chat_file, session_key=session_key, initial_channel=initial_channel, capture_file=capture_file
        )

    def compose(self) -> textual.app.ComposeResult:
        yield self.pane
        yield textual.widgets.Footer()


# Keep backwards-compatible alias.
ChatViewerScreen = CapsScreen


def chat_viewer_main(
    chat_file: str, session_key: str = "", initial_channel: str = "", logfile: str = "", capture_file: str = ""
) -> None:
    """Launch standalone Capture Window TUI."""
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    app = client_tui_base.EditorApp(
        ChatViewerScreen(  # type: ignore[arg-type]
            chat_file=chat_file, session_key=session_key, initial_channel=initial_channel, capture_file=capture_file
        ),
        session_key=session_key,
    )
    app.run(mouse=False)


# ---------------------------------------------------------------------------
# Tabbed editor: combines all 8 editor panes into a single TUI subprocess.
# ---------------------------------------------------------------------------

# Tab definitions: (label, tab_id)
EDITOR_TABS: list[tuple[str, str]] = [
    ("Highlights", "highlights"),
    ("Rooms", "rooms"),
    ("Macros", "macros"),
    ("Autoreplies", "autoreplies"),
    ("Chats", "captures"),
    ("Bars", "bars"),
    ("Theme", "theme"),
]

GLOBAL_TABS: set[str] = {"theme"}


class TabbedEditorScreen(textual.screen.Screen[None]):
    """Full-screen tabbed editor combining all editor panes."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("escape", "close_or_back", "Close", priority=True),
        textual.binding.Binding("f1", "show_help", "Help", show=False, priority=True),
    ]

    CSS = """
    TabbedEditorScreen {
        height: 100%;
        width: 100%;
    }
    #te-tab-bar {
        height: 1;
        background: $surface;
    }
    #te-tab-bar Button {
        min-width: 0;
        height: 1;
        margin: 0 0 0 0;
        padding: 0 1;
        border: none;
        background: $surface-lighten-1;
    }
    #te-tab-bar Button.active-tab {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    #te-content {
        height: 1fr;
    }
    """

    def __init__(self, params: dict[str, typing.Any]) -> None:
        """
        Initialize tabbed editor from a parameters dict.

        :param params: Dict with keys for each pane's
            constructor args, plus ``initial_tab``,
            ``initial_channel``, and ``hide_globals``.
        """
        super().__init__()
        self.params = params
        self.initial_tab = params.get("initial_tab", "highlights")
        self.hide_globals: bool = params.get("hide_globals", False)
        self.panes: dict[str, typing.Any] = {}
        self.loaded: set[str] = set()
        self.dirty: set[str] = set()

    def visible_tabs(self) -> list[tuple[str, str]]:
        """Return tabs filtered by ``hide_globals``."""
        if self.hide_globals:
            return [(label, tid) for label, tid in EDITOR_TABS if tid not in GLOBAL_TABS]
        return list(EDITOR_TABS)

    def compose(self) -> textual.app.ComposeResult:
        """Build tab bar and content switcher with lazy-loaded panes."""
        tabs = self.visible_tabs()
        with textual.containers.Horizontal(id="te-tab-bar"):
            for label, tab_id in tabs:
                btn = textual.widgets.Button(label, id=f"te-btn-{tab_id}")
                if tab_id == self.initial_tab:
                    btn.add_class("active-tab")
                yield btn

        with textual.widgets.ContentSwitcher(id="te-content", initial=self.initial_tab):
            for label, tab_id in tabs:
                pane = self.create_pane(tab_id)
                self.panes[tab_id] = pane
                yield pane

        yield textual.widgets.Footer()

    PANE_FACTORIES: typing.ClassVar[dict[str, tuple[type, dict[str, str]]]] = {
        "highlights": (client_tui_editors.HighlightEditPane, {"path": "highlights_file", "session_key": "session_key"}),
        "rooms": (
            RoomBrowserPane,
            {
                "rooms_path": "rooms_file",
                "session_key": "session_key",
                "current_room_file": "current_room_file",
                "fasttravel_file": "fasttravel_file",
            },
        ),
        "macros": (
            client_tui_editors.MacroEditPane,
            {
                "path": "macros_file",
                "session_key": "session_key",
                "rooms_file": "rooms_file",
                "current_room_file": "current_room_file",
            },
        ),
        "autoreplies": (
            client_tui_editors.AutoreplyEditPane,
            {
                "path": "autoreplies_file",
                "session_key": "session_key",
                "select_pattern": "select_pattern",
                "gmcp_snapshot_path": "gmcp_snapshot_file",
            },
        ),
        "captures": (
            CapsPane,
            {
                "chat_file": "chat_file",
                "session_key": "session_key",
                "initial_channel": "initial_channel",
                "capture_file": "capture_file",
            },
        ),
        "bars": (
            client_tui_editors.ProgressBarEditPane,
            {"path": "progressbars_file", "session_key": "session_key", "gmcp_snapshot_path": ("gmcp_snapshot_file")},
        ),
        "theme": (client_tui_editors.ThemeEditPane, {"session_key": "session_key"}),
    }

    def create_pane(self, tab_id: str) -> textual.containers.Vertical:
        """Instantiate the pane widget for *tab_id*."""
        cls, param_map = self.PANE_FACTORIES[tab_id]
        kwargs = {k: self.params.get(v, "") for k, v in param_map.items()}
        pane = cls(**kwargs)
        pane.id = tab_id
        return pane  # type: ignore[no-any-return]

    def on_mount(self) -> None:
        """Mark the initial tab as loaded."""
        self.loaded.add(self.initial_tab)

    def action_show_help(self) -> None:
        """Open the keybindings help screen."""
        self.app.push_screen(client_tui_base.CommandHelpScreen(topic="keybindings"))

    def on_key(self, event: textual.events.Key) -> None:
        """Left/right switch tabs only when a tab-bar button has focus."""
        if event.key not in ("left", "right"):
            return
        tab_buttons = list(self.query("#te-tab-bar Button"))
        if self.focused not in tab_buttons:
            return
        if event.key == "left":
            self.action_prev_tab()
        else:
            self.action_next_tab()
        event.prevent_default()

    def action_next_tab(self) -> None:
        """Switch to the next tab (wraps around)."""
        ids = [tid for _, tid in self.visible_tabs()]
        current = self.query_one("#te-content", textual.widgets.ContentSwitcher).current or ""
        idx = ids.index(current) if current in ids else 0
        self.action_switch_tab(ids[(idx + 1) % len(ids)])

    def action_prev_tab(self) -> None:
        """Switch to the previous tab (wraps around)."""
        ids = [tid for _, tid in self.visible_tabs()]
        current = self.query_one("#te-content", textual.widgets.ContentSwitcher).current or ""
        idx = ids.index(current) if current in ids else 0
        self.action_switch_tab(ids[(idx - 1) % len(ids)])

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch to *tab_id*, loading it lazily if needed."""
        switcher = self.query_one("#te-content", textual.widgets.ContentSwitcher)
        if switcher.current == tab_id:
            return
        switcher.current = tab_id
        for btn in self.query("#te-tab-bar Button"):
            btn.remove_class("active-tab")
            if btn.id == f"te-btn-{tab_id}":
                btn.add_class("active-tab")
        if tab_id not in self.loaded:
            self.loaded.add(tab_id)
            pane = self.panes.get(tab_id)
            if pane and hasattr(pane, "load_from_file"):
                pane.load_from_file()
                if hasattr(pane, "refresh_table"):
                    pane.refresh_table()

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle tab bar button clicks."""
        btn_id = event.button.id or ""
        if btn_id.startswith("te-btn-"):
            tab_id = btn_id[len("te-btn-") :]
            self.action_switch_tab(tab_id)

    def action_close_or_back(self) -> None:
        """Close form if visible in an editor pane, otherwise exit app."""
        current = self.query_one("#te-content", textual.widgets.ContentSwitcher).current
        pane = self.panes.get(current or "")
        if pane and hasattr(pane, "form_visible") and pane.form_visible:
            if hasattr(pane, "action_cancel_or_close"):
                pane.action_cancel_or_close()
            return
        self.save_all_dirty()
        self.app.exit()

    def save_all_dirty(self) -> None:
        """Auto-save any panes that have been loaded and have a save method."""
        for tab_id in self.loaded:
            pane = self.panes.get(tab_id)
            if pane and hasattr(pane, "save_to_file"):
                pane.save_to_file()

    def handle_travel(self) -> None:
        """Save all editors and exit when room travel is requested."""
        self.save_all_dirty()
        self.app.exit()


def unified_editor_main() -> None:
    """
    Launch the tabbed editor TUI subprocess.

    Reads a single JSON blob from ``sys.argv[1]``
    containing all parameters for every pane. Called from
    the REPL via ``launch_unified_editor()``.
    """
    params = json.loads(sys.argv[1])
    logfile = params.get("logfile", "")
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()

    screen = TabbedEditorScreen(params)
    session_key = params.get("session_key", "")
    app = client_tui_base.EditorApp(screen, session_key=session_key)  # type: ignore[arg-type]
    client_tui_base.run_editor_app(app)


# ---------------------------------------------------------------------------
# Confirm dialog
# ---------------------------------------------------------------------------


class ConfirmDialogScreen(textual.screen.Screen[bool]):
    """Confirmation dialog with optional warning."""

    BINDINGS = [textual.binding.Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    ConfirmDialogScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: round $surface-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #confirm-body {
        margin-bottom: 1;
    }
    #confirm-warning {
        color: $error;
        margin-bottom: 1;
    }
    #confirm-buttons {
        height: 3;
        align-horizontal: right;
    }
    #confirm-buttons Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }
    """

    def __init__(
        self, title: str, body: str, warning: str = "", result_file: str = "", show_dont_ask: bool = True
    ) -> None:
        """Initialize confirm dialog with title, body, and optional warning."""
        super().__init__()
        self.title = title
        self.body = body
        self.warning = warning
        self.result_file = result_file
        self.show_dont_ask = show_dont_ask

    def compose(self) -> textual.app.ComposeResult:
        """Build the confirm dialog layout."""
        with textual.containers.Vertical(id="confirm-dialog"):
            yield textual.widgets.Static(self.title, id="confirm-title")  # type: ignore[arg-type]
            yield textual.widgets.Static(self.body, id="confirm-body")
            if self.warning:
                yield textual.widgets.Static(self.warning, id="confirm-warning")
            with textual.containers.Horizontal(id="confirm-buttons"):
                yield textual.widgets.Button("Cancel", variant="error", id="confirm-cancel")
                yield textual.widgets.Button("OK", variant="success", id="confirm-ok")

    def on_mount(self) -> None:
        """Focus OK button on mount."""
        self.query_one("#confirm-ok", textual.widgets.Button).focus()

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle OK/Cancel button presses."""
        if event.button.id == "confirm-ok":
            self.write_result(True)
            self.dismiss(True)
        elif event.button.id == "confirm-cancel":
            self.write_result(False)
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Handle Escape key."""
        self.write_result(False)
        self.dismiss(False)

    def write_result(self, confirmed: bool) -> None:
        """Write result to file for the parent process to read."""
        if not self.result_file:
            return
        result = json.dumps({"confirmed": confirmed})
        with open(self.result_file, "w", encoding="utf-8") as f:
            f.write(result)


def confirm_dialog_main(title: str, body: str, warning: str = "", result_file: str = "", logfile: str = "") -> None:
    """Launch standalone confirm dialog TUI."""
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    screen = ConfirmDialogScreen(title=title, body=body, warning=warning, result_file=result_file)
    app = client_tui_base.EditorApp(screen)  # type: ignore[arg-type]
    client_tui_base.run_editor_app(app)


# ---------------------------------------------------------------------------
# Random walk dialog
# ---------------------------------------------------------------------------


class RandomwalkDialogScreen(textual.screen.Screen[bool]):
    """Random walk confirmation dialog with visit-level parameter."""

    BINDINGS = [
        textual.binding.Binding("escape", "cancel", "Cancel", show=False),
        textual.binding.Binding("f1", "show_help", "Help", show=False),
    ]

    DEFAULT_CSS = """
    RandomwalkDialogScreen {
        align: center middle;
    }
    #rw-dialog {
        width: 100%;
        height: 100%;
        border: round $surface-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    #rw-title {
        text-style: bold;
        text-align: center;
    }
    #rw-body {
        margin-bottom: 1;
    }
    #rw-warning {
        color: $error;
        margin-bottom: 1;
    }
    #rw-hint {
        margin-bottom: 1;
    }
    #rw-options-col {
        height: auto;
        margin-bottom: 1;
    }
    .rw-option {
        height: 3;
        margin-bottom: 1;
    }
    .rw-option Label {
        padding-top: 1;
        width: auto;
        margin-right: 1;
    }
    .rw-option Input {
        width: 8;
    }
    .rw-option Switch {
        width: auto;
    }
    #rw-switches {
        height: auto;
        margin-bottom: 1;
        border: round $surface-lighten-2;
        padding: 0 1;
    }
    .rw-switch-row {
        height: 3;
    }
    .rw-switch-cell {
        width: 1fr;
        height: 3;
    }
    .rw-switch-cell Label {
        padding-top: 1;
        width: auto;
        margin-right: 1;
    }
    .rw-switch-cell Switch {
        width: auto;
    }
    #rw-error {
        color: $error;
        height: 1;
    }
    #rw-buttons {
        height: 3;
        align-horizontal: right;
    }
    #rw-buttons Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }
    """

    def __init__(
        self,
        result_file: str = "",
        default_visit_level: int = 2,
        default_auto_search: bool = False,
        default_auto_evaluate: bool = False,
        default_auto_survey: bool = False,
        default_autoreplies: bool = True,
    ) -> None:
        super().__init__()
        self.result_file = result_file
        self.default_visit_level = default_visit_level
        self.default_auto_search = default_auto_search
        self.default_auto_evaluate = default_auto_evaluate
        self.default_auto_survey = default_auto_survey
        self.default_autoreplies = default_autoreplies

    def compose(self) -> textual.app.ComposeResult:
        with textual.containers.Vertical(id="rw-dialog"):
            yield textual.widgets.Static("Random Walk", id="rw-title")
            yield textual.widgets.Static(
                "Random walk explores rooms by picking "
                "random exits, "
                "preferring unvisited rooms. It never "
                "returns through "
                "the entrance you came from. Autoreplies "
                "fire in each "
                "room. Stops when all reachable rooms are "
                "visited the "
                "required number of times.",
                id="rw-body",
            )
            yield textual.widgets.Static(
                "WARNING: This can lead to dangerous "
                "areas, death traps, or aggressive "
                "monsters! Your character may die. "
                "Use with caution.",
                id="rw-warning",
            )
            yield textual.widgets.Static(
                "Tip: Use the Block button in the Rooms screen to exclude dangerous areas.", id="rw-hint"
            )
            with textual.containers.Vertical(id="rw-options-col"), textual.containers.Horizontal(classes="rw-option"):
                lbl = textual.widgets.Label("Visit level:")
                lbl.tooltip = "Minimum number of times each reachable room must be visited before the walk stops."
                yield lbl
                yield textual.widgets.Input(value=str(self.default_visit_level), id="rw-visit-level", type="integer")
            with textual.containers.Vertical(id="rw-switches"):
                with textual.containers.Horizontal(classes="rw-switch-row"):
                    with textual.containers.Horizontal(classes="rw-switch-cell"):
                        yield textual.widgets.Label("Auto search:")
                        yield textual.widgets.Switch(value=(self.default_auto_search), id="rw-auto-search")
                    with textual.containers.Horizontal(classes="rw-switch-cell"):
                        yield textual.widgets.Label("Auto consider:")
                        yield textual.widgets.Switch(
                            value=(self.default_auto_evaluate),
                            id="rw-auto-consider",
                            disabled=(not self.default_autoreplies),
                        )
                with textual.containers.Horizontal(classes="rw-switch-row"):
                    with textual.containers.Horizontal(classes="rw-switch-cell"):
                        yield textual.widgets.Label("Auto survey:")
                        yield textual.widgets.Switch(
                            value=(self.default_auto_survey),
                            id="rw-auto-survey",
                            disabled=(not self.default_autoreplies),
                        )
                    with textual.containers.Horizontal(classes="rw-switch-cell"):
                        yield textual.widgets.Label("Autoreplies:")
                        yield textual.widgets.Switch(value=(self.default_autoreplies), id="rw-autoreplies")
            yield textual.widgets.Static("", id="rw-error")
            with textual.containers.Horizontal(id="rw-buttons"):
                yield textual.widgets.Button("Help", variant="primary", id="rw-help")
                yield textual.widgets.Button("Cancel", variant="error", id="rw-cancel")
                yield textual.widgets.Button("OK", variant="success", id="rw-ok")

    def on_mount(self) -> None:
        """Focus the OK button on mount."""
        self.query_one("#rw-ok", textual.widgets.Button).focus()

    def action_show_help(self) -> None:
        """Show room-mapping help screen."""
        self.app.push_screen(client_tui_base.CommandHelpScreen(topic="room-mapping"))

    def on_switch_changed(self, event: textual.widgets.Switch.Changed) -> None:
        """Disable consider/survey switches when autoreplies is OFF."""
        if event.switch.id == "rw-autoreplies":
            self.query_one("#rw-auto-consider", textual.widgets.Switch).disabled = not event.value
            self.query_one("#rw-auto-survey", textual.widgets.Switch).disabled = not event.value

    def validate_and_dismiss(self) -> None:
        raw = self.query_one("#rw-visit-level", textual.widgets.Input).value.strip()
        try:
            level = int(raw)
        except ValueError:
            self.query_one("#rw-error", textual.widgets.Static).update("Visit level must be a number.")
            return
        if level < 1:
            self.query_one("#rw-error", textual.widgets.Static).update("Visit level must be at least 1.")
            return
        auto_search = self.query_one("#rw-auto-search", textual.widgets.Switch).value
        auto_evaluate = self.query_one("#rw-auto-consider", textual.widgets.Switch).value
        auto_survey = self.query_one("#rw-auto-survey", textual.widgets.Switch).value
        autoreplies = self.query_one("#rw-autoreplies", textual.widgets.Switch).value
        self.write_result(True, level, auto_search, auto_evaluate, auto_survey, autoreplies)
        self.dismiss(True)

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle OK, Cancel, and Help button presses."""
        if event.button.id == "rw-help":
            self.action_show_help()
        elif event.button.id == "rw-ok":
            self.validate_and_dismiss()
        elif event.button.id == "rw-cancel":
            self.write_result(
                False,
                self.default_visit_level,
                self.default_auto_search,
                self.default_auto_evaluate,
                self.default_auto_survey,
                self.default_autoreplies,
            )
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Cancel the dialog and write default values."""
        self.write_result(
            False,
            self.default_visit_level,
            self.default_auto_search,
            self.default_auto_evaluate,
            self.default_auto_survey,
            self.default_autoreplies,
        )
        self.dismiss(False)

    def write_result(
        self,
        confirmed: bool,
        visit_level: int,
        auto_search: bool = False,
        auto_evaluate: bool = False,
        auto_survey: bool = False,
        autoreplies: bool = True,
    ) -> None:
        if not self.result_file:
            return
        cmd = f"`randomwalk 999 {visit_level}"
        if auto_search:
            cmd += " autosearch"
        if auto_evaluate:
            cmd += " autoevaluate"
        if auto_survey:
            cmd += " autosurvey"
        if not autoreplies:
            cmd += " noreply"
        cmd += "`"
        result = json.dumps(
            {
                "confirmed": confirmed,
                "visit_level": visit_level,
                "auto_search": auto_search,
                "auto_evaluate": auto_evaluate,
                "auto_survey": auto_survey,
                "autoreplies": autoreplies,
                "command": cmd,
            }
        )
        with open(self.result_file, "w", encoding="utf-8") as f:
            f.write(result)


def randomwalk_dialog_main(
    result_file: str = "",
    default_visit_level: str = "2",
    default_auto_search: str = "0",
    default_auto_evaluate: str = "0",
    default_auto_survey: str = "0",
    default_autoreplies: str = "1",
    logfile: str = "",
) -> None:
    """Launch standalone random walk dialog TUI."""
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    screen = RandomwalkDialogScreen(
        result_file=result_file,
        default_visit_level=int(default_visit_level),
        default_auto_search=(default_auto_search == "1"),
        default_auto_evaluate=(default_auto_evaluate == "1"),
        default_auto_survey=(default_auto_survey == "1"),
        default_autoreplies=(default_autoreplies == "1"),
    )
    app = client_tui_base.EditorApp(screen)  # type: ignore[arg-type]
    client_tui_base.run_editor_app(app)


# ---------------------------------------------------------------------------
# Autodiscover dialog
# ---------------------------------------------------------------------------


class AutodiscoverDialogScreen(textual.screen.Screen[bool]):
    """Autodiscover confirmation dialog with BFS/DFS strategy selection."""

    BINDINGS = [
        textual.binding.Binding("escape", "cancel", "Cancel", show=False),
        textual.binding.Binding("f1", "show_help", "Help", show=False),
    ]

    DEFAULT_CSS = """
    AutodiscoverDialogScreen {
        align: center middle;
    }
    #ad-dialog {
        width: 100%;
        height: 100%;
        border: round $surface-lighten-2;
        background: $surface;
        padding: 1 2;
    }
    #ad-title {
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #ad-body {
        margin-bottom: 1;
    }
    #ad-warning {
        color: $error;
        margin-bottom: 1;
    }
    #ad-hint {
        margin-bottom: 1;
    }
    #ad-strategy-row {
        height: auto;
        margin-bottom: 1;
    }
    .ad-strategy {
        width: 1fr;
        height: auto;
    }
    #ad-switches {
        height: auto;
        margin-bottom: 1;
        border: round $surface-lighten-2;
        padding: 0 1;
    }
    .ad-switch-row {
        height: 3;
    }
    .ad-switch-cell {
        width: 1fr;
        height: 3;
    }
    .ad-switch-cell Label {
        padding-top: 1;
        width: auto;
        margin-right: 1;
    }
    .ad-switch-cell Switch {
        width: auto;
    }
    #ad-buttons {
        height: 3;
        align-horizontal: right;
    }
    #ad-buttons Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }
    """

    def __init__(
        self,
        result_file: str = "",
        default_strategy: str = "bfs",
        default_auto_search: bool = False,
        default_auto_evaluate: bool = False,
        default_auto_survey: bool = False,
        default_autoreplies: bool = True,
    ) -> None:
        super().__init__()
        self.result_file = result_file
        self.default_strategy = default_strategy
        self.default_auto_search = default_auto_search
        self.default_auto_evaluate = default_auto_evaluate
        self.default_auto_survey = default_auto_survey
        self.default_autoreplies = default_autoreplies

    def compose(self) -> textual.app.ComposeResult:
        with textual.containers.Vertical(id="ad-dialog"):
            yield textual.widgets.Static("Autodiscover", id="ad-title")
            yield textual.widgets.Static(
                "Autodiscover explores exits from nearby "
                "rooms "
                "that lead to unvisited places. It will "
                "travel "
                "to each frontier exit, check the room, "
                "then "
                "return before trying the next branch.",
                id="ad-body",
            )
            yield textual.widgets.Static(
                "WARNING: This can lead to dangerous "
                "areas, "
                "death traps, or aggressive monsters! "
                "Your "
                "character may die. Use with caution.",
                id="ad-warning",
            )
            yield textual.widgets.Static(
                "Tip: Use the Block button in the Rooms screen to exclude dangerous areas.", id="ad-hint"
            )
            with textual.containers.Horizontal(id="ad-strategy-row"), textual.widgets.RadioSet(id="ad-strategy-set"):
                yield textual.widgets.RadioButton(
                    "BFS: explore nearest exits first", id="ad-bfs", value=(self.default_strategy == "bfs")
                )
                yield textual.widgets.RadioButton(
                    "DFS: explore farthest exits first", id="ad-dfs", value=(self.default_strategy == "dfs")
                )
            with textual.containers.Vertical(id="ad-switches"):
                with textual.containers.Horizontal(classes="ad-switch-row"):
                    with textual.containers.Horizontal(classes="ad-switch-cell"):
                        yield textual.widgets.Label("Auto search:")
                        yield textual.widgets.Switch(value=(self.default_auto_search), id="ad-auto-search")
                    with textual.containers.Horizontal(classes="ad-switch-cell"):
                        yield textual.widgets.Label("Auto consider:")
                        yield textual.widgets.Switch(
                            value=(self.default_auto_evaluate),
                            id="ad-auto-consider",
                            disabled=(not self.default_autoreplies),
                        )
                with textual.containers.Horizontal(classes="ad-switch-row"):
                    with textual.containers.Horizontal(classes="ad-switch-cell"):
                        yield textual.widgets.Label("Auto survey:")
                        yield textual.widgets.Switch(
                            value=(self.default_auto_survey),
                            id="ad-auto-survey",
                            disabled=(not self.default_autoreplies),
                        )
                    with textual.containers.Horizontal(classes="ad-switch-cell"):
                        yield textual.widgets.Label("Autoreplies:")
                        yield textual.widgets.Switch(value=(self.default_autoreplies), id="ad-autoreplies")
            with textual.containers.Horizontal(id="ad-buttons"):
                yield textual.widgets.Button("Help", variant="primary", id="ad-help")
                yield textual.widgets.Button("Cancel", variant="error", id="ad-cancel")
                yield textual.widgets.Button("OK", variant="success", id="ad-ok")

    def on_mount(self) -> None:
        """Focus the OK button on mount."""
        self.query_one("#ad-ok", textual.widgets.Button).focus()

    def action_show_help(self) -> None:
        """Show room-mapping help screen."""
        self.app.push_screen(client_tui_base.CommandHelpScreen(topic="room-mapping"))

    def get_strategy(self) -> str:
        """Return the selected strategy string."""
        if self.query_one("#ad-dfs", textual.widgets.RadioButton).value:
            return "dfs"
        return "bfs"

    def on_switch_changed(self, event: textual.widgets.Switch.Changed) -> None:
        """Disable consider/survey switches when autoreplies is OFF."""
        if event.switch.id == "ad-autoreplies":
            self.query_one("#ad-auto-consider", textual.widgets.Switch).disabled = not event.value
            self.query_one("#ad-auto-survey", textual.widgets.Switch).disabled = not event.value

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle OK and Cancel button presses."""
        if event.button.id == "ad-help":
            self.action_show_help()
        elif event.button.id == "ad-ok":
            auto_search = self.query_one("#ad-auto-search", textual.widgets.Switch).value
            auto_evaluate = self.query_one("#ad-auto-consider", textual.widgets.Switch).value
            auto_survey = self.query_one("#ad-auto-survey", textual.widgets.Switch).value
            autoreplies = self.query_one("#ad-autoreplies", textual.widgets.Switch).value
            self.write_result(True, self.get_strategy(), auto_search, auto_evaluate, auto_survey, autoreplies)
            self.dismiss(True)
        elif event.button.id == "ad-cancel":
            self.write_result(
                False,
                self.default_strategy,
                self.default_auto_search,
                self.default_auto_evaluate,
                self.default_auto_survey,
                self.default_autoreplies,
            )
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Cancel the dialog and write default values."""
        self.write_result(
            False,
            self.default_strategy,
            self.default_auto_search,
            self.default_auto_evaluate,
            self.default_auto_survey,
            self.default_autoreplies,
        )
        self.dismiss(False)

    def write_result(
        self,
        confirmed: bool,
        strategy: str,
        auto_search: bool = False,
        auto_evaluate: bool = False,
        auto_survey: bool = False,
        autoreplies: bool = True,
    ) -> None:
        """Write result JSON to disk for the parent process."""
        if not self.result_file:
            return
        cmd = f"`autodiscover {strategy}"
        if auto_search:
            cmd += " autosearch"
        if auto_evaluate:
            cmd += " autoevaluate"
        if auto_survey:
            cmd += " autosurvey"
        if not autoreplies:
            cmd += " noreply"
        cmd += "`"
        result = json.dumps(
            {
                "confirmed": confirmed,
                "strategy": strategy,
                "auto_search": auto_search,
                "auto_evaluate": auto_evaluate,
                "auto_survey": auto_survey,
                "autoreplies": autoreplies,
                "command": cmd,
            }
        )
        with open(self.result_file, "w", encoding="utf-8") as f:
            f.write(result)


def autodiscover_dialog_main(
    result_file: str = "",
    default_strategy: str = "bfs",
    default_auto_search: str = "0",
    default_auto_evaluate: str = "0",
    default_auto_survey: str = "0",
    default_autoreplies: str = "1",
    logfile: str = "",
) -> None:
    """Launch standalone autodiscover dialog TUI."""
    client_tui_base.restore_blocking_fds(logfile)
    client_tui_base.log_child_diagnostics()
    client_tui_base.patch_writer_thread_queue()
    screen = AutodiscoverDialogScreen(
        result_file=result_file,
        default_strategy=default_strategy,
        default_auto_search=(default_auto_search == "1"),
        default_auto_evaluate=(default_auto_evaluate == "1"),
        default_auto_survey=(default_auto_survey == "1"),
        default_autoreplies=(default_autoreplies == "1"),
    )
    app = client_tui_base.EditorApp(screen)  # type: ignore[arg-type]
    client_tui_base.run_editor_app(app)


# ---------------------------------------------------------------------------
# Room editor entry point
# ---------------------------------------------------------------------------


def edit_rooms_main(
    rooms_path: str, session_key: str = "", current_room_file: str = "", fasttravel_file: str = "", logfile: str = ""
) -> None:
    """Launch standalone room browser TUI."""
    client_tui_base.launch_editor(
        RoomBrowserScreen(
            rooms_path=rooms_path,
            session_key=session_key,
            current_room_file=current_room_file,
            fasttravel_file=fasttravel_file,
        ),
        session_key=session_key,
        logfile=logfile,
    )


# ---------------------------------------------------------------------------
# Top-level session manager app
# ---------------------------------------------------------------------------


class TelnetSessionApp(textual.app.App[None]):
    """Textual TUI for managing telix client sessions."""

    TITLE = "telix Session Manager"

    def on_mouse_down(self, event: textual.events.MouseDown) -> None:
        """Paste X11 primary selection on middle-click."""
        if event.button != 2:
            return
        event.stop()
        text = client_tui_base.read_primary_selection()
        if not text:
            return
        focused = self.focused
        if focused is not None and hasattr(focused, "insert_text_at_cursor"):
            focused.insert_text_at_cursor(text)

    def set_pointer_shape(self, shape: str) -> None:
        """Disable pointer shape changes to prevent WriterThread deadlock."""

    def on_mount(self) -> None:
        """Push the session list screen on startup."""
        prefs = telix.rooms.load_prefs(client_tui_base.DEFAULTS_KEY)
        saved_theme = prefs.get("tui_theme")
        if isinstance(saved_theme, str) and saved_theme:
            self.theme = saved_theme
        else:
            self.theme = "gruvbox"
        self.push_screen(client_tui_base.SessionListScreen())

    def watch_theme(self, old: str, new: str) -> None:
        """Persist theme choice to global preferences."""
        if new:
            prefs = telix.rooms.load_prefs(client_tui_base.DEFAULTS_KEY)
            prefs["tui_theme"] = new
            telix.rooms.save_prefs(client_tui_base.DEFAULTS_KEY, prefs)


def tui_main() -> None:
    """Launch the Textual TUI session manager."""
    client_tui_base.patch_writer_thread_queue()
    app = TelnetSessionApp()
    try:
        app.run()
    except BaseException:
        import traceback

        client_tui_base.pause_before_exit()
        sys.stdout.write(client_tui_base.TERMINAL_CLEANUP)
        sys.stdout.flush()
        client_tui_base.restore_opost()
        traceback.print_exc()
        raise
    if app.return_code and app.return_code != 0:
        client_tui_base.pause_before_exit()
    # Move cursor to bottom-right corner and print a newline while still in
    # the alternate screen, then exit fullscreen and print another newline
    # so the shell prompt appears on a clean line.
    sys.stdout.write("\x1b[999;999H\n" + client_tui_base.TERMINAL_CLEANUP + "\n")
    sys.stdout.flush()
