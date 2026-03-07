"""Room browser, picker, and graph editor screens for the telix TUI."""

# std imports
import typing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rooms import RoomStore

# 3rd party
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

NAME_COL_BASE = 17
ID_COL_BASE = 10
BUTTON_COL_MIN = 20
BUTTON_COL_GROW = 15

BOOKMARK_STYLE = "$accent"
ARROW_STYLE = "$primary"
HOME_STYLE = "$accent"
BLOCKED_STYLE = "$error"
MARKED_STYLE = "$accent"


class RoomTree(textual.widgets.Tree[str]):
    """Room tree with aligned icon+arrow prefix columns."""

    ICON_NODE = "\u25c2 "
    ICON_NODE_EXPANDED = "\u25be "

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
        _ = tree._tree_lines
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
