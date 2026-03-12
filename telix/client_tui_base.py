"""
Foundation layer for the Textual TUI editor infrastructure.

Provides help panes, the abstract list-editor base classes, and the editor
app infrastructure used by all standalone editor entry points.  Session
management (SessionConfig, SessionListScreen, etc.) lives in
``client_tui_session_manager``.  No imports from other ``client_tui_*`` files
except ``client_tui_session_manager``.
"""

# std imports
import os
import abc
import typing
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# 3rd party
import textual.app
import textual.events
import textual.screen
import textual.binding
import textual.widgets
import textual.css.query
import textual.containers

# local
from . import rooms, terminal, client_tui_session_manager

log = logging.getLogger(__name__)

# Re-export constants and symbols used by other modules that import from client_tui_base.
TERMINAL_CLEANUP = client_tui_session_manager.TERMINAL_CLEANUP
PRIMARY_PASTE_COMMANDS = client_tui_session_manager.PRIMARY_PASTE_COMMANDS
ENCODINGS = client_tui_session_manager.ENCODINGS
DEFAULTS_KEY = client_tui_session_manager.DEFAULTS_KEY
BATCH_SIZE = client_tui_session_manager.BATCH_SIZE
FLAG_TO_WIDGET = client_tui_session_manager.FLAG_TO_WIDGET
TOOLTIP_CACHE = client_tui_session_manager.TOOLTIP_CACHE
SessionConfig = client_tui_session_manager.SessionConfig
SessionListScreen = client_tui_session_manager.SessionListScreen
ThemePickerScreen = client_tui_session_manager.ThemePickerScreen
SessionEditScreen = client_tui_session_manager.SessionEditScreen
int_val = client_tui_session_manager.int_val
float_val = client_tui_session_manager.float_val
read_primary_selection = client_tui_session_manager.read_primary_selection
normalize_encoding = client_tui_session_manager.normalize_encoding
build_tooltips = client_tui_session_manager.build_tooltips
navigate_from_button = client_tui_session_manager.navigate_from_button
navigate_from_table = client_tui_session_manager.navigate_from_table
handle_arrow_navigation = client_tui_session_manager.handle_arrow_navigation
ensure_dirs = client_tui_session_manager.ensure_dirs
load_sessions = client_tui_session_manager.load_sessions
save_sessions = client_tui_session_manager.save_sessions
build_command = client_tui_session_manager.build_command
build_telnet_command = client_tui_session_manager.build_telnet_command
build_ws_command = client_tui_session_manager.build_ws_command
build_ssh_command = client_tui_session_manager.build_ssh_command
relative_time = client_tui_session_manager.relative_time


def get_help_topic(topic: str) -> str:
    """Load help text for a TUI dialog topic from bundled markdown files."""
    from telix.help import get_help

    return get_help(topic)


class HelpPane(textual.containers.Vertical):
    """Widget containing help content -- embeddable in a tab or standalone screen."""

    DEFAULT_CSS = """
    HelpPane {
        width: 100%;
        height: 100%;
    }
    #help-dialog {
        width: 100%;
        height: 100%;
        background: $surface;
        padding: 0 1;
    }
    #help-scroll {
        height: 1fr;
    }
    #help-scroll Markdown {
        margin: 0 1;
    }
    """

    def __init__(self, topic: str = "macro") -> None:
        super().__init__()
        self.topic = topic

    def compose(self) -> textual.app.ComposeResult:
        content = get_help_topic(self.topic)
        with textual.containers.Vertical(id="help-dialog"), textual.containers.VerticalScroll(id="help-scroll"):
            yield textual.widgets.Markdown(content, id="help-content")

    def update_topic(self, topic: str) -> None:
        """
        Replace help content with a different topic.

        :param topic: Help topic key (e.g. ``"macro"``, ``"keybindings"``).
        """
        if topic == self.topic:
            return
        self.topic = topic
        content = get_help_topic(topic)
        try:
            md = self.query_one("#help-content", textual.widgets.Markdown)
            md.update(content)
        except textual.css.query.NoMatches:
            pass


class CommandHelpScreen(textual.screen.Screen[None]):
    """Scrollable help screen with context-specific documentation."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("escape", "close", "Exit"),
        textual.binding.Binding("q", "close", "Exit", show=False),
    ]

    def __init__(self, topic: str = "macro") -> None:
        super().__init__()
        self.pane = HelpPane(topic=topic)

    def compose(self) -> textual.app.ComposeResult:
        yield self.pane
        yield textual.widgets.Footer()

    def action_close(self) -> None:
        """Dismiss the help screen."""
        self.dismiss(None)


class EditListPane(textual.containers.Vertical):
    """Base pane for list-editor UIs (macros, triggers, etc.)."""

    DEFAULT_CSS = """
    EditListPane {
        width: 100%; height: 100%;
    }
    .edit-panel {
        width: 100%; height: 100%;
        border: round $surface-lighten-2; background: $surface; padding: 1 1;
    }
    .edit-body { height: 1fr; }
    .edit-button-col {
        width: 11; height: auto; padding-right: 1;
    }
    .edit-button-col Button {
        width: 100%; min-width: 0; margin-bottom: 0;
    }
    .edit-copy { background: $primary-lighten-1; }
    .edit-copy:hover { background: $primary-lighten-2; }
    .edit-right { width: 1fr; height: 100%; }
    .edit-search { height: auto; }
    .edit-table { height: 1fr; min-height: 4; overflow-x: hidden; }
    .edit-form { height: 1fr; }
    .edit-form .field-row { height: 3; margin: 0; }
    .edit-form Input { width: 1fr; border: tall grey; }
    .edit-form Input:focus { border: tall $accent; }
    .edit-form-buttons { height: 3; align-horizontal: right; }
    .edit-form-buttons Button { width: auto; min-width: 10; margin-left: 1; }
    .insert-btn { width: auto; min-width: 0; margin-left: 1; }
    .form-label { width: 8; padding-top: 1; }
    .form-label-short { width: 9; padding-top: 1; }
    .form-label-mid { width: 5; padding-top: 1; }
    .form-label-pct { width: 12; padding-top: 1; }
    .toggle-label { width: auto; padding-top: 1; content-align-horizontal: right; }
    .toggle-gap { width: 1fr; max-width: 6; }
    .form-gap { width: 2; }
    .form-gap-wide { width: 5; }
    .form-btn-spacer { width: 1; }
    """

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("escape", "cancel_or_close", "Cancel", priority=True),
        textual.binding.Binding("f1", "show_help", "Help", show=True),
        textual.binding.Binding("plus", "reorder_hint", "Change Priority", key_display="+/=/-", show=True),
        textual.binding.Binding("enter", "save_hint", "Save", show=True),
    ]

    @property
    @abc.abstractmethod
    def prefix(self) -> str: ...

    @property
    @abc.abstractmethod
    def noun(self) -> str:
        """Display noun for this editor, e.g. 'Macro' or 'Trigger'."""

    @property
    def noun_plural(self) -> str:
        """Plural form of :attr:`noun`; override for irregular plurals."""
        return self.noun + "s"

    @property
    @abc.abstractmethod
    def items(self) -> list[typing.Any]: ...

    def item_label(self, idx: int) -> str:
        """Return a display label for the item at *idx*."""
        return str(self.items[idx][0]) if idx < len(self.items) else ""

    growable_keys: list[str] = []
    """Column keys (from ``add_column(key=…)``) that should expand to fill space."""

    def __init__(self) -> None:
        super().__init__()
        self.editing_idx: int | None = None
        self.filtered_indices: list[int] = []
        self.search_query: str = ""

    def request_close(self, result: bool | None = None) -> None:
        """Dismiss the parent screen or exit the app."""
        try:
            self.screen.dismiss(result)
        except textual.app.ScreenStackError:
            self.app.exit()

    @property
    def form_visible(self) -> bool:
        return bool(self.query_one(f"#{self.prefix}-form").display)

    def fit_growable_columns(self) -> None:
        """Distribute remaining table width equally among growable columns."""
        table = self.query_one(f"#{self.prefix}-table", textual.widgets.DataTable)
        avail = table.size.width
        if avail <= 0:
            return
        pad = table.cell_padding
        fixed_total = 0
        growable: list[typing.Any] = []
        for col in table.ordered_columns:
            if str(col.key) in self.growable_keys:
                growable.append(col)
            else:
                fixed_total += col.get_render_width(table)
        if not growable:
            return
        remaining = max(avail - fixed_total - 2, len(growable))
        each = remaining // len(growable)
        for col in growable:
            col.auto_width = False
            col.width = max(each - 2 * pad, 4)
        table.refresh()

    def on_resize(self, event: textual.events.Resize) -> None:
        """Recompute growable column widths on terminal resize."""
        if self.growable_keys:
            self.call_after_refresh(self.fit_growable_columns)

    def set_action_buttons_disabled(self, disabled: bool) -> None:
        """Enable or disable the add/edit/copy buttons."""
        pfx = self.prefix
        for suffix in ("add", "edit", "copy"):
            self.query_one(f"#{pfx}-{suffix}", textual.widgets.Button).disabled = disabled

    def focus_default(self) -> None:
        """Focus the list table."""
        self.query_one(f"#{self.prefix}-table", textual.widgets.DataTable).focus()

    def hide_form(self) -> None:
        pfx = self.prefix
        self.query_one(f"#{pfx}-form").display = False
        self.query_one(f"#{pfx}-table").display = True
        try:
            self.query_one(f"#{pfx}-search", textual.widgets.Input).display = True
        except textual.css.query.NoMatches:
            pass
        self.editing_idx = None
        self.set_action_buttons_disabled(False)
        self.query_one(f"#{pfx}-table", textual.widgets.DataTable).focus()

    def finalize_edit(self, entry: typing.Any, is_valid: bool) -> None:
        """Insert or update an item, refresh, and hide the form."""
        if is_valid:
            if self.editing_idx is not None:
                self.items[self.editing_idx] = entry
                target_row = self.editing_idx
            else:
                target_row = len(self.items)
                self.items.append(entry)
            self.refresh_table()
            table = self.query_one(f"#{self.prefix}-table", textual.widgets.DataTable)
            table.move_cursor(row=target_row)
        self.hide_form()

    def selected_idx(self) -> int | None:
        table = self.query_one(f"#{self.prefix}-table", textual.widgets.DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        row_pos = int(str(row_key.value))
        if self.filtered_indices:
            if row_pos < len(self.filtered_indices):
                return self.filtered_indices[row_pos]
            return None
        return row_pos

    def edit_selected(self) -> None:
        idx = self.selected_idx()
        if idx is not None and idx < len(self.items):
            self.editing_idx = idx
            self.show_form(*self.items[idx])

    def copy_selected(self) -> None:
        idx = self.selected_idx()
        if idx is not None and idx < len(self.items):
            self.items.insert(idx + 1, self.items[idx])
            self.refresh_table()
            table = self.query_one(f"#{self.prefix}-table", textual.widgets.DataTable)
            table.move_cursor(row=idx + 1)

    def reorder(self, move_down: bool) -> None:
        idx = self.selected_idx()
        if idx is None:
            return
        items = self.items
        target = idx + 1 if move_down else idx - 1
        if target < 0 or target >= len(items):
            return
        items[idx], items[target] = items[target], items[idx]
        self.refresh_table()
        table = self.query_one(f"#{self.prefix}-table", textual.widgets.DataTable)
        table.move_cursor(row=target)

    def on_input_submitted(self, event: textual.widgets.Input.Submitted) -> None:
        """Submit the form when Enter is pressed in an input field."""
        if self.form_visible:
            event.stop()
            self.submit_form()

    def action_cancel_or_close(self) -> None:
        """Cancel form editing or close the screen."""
        if self.form_visible:
            self.hide_form()
        else:
            self.request_close(None)

    def action_reorder_hint(self) -> None:
        """Placeholder for reorder key binding hint."""

    def action_save_hint(self) -> None:
        """Placeholder for save key binding hint."""

    def action_show_help(self) -> None:
        """Open the context-sensitive help screen."""
        self.app.push_screen(CommandHelpScreen(topic=self.prefix))

    def matches_search(self, idx: int, query: str) -> bool:
        """Return True if item at *idx* matches the search *query*."""
        return True

    def on_input_changed(self, event: textual.widgets.Input.Changed) -> None:
        """Filter table when search input changes."""
        if event.input.id == f"{self.prefix}-search":
            self.search_query = event.value
            self.refresh_table()

    def on_key(self, event: textual.events.Key) -> None:
        """Arrow/Home/End/+/- keys navigate and reorder the table."""
        pfx = self.prefix
        search_id = f"#{pfx}-search"
        try:
            search_input = self.query_one(search_id, textual.widgets.Input)
        except textual.css.query.NoMatches:
            search_input = None

        if search_input is not None and event.key in ("up", "down"):
            table = self.query_one(f"#{pfx}-table", textual.widgets.DataTable)
            if self.screen.focused is search_input and event.key == "down":
                table.focus()
                event.prevent_default()
                return
            if self.screen.focused is table and event.key == "up" and table.cursor_row == 0:
                search_input.focus()
                event.prevent_default()
                return

        if event.key in ("home", "end"):
            table = self.query_one(f"#{self.prefix}-table", textual.widgets.DataTable)
            if self.screen.focused is table and table.row_count > 0:
                row = 0 if event.key == "home" else table.row_count - 1
                table.move_cursor(row=row)
                event.prevent_default()
        elif event.key in ("up", "down", "left", "right"):
            client_tui_session_manager.handle_arrow_navigation(
                self.screen, event, f"#{self.prefix}-button-col", f"#{self.prefix}-table", f"#{self.prefix}-form"
            )
        elif event.key in ("plus", "minus", "equals_sign") and not self.form_visible:
            self.reorder(event.key in ("plus", "equals_sign"))

    def on_data_table_row_selected(self, event: textual.widgets.DataTable.RowSelected) -> None:
        """Double-click or Enter on a table row opens it for editing."""
        row_pos = int(str(event.row_key.value))
        if self.filtered_indices:
            if row_pos < len(self.filtered_indices):
                idx = self.filtered_indices[row_pos]
            else:
                return
        else:
            idx = row_pos
        if idx < len(self.items):
            self.editing_idx = idx
            self.show_form(*self.items[idx])

    def action_add(self) -> None:
        self.editing_idx = None
        self.show_form()

    def action_delete(self) -> None:
        from .client_tui_dialogs import ConfirmDialogScreen

        if self.form_visible:
            self.hide_form()
        idx = self.selected_idx()
        if idx is not None and idx < len(self.items):
            label = self.item_label(idx)
            safe_idx: int = idx

            def do_confirm(confirmed: bool, idx: int = safe_idx) -> None:
                if confirmed and idx < len(self.items):
                    self.items.pop(idx)
                    self.refresh_table()

            self.app.push_screen(
                ConfirmDialogScreen(
                    title=f"Delete {self.noun}", body=f"Delete {self.noun.lower()} '{label}'?", show_dont_ask=False
                ),
                callback=do_confirm,  # type: ignore[arg-type]
            )

    def action_ok(self) -> None:
        if self.form_visible:
            self.submit_form()

    def action_save(self) -> None:
        if self.form_visible:
            self.submit_form()
        self.save_to_file()
        self.request_close(True)

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Handle common list-editor button presses."""
        btn = event.button.id or ""
        pfx = self.prefix
        suffix = btn.removeprefix(pfx + "-") if btn.startswith(pfx + "-") else ""
        handlers: dict[str, typing.Any] = {
            "add": self.action_add,
            "edit": self.edit_selected,
            "copy": self.copy_selected,
            "delete": self.action_delete,
            "ok": self.action_ok,
            "cancel-form": self.hide_form,
            "save": self.action_save,
            "close": lambda: self.request_close(None),
            "help": lambda: self.app.push_screen(CommandHelpScreen(topic=self.prefix)),
        }
        handler = handlers.get(suffix)
        if handler:
            handler()
        elif suffix:
            self.do_extra_button(suffix, btn)

    @property
    def text_input_id(self) -> str:
        """ID of the text/reply Input widget for command insertion."""
        return f"{self.prefix}-text"

    def insert_command(self, cmd: str) -> None:
        """Insert a command at the cursor position, adding separators."""
        if self.form_visible:
            widget_id = f"#{self.text_input_id}"
            try:
                ta = self.query_one(widget_id, textual.widgets.TextArea)
                ta.insert(("\n" if ta.text.strip() else "") + cmd)
                return
            except textual.css.query.QueryError:
                pass
            inp = self.query_one(widget_id, textual.widgets.Input)
            val = inp.value
            pos = inp.cursor_position
            before = val[:pos]
            after = val[pos:]
            if before and not before.endswith(";"):
                cmd = ";" + cmd
            if after and not after.startswith(";"):
                cmd = cmd + ";"
            inp.value = before + cmd + after
            inp.cursor_position = len(before) + len(cmd)
        else:
            self.editing_idx = None
            self.show_form()

    rooms_path: str = ""
    current_room_path: str = ""
    session_key: str = ""

    def pick_room_for_travel(self) -> None:
        """Open room picker and insert a travel command."""
        from .client_tui_rooms import RoomPickerScreen

        rooms_file = self.rooms_path
        if not rooms_file or not os.path.exists(rooms_file):
            return

        def do_pick(room_id: str | None) -> None:
            if room_id is None:
                return
            cmd = f"`travel {room_id}`"
            self.insert_command(cmd)

        kwargs: dict[str, str] = {"rooms_path": rooms_file, "session_key": self.session_key}
        if self.current_room_path:
            kwargs["current_room_file"] = self.current_room_path
        self.app.push_screen(RoomPickerScreen(**kwargs), callback=do_pick)

    COMMAND_BUTTONS: typing.ClassVar[dict[str, str]] = {
        "btn-when": "`when hp%>=99`",
        "btn-until": "`until 10 pattern`",
        "btn-delay": "`delay 1s`",
        "delay": "`delay 1s`",
        "btn-randomwalk": "`randomwalk`",
        "return": "`return`",
        "autodiscover": "`autodiscover`",
    }

    def do_extra_button(self, suffix: str, btn: str) -> None:
        """Handle shared command-builder buttons; override for extras."""
        cmd = self.COMMAND_BUTTONS.get(suffix)
        if cmd is not None:
            self.insert_command(cmd)

    @abc.abstractmethod
    def show_form(self, *args: typing.Any) -> None: ...

    @abc.abstractmethod
    def submit_form(self) -> None: ...

    @abc.abstractmethod
    def refresh_table(self) -> None: ...

    def update_count_label(self) -> None:
        """Update the count label and refit growable columns after refresh."""
        n_total = len(self.items)
        n_shown = len(self.filtered_indices)
        noun = self.noun_plural
        label = self.query_one(f"#{self.prefix}-count", textual.widgets.Static)
        if self.search_query:
            label.update(f"{n_shown:,}/{n_total:,} {noun}")
        else:
            label.update(f"{n_total:,} {noun}")
        self.call_after_refresh(self.fit_growable_columns)

    @abc.abstractmethod
    def save_to_file(self) -> None: ...


class EditListScreen(textual.screen.Screen["bool | None"]):
    """Thin screen wrapper around an ``EditListPane``."""

    @property
    def pane(self) -> EditListPane:
        """Return the pane widget -- subclasses set ``self.__pane`` in __init__."""
        return self.__pane

    @pane.setter
    def pane(self, value: EditListPane) -> None:
        self.__pane = value

    def compose(self) -> textual.app.ComposeResult:
        yield self.pane
        yield textual.widgets.Footer()

    def on_mount(self) -> None:
        """Focus the pane's default widget after layout."""
        self.call_after_refresh(self.pane.focus_default)


class EditorApp(textual.app.App[None]):
    """Minimal Textual app for standalone macro/trigger editing."""

    def __init__(self, screen: textual.screen.Screen[bool | None], session_key: str = "") -> None:
        """Initialize with the editor screen to push."""
        super().__init__()
        self.editor_screen = screen
        self.session_key = session_key

    def on_mouse_down(self, event: textual.events.MouseDown) -> None:
        """Paste X11 primary selection on middle-click."""
        if event.button != 2:
            return
        event.stop()
        text = client_tui_session_manager.read_primary_selection()
        if not text:
            return
        focused = self.focused
        if focused is not None and hasattr(focused, "insert_text_at_cursor"):
            focused.insert_text_at_cursor(text)

    def set_pointer_shape(self, shape: str) -> None:
        """
        Disable pointer shape changes to prevent WriterThread deadlock.

        Textual writes escape sequences to set cursor shape on mouse move.
        When the PTY output buffer is full, ``WriterThread.write()`` blocks,
        and the bounded queue causes ``queue.put()`` to block the main
        asyncio thread, freezing the entire app.
        """

    def on_mount(self) -> None:
        """Push the editor screen."""
        saved_theme = ""
        if self.session_key:
            prefs = rooms.load_prefs(self.session_key)
            saved_theme = prefs.get("tui_theme", "")  # type: ignore[assignment]
        if not saved_theme:
            saved_theme = rooms.load_prefs(client_tui_session_manager.DEFAULTS_KEY).get("tui_theme", "")  # type: ignore[assignment]
        if isinstance(saved_theme, str) and saved_theme:
            self.theme = saved_theme
        else:
            self.theme = "gruvbox"
        self.push_screen(self.editor_screen, callback=lambda _: self.exit())

    def watch_theme(self, old: str, new: str) -> None:
        """Persist theme choice to per-session and global preferences."""
        if not new:
            return
        save_key = self.session_key or client_tui_session_manager.DEFAULTS_KEY
        prefs = rooms.load_prefs(save_key)
        prefs["tui_theme"] = new
        rooms.save_prefs(save_key, prefs)


def patch_writer_thread_queue() -> None:
    """
    Make Textual's WriterThread queue unbounded.

    Textual's ``WriterThread`` uses a bounded queue (``maxsize=30``).
    When terminal output processing lags behind rapid re-renders
    (e.g. clicking between widgets), ``queue.put()`` blocks the main
    asyncio thread, freezing the entire app.  Setting the constant
    to 0 (unbounded) before the ``WriterThread`` is instantiated
    prevents the deadlock.
    """
    import textual.drivers._writer_thread as wt

    wt.MAX_QUEUED_WRITES = 0  # type: ignore[misc]


def restore_blocking_fds(logfile: str = "") -> None:
    """
    Restore blocking mode on stdin/stdout/stderr.

    The parent process may set ``O_NONBLOCK`` on the shared PTY file
    description (via asyncio ``connect_read_pipe``).
    Since stdin, stdout, and stderr all reference the same kernel file
    description, the child subprocess inherits non-blocking mode.
    Textual's ``WriterThread`` does not handle ``BlockingIOError``,
    so a non-blocking stderr causes the thread to die silently,
    freezing the app.

    :param logfile: Optional path to the parent's logfile for child logging.
    """
    terminal.restore_blocking_fds(logfile)


def log_child_diagnostics() -> None:
    """Log environment and terminal diagnostics in the child subprocess."""
    log = logging.getLogger(__name__)
    env_keys = ("TERM", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE")
    env = {k: os.environ.get(k, "") for k in env_keys}
    try:
        tsize = os.get_terminal_size()
        tsize_str = f"{tsize.columns}x{tsize.lines}"
    except OSError:
        tsize_str = "?"

    def fd_blocking(fd: int) -> bool | None:
        try:
            return os.get_blocking(fd)
        except OSError:
            return None

    log.debug(
        "child env: %s terminal_size=%s fd0_blocking=%s fd2_blocking=%s", env, tsize_str, fd_blocking(0), fd_blocking(2)
    )
    os.environ["TEXTUAL_DEBUG"] = "1"


def launch_editor(screen: textual.screen.Screen[typing.Any], session_key: str = "", logfile: str = "") -> None:
    """Common bootstrap for standalone editor entry points."""
    restore_blocking_fds(logfile)
    log_child_diagnostics()
    patch_writer_thread_queue()
    EditorApp(screen, session_key=session_key).run()


def launch_editor_in_thread(screen: textual.screen.Screen[typing.Any], session_key: str = "") -> None:
    """
    Bootstrap for editor launch from an in-process worker thread.

    The calling thread's :func:`~telix.terminal_unix.blocking_fds` context manager already
    ensures blocking FDs, so :func:`restore_blocking_fds` is not called here.  Exceptions
    propagate naturally to the thread wrapper in the REPL; crash-hook installation is omitted.
    On non-zero exit the return code is logged instead of calling :func:`sys.exit`.

    :param screen: Textual screen to wrap in an :class:`EditorApp`.
    :param session_key: Session key forwarded to :class:`EditorApp`.
    """
    log_child_diagnostics()
    patch_writer_thread_queue()
    EditorApp(screen, session_key=session_key).run()
