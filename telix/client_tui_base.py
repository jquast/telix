"""
Foundation layer for the Textual TUI session manager.

Provides imports, constants, helpers, session configuration, the session
list/edit screens, the abstract list-editor base, help panes, and the
editor app infrastructure.  No imports from other ``client_tui_*`` files.
"""

from __future__ import annotations

# std imports
import os
import sys
import json
import logging
import datetime
import subprocess
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar
from dataclasses import asdict, fields, dataclass

if TYPE_CHECKING:
    from textual.widget import Widget

# 3rd party
from textual import events
from textual.app import App, ComposeResult, ScreenStackError
from textual.screen import Screen
from textual.binding import Binding
from textual.widgets import (
    Input,
    Label,
    Button,
    Footer,
    Select,
    Static,
    Switch,
    Markdown,
    RadioSet,
    DataTable,
    RadioButton,
    ContentSwitcher,
)
from textual.css.query import NoMatches
from textual.containers import Vertical, Horizontal, VerticalScroll

# Reset SGR, cursor, alt-screen, mouse, and bracketed paste,
# then move cursor home and clear the screen so tracebacks start clean.
TERMINAL_CLEANUP = (
    "\x1b[m\x1b[?25h\x1b[?1049l"
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l"
    "\x1b[H\x1b[2J"
)


def restore_opost() -> None:
    r"""
    Ensure the terminal OPOST flag is set so ``\\n`` maps to ``\\r\\n``.

    Textual puts the terminal in raw mode which disables output post-processing.  If the driver
    fails to fully restore termios (or we catch an exception before it gets the chance), newlines
    render as bare LF producing staircase output.
    """
    import termios

    try:
        fd = sys.stdout.fileno()
        attrs = termios.tcgetattr(fd)
        if not (attrs[1] & termios.OPOST):
            attrs[1] |= termios.OPOST
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (OSError, termios.error, ValueError, AttributeError):
        pass


PRIMARY_PASTE_COMMANDS = (
    ("xclip", "-selection", "primary", "-o"),
    ("xsel", "--primary", "--output"),
    ("wl-paste", "--primary", "--no-newline"),
)


def read_primary_selection() -> str:
    """Read text from the X11/Wayland primary selection via external helper."""
    for cmd in PRIMARY_PASTE_COMMANDS:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=2, check=False)
            if result.returncode == 0:
                return result.stdout.decode("utf-8", errors="replace")
        except FileNotFoundError:
            continue
    return ""


ENCODINGS = (
    "utf8",
    "cp437",
    "latin-1",
    "ascii",
    "iso-8859-1",
    "iso-8859-2",
    "iso-8859-15",
    "cp1251",
    "koi8-r",
    "big5",
    "gbk",
    "euc-kr",
    "shift-jis",
    "atascii",
    "petscii",
)

# local
from .paths import DATA_DIR, CONFIG_DIR, SESSIONS_FILE  # noqa: E402

DEFAULTS_KEY = "__defaults__"
BATCH_SIZE = 100


# Map CLI flag names (without leading --) to TUI widget IDs.
FLAG_TO_WIDGET: dict[str, str] = {
    "term": "term",
    "encoding": "encoding",
    "encoding-errors": "encoding-errors",
    "force-binary": "force-binary",
    "raw-mode": "mode-raw",
    "line-mode": "mode-line",
    "connect-timeout": "connect-timeout",
    "send-environ": "send-environ",
    "always-will": "always-will",
    "always-do": "always-do",
    "colormatch": "colormatch",
    "background-color": "background-color",
    "ice-colors": "ice-colors",
    "ascii-eol": "ascii-eol",
    "ansi-keys": "ansi-keys",
    "ssl": "ssl",
    "ssl-no-verify": "ssl-no-verify",
    "no-repl": "use-repl",
    "loglevel": "loglevel",
    "logfile": "logfile",
    "typescript": "typescript",
}


def handle_arrow_navigation(
    screen: Screen,
    event: events.Key,
    button_col_selector: str,
    table_selector: str,
    form_selector: str = "",
) -> None:
    """
    Arrow key navigation between a button column, data table, and form.

    :param screen: The screen handling the key event.
    :param event: The key event.
    :param button_col_selector: CSS selector for the button column container.
    :param table_selector: CSS selector for the DataTable.
    :param form_selector: CSS selector for the inline form (optional).
    """
    focused = screen.focused
    buttons = list(screen.query(f"{button_col_selector} Button"))
    table = screen.query_one(table_selector, DataTable)

    # When the form is visible, handle navigation within form fields.
    if form_selector:
        try:
            form = screen.query_one(form_selector)
        except NoMatches:
            form = None
        if form is not None and form.display:
            form_fields: list[Input | Switch | Button] = [
                w
                for w in form.query("Input, Switch, Button")
                if isinstance(w, (Input, Switch, Button))
            ]
            if focused in form_fields:
                idx = form_fields.index(focused)
                if event.key == "up" and idx > 0:
                    form_fields[idx - 1].focus()
                    event.prevent_default()
                elif event.key == "down" and idx < len(form_fields) - 1:
                    form_fields[idx + 1].focus()
                    event.prevent_default()
                elif event.key == "left" and isinstance(focused, (Switch, Button)):
                    if buttons:
                        screen.call_later(buttons[0].focus)
                    event.prevent_default()
                return
            if isinstance(focused, Button) and focused in buttons:
                if event.key == "right" and form_fields:
                    screen.call_later(form_fields[0].focus)
                    event.prevent_default()
                    return

    if isinstance(focused, Input):
        return

    if isinstance(focused, Button) and focused in buttons:
        idx = buttons.index(focused)
        if event.key == "up" and idx > 0:
            buttons[idx - 1].focus()
            event.prevent_default()
        elif event.key == "down" and idx < len(buttons) - 1:
            buttons[idx + 1].focus()
            event.prevent_default()
        elif event.key == "right":
            screen.call_later(table.focus)
            event.prevent_default()
    elif focused is table and event.key == "left":
        if buttons:
            screen.call_later(buttons[0].focus)
        event.prevent_default()


TOOLTIP_CACHE: dict[str, str] | None = None


def build_tooltips() -> dict[str, str]:
    """Extract help text from argparse and return ``{widget_id: help}``."""
    global TOOLTIP_CACHE
    if TOOLTIP_CACHE is not None:
        return TOOLTIP_CACHE
    from telnetlib3.client import _get_argument_parser

    parser = _get_argument_parser()
    tips: dict[str, str] = {}
    for action in parser._actions:
        if not action.help:
            continue
        for opt in action.option_strings:
            flag = opt.lstrip("-")
            widget_id = FLAG_TO_WIDGET.get(flag)
            if widget_id:
                tips[widget_id] = action.help
    TOOLTIP_CACHE = tips
    return tips


@dataclass
class SessionConfig:
    """
    Persistent configuration for a single telnet session.

    Field defaults mirror the CLI defaults in
    :func:`telnetlib3.client.get_argument_parser`.
    """

    # Metadata
    name: str = ""
    last_connected: str = ""

    # Connection
    host: str = ""
    port: int = 23
    ssl: bool = False
    ssl_cafile: str = ""
    ssl_no_verify: bool = False

    # Terminal
    term: str = ""  # empty = use $TERM at runtime
    speed: int = 38400
    encoding: str = "utf8"
    force_binary: bool = True
    encoding_errors: str = "replace"

    # Mode: "auto", "raw", or "line"
    mode: str = "auto"

    # Display
    colormatch: str = "vga"
    color_brightness: float = 1.0
    color_contrast: float = 1.0
    background_color: str = "#000000"
    ice_colors: bool = True

    # Input
    ansi_keys: bool = False
    ascii_eol: bool = False

    # Negotiation
    connect_minwait: float = 0.0
    connect_maxwait: float = 4.0
    connect_timeout: float = 10.0

    # Environment
    send_environ: str = "TERM,LANG,COLUMNS,LINES,COLORTERM"

    # Compression: None = passive (accept if offered), True = request, False = reject
    compression: bool | None = None

    # Advanced
    always_will: str = ""  # comma-separated option names
    always_do: str = ""
    loglevel: str = "warn"
    logfile: str = ""
    typescript: str = ""
    no_repl: bool = False

    # Bookmarked sessions sort to top of the list
    bookmarked: bool = False


def ensure_dirs() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def load_sessions() -> dict[str, SessionConfig]:
    """Load session configs from ``~/.config/telix/sessions.json``."""
    ensure_dirs()
    if not os.path.exists(SESSIONS_FILE):
        return {}
    with open(SESSIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    known = {f.name for f in fields(SessionConfig)}
    result: dict[str, SessionConfig] = {}
    for key, val in data.items():
        filtered = {k: v for k, v in val.items() if k in known}
        result[key] = SessionConfig(**filtered)
    return result


def save_sessions(sessions: dict[str, SessionConfig]) -> None:
    """Save session configs to ``~/.config/telix/sessions.json``."""
    from .paths import atomic_write

    ensure_dirs()
    data = {key: asdict(cfg) for key, cfg in sessions.items()}
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write(str(SESSIONS_FILE), content)


CMD_STR_FLAGS: list[tuple[str, str, object]] = [
    ("term", "--term", ""),
    ("encoding", "--encoding", "utf8"),
    ("speed", "--speed", 38400),
    ("encoding_errors", "--encoding-errors", "replace"),
    ("colormatch", "--colormatch", "vga"),
    ("color_brightness", "--color-brightness", 1.0),
    ("color_contrast", "--color-contrast", 1.0),
    ("background_color", "--background-color", "#000000"),
    ("connect_minwait", "--connect-minwait", 0.0),
    ("connect_maxwait", "--connect-maxwait", 4.0),
    ("send_environ", "--send-environ", "TERM,LANG,COLUMNS,LINES,COLORTERM"),
    ("loglevel", "--loglevel", "warn"),
    ("logfile", "--logfile", ""),
    ("typescript", "--typescript", ""),
    ("ssl_cafile", "--ssl-cafile", ""),
]

CMD_BOOL_FLAGS: list[tuple[str, str, bool]] = [
    ("ssl", "--ssl", False),
    ("ssl_no_verify", "--ssl-no-verify", False),
    ("no_repl", "--no-repl", False),
    ("ansi_keys", "--ansi-keys", False),
    ("ascii_eol", "--ascii-eol", False),
]

CMD_NEG_BOOL_FLAGS: list[tuple[str, str, bool]] = [("ice_colors", "--no-ice-colors", True)]


def build_command(config: SessionConfig) -> list[str]:
    """
    Build ``telnetlib3-client`` CLI arguments from *config*.

    Only emits flags that differ from the CLI defaults.
    """
    cmd = [
        sys.executable,
        "-c",
        "from telnetlib3.client import main; main()",
        config.host,
        str(config.port),
    ]

    if not config.no_repl:
        cmd.extend(["--shell", "telix.client_shell.telix_client_shell"])

    for attr, flag, default in CMD_STR_FLAGS:
        val = getattr(config, attr)
        if val != default:
            cmd.extend([flag, str(val)])

    if config.mode == "raw":
        cmd.append("--raw-mode")
    elif config.mode == "line":
        cmd.append("--line-mode")

    for attr, flag, default in CMD_BOOL_FLAGS:
        if getattr(config, attr) != default:
            cmd.append(flag)

    for attr, flag, default in CMD_NEG_BOOL_FLAGS:
        if getattr(config, attr) != default:
            cmd.append(flag)

    if config.compression is True:
        cmd.append("--compression")
    elif config.compression is False:
        cmd.append("--no-compression")

    if config.connect_timeout > 0 and config.connect_timeout != 10.0:
        cmd.extend(["--connect-timeout", str(config.connect_timeout)])

    for attr, flag in (("always_will", "--always-will"), ("always_do", "--always-do")):
        for opt in getattr(config, attr).split(","):
            if opt := opt.strip():
                cmd.extend([flag, opt])

    return cmd


def relative_time(iso_str: str) -> str:
    """Return a short relative-time string like ``'5m ago'`` or ``'3d ago'``."""
    from .util import relative_time

    return relative_time(iso_str)


class SessionListScreen(Screen[None]):
    """Main screen: table of saved sessions with action buttons."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit_app", "Quit"),
        Binding("n", "new_session", "New"),
        Binding("e", "edit_session", "Edit"),
        Binding("b", "toggle_bookmark", "Bookmark"),
        Binding("d", "delete_session", "Delete"),
        Binding("enter", "connect", "Connect"),
        Binding("f1", "show_help", "Help"),
    ]

    CSS = """
    SessionListScreen {
        align: center top;
    }
    #session-panel {
        width: 100%;
        height: 100%;
        background: $surface;
        padding: 0 1;
    }
    #session-body {
        height: 1fr;
    }
    #session-search { height: auto; margin-bottom: 0; }
    #session-table {
        width: 1fr;
        height: 100%;
        min-height: 5;
        overflow-x: hidden;
    }
    #button-col {
        width: 12;
        height: auto;
        padding-right: 1;
    }
    #button-col Button {
        width: 100%;
        min-width: 0;
        margin-bottom: 0;
    }
    #bookmark-btn {
        background: $accent;
        color: $text;
        text-style: none;
    }
    #bookmark-btn:hover {
        background: $accent-lighten-1;
        color: $text;
        text-style: none;
    }
    """

    def __init__(self) -> None:
        """Initialize session list with empty session dict."""
        super().__init__()
        self.sessions: dict[str, SessionConfig] = {}
        self.pending_rows: list[tuple[str, SessionConfig]] = []
        self.refresh_gen: int = 0

    def compose(self) -> ComposeResult:
        """Build the session list layout."""
        with Vertical(id="session-panel"):
            yield Input(placeholder="Search sessions\u2026", id="session-search")
            with Horizontal(id="session-body"):
                with Vertical(id="button-col"):
                    yield Button("Connect", variant="primary", id="connect-btn")
                    yield Button("New", variant="success", id="add-btn")
                    yield Button("Bookmark", variant="default", id="bookmark-btn")
                    yield Button("Delete", variant="error", id="delete-btn")
                    yield Button("Edit", variant="warning", id="edit-btn")
                yield DataTable(id="session-table")
        yield Footer()

    def on_mount(self) -> None:
        """Load sessions and populate the data table."""
        self.sessions = load_sessions()
        if not self.sessions:
            from .directory import directory_to_sessions

            self.sessions = directory_to_sessions()
            save_sessions(self.sessions)
        table = self.query_one("#session-table", DataTable)
        table.cursor_type = "row"
        table.add_column(" ", width=4, key="icon")
        table.add_columns("Host/Name", "Port", "Enc", "Last", "Flags")
        self.refresh_table()
        if table.row_count > 0:
            table.focus()

    @staticmethod
    def flags(cfg: SessionConfig) -> str:
        """Return short flag codes summarizing non-default session options."""
        parts: list[str] = []
        if cfg.ssl:
            parts.append("ssl")
        if cfg.mode == "raw":
            parts.append("raw")
        elif cfg.mode == "line":
            parts.append("line")
        if not cfg.force_binary:
            parts.append("!bin")
        if cfg.ansi_keys:
            parts.append("ansi")
        if cfg.ascii_eol:
            parts.append("eol")
        if not cfg.ice_colors:
            parts.append("!ice")
        if cfg.no_repl:
            parts.append("!repl")
        if cfg.typescript:
            parts.append("ts")
        return " ".join(parts)

    def add_rows(self, table: DataTable, items: list[tuple[str, SessionConfig]]) -> None:
        """Add a list of ``(key, cfg)`` pairs as rows to *table*."""
        for key, cfg in items:
            table.add_row(
                "\u2021" if cfg.bookmarked else "",
                cfg.name or cfg.host,
                str(cfg.port),
                cfg.encoding,
                relative_time(cfg.last_connected),
                self.flags(cfg),
                key=key,
            )

    def refresh_table(self, search: str = "") -> None:
        """Rebuild the session table, loading rows in batches."""
        self.refresh_gen += 1
        gen = self.refresh_gen
        table = self.query_one("#session-table", DataTable)
        table.clear()
        needle = search.strip().lower()
        items = [
            (key, cfg)
            for key, cfg in self.sessions.items()
            if key != DEFAULTS_KEY
            and (not needle or needle in f"{cfg.name} {cfg.host} {cfg.port} {cfg.encoding}".lower())
        ]
        # Bookmarked first, then most recently connected first, then name.
        items.sort(key=lambda kc: (kc[1].name or kc[1].host).lower())
        items.sort(key=lambda kc: kc[1].last_connected or "", reverse=True)
        items.sort(key=lambda kc: not kc[1].bookmarked)
        first, rest = items[:BATCH_SIZE], items[BATCH_SIZE:]
        self.add_rows(table, first)
        self.pending_rows = rest
        if rest:
            self.call_later(self.load_next_batch, gen)

    def load_next_batch(self, gen: int) -> None:
        """Add the next batch of rows; bail if a newer refresh has started."""
        if gen != self.refresh_gen:
            return
        table = self.query_one("#session-table", DataTable)
        batch, self.pending_rows = (self.pending_rows[:BATCH_SIZE], self.pending_rows[BATCH_SIZE:])
        self.add_rows(table, batch)
        if self.pending_rows:
            self.call_later(self.load_next_batch, gen)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter session table when search input changes."""
        if event.input.id == "session-search":
            self.refresh_table(event.value)

    def save(self) -> None:
        save_sessions(self.sessions)

    def session_keys(self) -> list[str]:
        return [k for k in self.sessions if k != DEFAULTS_KEY]

    def selected_key(self) -> str | None:
        table = self.query_one("#session-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return str(row_key.value)

    def on_key(self, event: events.Key) -> None:
        """Arrow/Home/End keys navigate between search, buttons, and the table."""
        search_input = self.query_one("#session-search", Input)
        table = self.query_one("#session-table", DataTable)

        if event.key in ("up", "down"):
            if self.focused is search_input and event.key == "down":
                table.focus()
                event.prevent_default()
                return
            if self.focused is table and event.key == "up" and table.cursor_row == 0:
                search_input.focus()
                event.prevent_default()
                return

        if event.key in ("home", "end"):
            if self.focused is table and table.row_count > 0:
                row = 0 if event.key == "home" else table.row_count - 1
                table.move_cursor(row=row)
                event.prevent_default()
        elif event.key in ("up", "down", "left", "right"):
            handle_arrow_navigation(self, event, "#button-col", "#session-table")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch button press to the appropriate action."""
        handlers = {
            "connect-btn": self.action_connect,
            "add-btn": self.action_new_session,
            "bookmark-btn": self.action_toggle_bookmark,
            "edit-btn": self.action_edit_session,
            "delete-btn": self.action_delete_session,
            "quit-btn": self.action_quit_app,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Connect on double-click or Enter."""
        self.action_connect()

    def action_quit_app(self) -> None:
        """Exit the application."""
        self.app.exit()

    def action_new_session(self) -> None:
        """Open editor for a new session pre-filled with defaults."""
        defaults = self.sessions.get(DEFAULTS_KEY, SessionConfig())
        new_cfg = SessionConfig(**asdict(defaults))
        new_cfg.name = ""
        new_cfg.host = ""
        new_cfg.last_connected = ""
        self.app.push_screen(
            SessionEditScreen(config=new_cfg, is_new=True), callback=self.do_edit_result
        )

    def require_selected(self) -> str | None:
        """Return selected session key, or notify and return ``None``."""
        key = self.selected_key()
        if key is None:
            self.notify("No session selected", severity="warning")
        return key

    def action_edit_session(self) -> None:
        """Open editor for the selected session."""
        old_key = self.require_selected()
        if old_key is None:
            return
        cfg = self.sessions[old_key]

        def do_edit(config: SessionConfig | None) -> None:
            if config is None:
                return
            new_key = config.name or config.host
            if not new_key:
                return
            if new_key != old_key and old_key in self.sessions:
                del self.sessions[old_key]
            self.sessions[new_key] = config
            self.save()
            self.refresh_table()
            self.select_row(new_key)

        self.app.push_screen(SessionEditScreen(config=cfg), callback=do_edit)

    def action_delete_session(self) -> None:
        """Delete the selected session after confirmation."""
        from .client_tui_dialogs import ConfirmDialogScreen

        key = self.require_selected()
        if key is None:
            return

        def do_confirm(confirmed: bool) -> None:
            if confirmed:
                del self.sessions[key]
                self.save()
                self.refresh_table()
                self.notify(f"Deleted {key}")

        self.app.push_screen(
            ConfirmDialogScreen(title="Delete Session", body=f"Delete session '{key}'?"),
            callback=do_confirm,
        )

    def action_toggle_bookmark(self) -> None:
        """Toggle bookmark on the selected session and re-sort."""
        key = self.require_selected()
        if key is None:
            return
        cfg = self.sessions[key]
        cfg.bookmarked = not cfg.bookmarked
        self.save()
        self.refresh_table()
        self.select_row(key)

    def action_show_help(self) -> None:
        """Open the session manager help screen."""
        self.app.push_screen(CommandHelpScreen(topic="session"))

    def action_connect(self) -> None:
        """Launch a telnet connection to the selected session."""
        key = self.require_selected()
        if key is None:
            return
        cfg = self.sessions[key]
        if not cfg.host:
            self.notify("No host configured", severity="error")
            return

        cfg.last_connected = datetime.datetime.now().isoformat()
        self.save()

        cmd = build_command(cfg)
        with self.app.suspend():
            # Move to bottom-right and print newline so the TUI
            # scrolls cleanly off screen before the client starts.
            tsize = os.get_terminal_size()
            sys.stdout.write(f"\x1b[{tsize.lines};{tsize.columns}H\r\n")
            sys.stdout.flush()
            proc = None
            try:
                # stderr must NOT be piped -- the child may launch
                # Textual subprocesses (F8/F9 editors) that write all
                # output to sys.__stderr__.  A piped stderr would send
                # that output into the pipe instead of the terminal,
                # hanging the editor.
                proc = subprocess.Popen(cmd)
                proc.wait()
            except KeyboardInterrupt:
                if proc is not None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            finally:
                # The child process shares the kernel file description
                # for stdin/stdout.  asyncio's connect_read_pipe sets
                # O_NONBLOCK on the shared description, which persists
                # after the child exits.  Textual's input loop expects
                # blocking reads -- restore before Textual resumes.
                os.set_blocking(sys.stdin.fileno(), True)
                # Reset terminal to known-good state -- the child may
                # have left raw mode, SGR attributes, mouse tracking,
                # or alternate screen active.
                sys.stdout.write(TERMINAL_CLEANUP)
                sys.stdout.flush()
        self.refresh_table()
        self.select_row(key)

    def do_edit_result(self, config: SessionConfig | None) -> None:
        if config is None:
            return
        key = config.name or config.host
        if not key:
            return
        self.sessions[key] = config
        self.save()
        self.refresh_table()
        self.select_row(key)

    def select_row(self, key: str) -> None:
        """Move the table cursor to the row with the given key."""
        table = self.query_one("#session-table", DataTable)
        for row_idx, row_key in enumerate(table.rows):
            if str(row_key.value) == key:
                table.move_cursor(row=row_idx)
                break


class SessionEditScreen(Screen[SessionConfig | None]):  # type: ignore[misc]
    """Full-screen form for adding or editing a session."""

    CSS = """
    SessionEditScreen {
        align: center middle;
    }
    #edit-panel {
        width: 100%;
        max-width: 65;
        height: 100%;
        max-height: 26;
        border: round $surface-lighten-2;
        background: $surface;
        padding: 1 1;
    }
    #tab-bar {
        height: 1;
        margin-bottom: 1;
    }
    #tab-bar Button {
        min-width: 0;
        height: 1;
        margin: 0 1 0 0;
        border: none;
        background: $surface-lighten-1;
    }
    #tab-bar Button.active-tab {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    #tab-content {
        height: 1fr;
    }
    .tab-pane {
        height: auto;
    }
    .field-row {
        height: 3;
        margin-bottom: 0;
    }
    .field-label {
        width: 14;
        padding-top: 1;
    }
    .field-label-short {
        width: auto;
        padding-top: 1;
        margin-left: 1;
        margin-right: 1;
    }
    .field-input {
        width: 1fr;
    }
    .switch-row {
        height: 3;
    }
    #host-port-sep {
        width: 1;
        padding-top: 1;
    }
    #port {
        max-width: 14;
    }
    #ssl-compress-row {
        height: auto;
    }
    #server-type-col {
        width: auto;
        max-width: 17;
        height: auto;
    }
    #compression-col {
        width: auto;
        max-width: 19;
        height: auto;
    }
    #ssl-timeout-col {
        width: 1fr;
        height: auto;
        padding-left: 2;
    }
    #ssl-label {
        width: 11;
        padding-top: 1;
    }
    #timeout-label {
        width: 11;
        padding-top: 1;
    }
    #connect-timeout {
        max-width: 13;
    }
    #mode-repl-row {
        height: auto;
    }
    #mode-col {
        width: auto;
        max-width: 25;
        height: auto;
    }
    #repl-col {
        width: 1fr;
        height: auto;
        padding-top: 1;
        padding-left: 4;
    }
    #keys-eol-row {
        height: 3;
    }
    .dimmed {
        color: $text-muted;
    }
    #enc-label {
        width: 10;
        padding-top: 1;
    }
    #enc-errors-label {
        width: 12;
        padding-top: 1;
        padding-left: 4;
    }
    #encoding {
        max-width: 20;
    }
    #encoding-errors {
        max-width: 15;
    }
    #background-color {
        max-width: 12;
    }
    #colormatch {
        max-width: 14;
    }
    #palette-preview {
        width: 1fr;
        padding-top: 1;
    }
    #bottom-bar {
        height: 3;
        margin-top: 1;
    }
    #save-btn {
        dock: right;
    }
    #bottom-bar Button {
        margin-right: 1;
    }
    """

    def __init__(
        self, config: SessionConfig, is_defaults: bool = False, is_new: bool = False
    ) -> None:
        """Initialize edit screen with session config and mode flags."""
        super().__init__()
        self.config = config
        self.is_defaults = is_defaults
        self.is_new = is_new

    TAB_IDS: ClassVar[list[tuple[str, str]]] = [
        ("Connection", "tab-connection"),
        ("Terminal", "tab-terminal"),
        ("Display", "tab-display"),
        ("Advanced", "tab-advanced"),
    ]

    @staticmethod
    def field_row(label: str, *widgets: Widget, row_class: str = "field-row") -> Horizontal:
        """Return a ``Horizontal`` row with a label and widgets."""
        return Horizontal(Label(label, classes="field-label"), *widgets, classes=row_class)

    def compose_connection_tab(self, cfg: SessionConfig) -> ComposeResult:
        """Yield widgets for the Connection tab pane."""
        if not self.is_defaults:
            yield self.field_row(
                "Name",
                Input(
                    value=cfg.name,
                    placeholder="optional display name",
                    id="name",
                    classes="field-input",
                ),
            )
            yield Horizontal(
                Label("Host:Port", classes="field-label"),
                Input(value=cfg.host, placeholder="hostname", id="host", classes="field-input"),
                Static(":", id="host-port-sep"),
                Input(value=str(cfg.port), placeholder="23", id="port"),
                classes="field-row",
            )
        with Horizontal(id="ssl-compress-row"):
            with Vertical(id="server-type-col"):
                yield Label("Server Type")
                with RadioSet(id="server-type-radio"):
                    yield RadioButton("BBS", id="type-bbs")
                    yield RadioButton("MUD", id="type-mud")
            with Vertical(id="compression-col"):
                yield Label("MCCP Compression")
                with RadioSet(id="compression-radio"):
                    yield RadioButton(
                        "Passive", value=cfg.compression is None, id="compress-passive"
                    )
                    yield RadioButton("Yes", value=cfg.compression is True, id="compress-yes")
                    yield RadioButton("No", value=cfg.compression is False, id="compress-no")
            with Vertical(id="ssl-timeout-col"):
                with Horizontal(classes="switch-row"):
                    yield Label("SSL/TLS", id="ssl-label")
                    yield Switch(value=cfg.ssl, id="ssl")
                with Horizontal(classes="switch-row"):
                    yield Label("Timeout", id="timeout-label")
                    yield Input(value=str(cfg.connect_timeout), id="connect-timeout")

    def compose_terminal_tab(self, cfg: SessionConfig) -> ComposeResult:
        """Yield widgets for the Terminal tab pane."""
        yield self.field_row(
            "TERM",
            Input(
                value=cfg.term,
                placeholder=os.environ.get("TERM", "unknown"),
                id="term",
                classes="field-input",
            ),
        )
        with Horizontal(id="mode-repl-row"):
            with Vertical(id="mode-col"):
                yield Label("Terminal Mode")
                with RadioSet(id="mode-radio"):
                    yield RadioButton("Auto-detect", value=cfg.mode == "auto", id="mode-auto")
                    yield RadioButton("Raw mode", value=cfg.mode == "raw", id="mode-raw")
                    yield RadioButton("Line mode", value=cfg.mode == "line", id="mode-line")
            with Vertical(id="repl-col"), Horizontal(classes="switch-row"):
                repl_dim = "" if cfg.mode != "raw" else " dimmed"
                yield Label("Advanced REPL", id="repl-label", classes=f"field-label{repl_dim}")
                yield Switch(value=not cfg.no_repl, id="use-repl", disabled=cfg.mode == "raw")
        enc = cfg.encoding or "utf-8"
        is_retro = enc.lower() in ("atascii", "petscii")
        with Horizontal(classes="field-row"):
            yield Label("Encoding", id="enc-label")
            yield Select(
                [(e, e) for e in ENCODINGS],
                value=enc if enc in ENCODINGS else "utf-8",
                id="encoding",
                allow_blank=False,
            )
            yield Label("Errors", id="enc-errors-label")
            yield Select(
                [(v, v) for v in ("replace", "ignore", "strict")],
                value=cfg.encoding_errors,
                id="encoding-errors",
            )
        dim = "" if is_retro else " dimmed"
        with Horizontal(id="keys-eol-row"):
            with Horizontal(classes="switch-row"):
                yield Label("ANSI Keys", id="ansi-keys-label", classes=f"field-label{dim}")
                yield Switch(value=cfg.ansi_keys, id="ansi-keys", disabled=not is_retro)
            with Horizontal(classes="switch-row"):
                yield Label("ASCII EOL", id="ascii-eol-label", classes=f"field-label{dim}")
                yield Switch(value=cfg.ascii_eol, id="ascii-eol", disabled=not is_retro)

    def compose_display_tab(self, cfg: SessionConfig) -> ComposeResult:
        """Yield widgets for the Display tab pane."""
        yield Horizontal(
            Label("Color Palette", classes="field-label"),
            Select(
                [(v, v) for v in ("vga", "xterm", "none")], value=cfg.colormatch, id="colormatch"
            ),
            Static("", id="palette-preview"),
            classes="field-row",
        )
        with Horizontal(classes="switch-row"):
            yield Label("iCE Colors", classes="field-label")
            yield Switch(value=cfg.ice_colors, id="ice-colors")

    def compose_advanced_tab(self, cfg: SessionConfig) -> ComposeResult:
        """Yield widgets for the Advanced tab pane."""
        yield self.field_row(
            "Send Environ", Input(value=cfg.send_environ, id="send-environ", classes="field-input")
        )
        yield Horizontal(
            Label("Log Level:", classes="field-label"),
            Select(
                [(v, v) for v in ("trace", "debug", "info", "warn", "error", "critical")],
                value=cfg.loglevel,
                id="loglevel",
            ),
            Label("file:", classes="field-label-short"),
            Input(value=cfg.logfile, placeholder="path", id="logfile", classes="field-input"),
            classes="field-row",
        )
        yield self.field_row(
            "Typescript",
            Input(value=cfg.typescript, placeholder="path", id="typescript", classes="field-input"),
        )

    def compose(self) -> ComposeResult:
        """Build the tabbed session editor layout."""
        cfg = self.config
        with Vertical(id="edit-panel"):
            with Horizontal(id="tab-bar"):
                for i, (label, tab_id) in enumerate(self.TAB_IDS):
                    btn = Button(label, id=f"tabbtn-{tab_id}")
                    if i == 0:
                        btn.add_class("active-tab")
                    yield btn

            with ContentSwitcher(id="tab-content", initial="tab-connection"):
                with Vertical(id="tab-connection", classes="tab-pane"):
                    yield from self.compose_connection_tab(cfg)
                with Vertical(id="tab-terminal", classes="tab-pane"):
                    yield from self.compose_terminal_tab(cfg)
                with Vertical(id="tab-display", classes="tab-pane"):
                    yield from self.compose_display_tab(cfg)
                with Vertical(id="tab-advanced", classes="tab-pane"):
                    yield from self.compose_advanced_tab(cfg)

            with Horizontal(id="bottom-bar"):
                yield Button("Cancel", variant="error", id="cancel-btn")
                yield Button("Save", variant="success", id="save-btn")

    def on_mount(self) -> None:
        """Apply argparse-derived tooltips to form widgets."""
        tips = build_tooltips()
        for widget_id, help_text in tips.items():
            try:
                widget = self.query_one(f"#{widget_id}")
                widget.tooltip = help_text
            except NoMatches:
                pass
        self.update_palette_preview()
        for radio_set in self.query(RadioSet):
            idx = radio_set.pressed_index
            if idx >= 0:
                radio_set._selected = idx

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Handle radio-set changes for server type and terminal mode."""
        if event.radio_set.id == "server-type-radio":
            self.apply_server_type(event.pressed.id)
        elif event.radio_set.id == "mode-radio":
            is_raw = event.pressed.id == "mode-raw"
            repl_switch = self.query_one("#use-repl", Switch)
            repl_switch.disabled = is_raw
            self.query_one("#repl-label", Label).set_class(is_raw, "dimmed")

    def select_radio(self, radio_set_id: str, button_id: str) -> None:
        """Select a radio button by deselecting all siblings first."""
        radio_set = self.query_one(f"#{radio_set_id}", RadioSet)
        for btn in radio_set.query(RadioButton):
            btn.value = False
        self.query_one(f"#{button_id}", RadioButton).value = True

    def apply_server_type(self, button_id: str) -> None:
        """Apply preset field values for BBS or MUD server type."""
        if button_id == "type-bbs":
            self.query_one("#colormatch", Select).value = "vga"
            self.query_one("#ice-colors", Switch).value = True
            self.select_radio("mode-radio", "mode-raw")
            self.query_one("#use-repl", Switch).value = False
            self.query_one("#use-repl", Switch).disabled = True
            self.query_one("#repl-label", Label).set_class(True, "dimmed")
            self.select_radio("compression-radio", "compress-passive")
            self.update_palette_preview()
            self.notify(
                "BBS: Color Palette vga, iCE Colors on, Raw mode,"
                " REPL off, MCCP Compression passive"
            )
        elif button_id == "type-mud":
            self.select_radio("compression-radio", "compress-yes")
            self.select_radio("mode-radio", "mode-line")
            self.query_one("#use-repl", Switch).value = True
            self.query_one("#use-repl", Switch).disabled = False
            self.query_one("#repl-label", Label).set_class(False, "dimmed")
            self.query_one("#colormatch", Select).value = "none"
            self.query_one("#ice-colors", Switch).value = False
            self.update_palette_preview()
            self.notify(
                "MUD: MCCP Compression yes, Line mode,"
                " REPL on, Color Palette none, iCE Colors off"
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        """React to Select widget changes."""
        if event.select.id == "colormatch":
            self.update_palette_preview()
        elif event.select.id == "encoding":
            is_retro = str(event.value).lower() in ("atascii", "petscii")
            self.query_one("#ansi-keys", Switch).disabled = not is_retro
            self.query_one("#ascii-eol", Switch).disabled = not is_retro
            for label_id in ("#ansi-keys-label", "#ascii-eol-label"):
                label = self.query_one(label_id, Label)
                label.set_class(not is_retro, "dimmed")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Update palette preview when ice_colors changes."""
        if event.switch.id == "ice-colors":
            self.update_palette_preview()

    def update_palette_preview(self) -> None:
        """Render CP437 full-block color preview for the selected palette."""
        from telnetlib3.color_filter import PALETTES

        palette_name = self.query_one("#colormatch", Select).value
        preview = self.query_one("#palette-preview", Static)
        if palette_name == "none" or palette_name not in PALETTES:
            preview.update("")
            return
        palette = PALETTES[palette_name]
        ice = self.query_one("#ice-colors", Switch).value
        block = "\u2588"
        fg_blocks = "".join(f"[rgb({r},{g},{b})]{block}[/]" for r, g, b in palette)
        bg_count = 16 if ice else 8
        bg_blocks = "".join(f"[on rgb({r},{g},{b})] [/]" for r, g, b in palette[:bg_count])
        preview.update(f"FG: {fg_blocks}\nBG: {bg_blocks}")

    def switch_to_tab(self, tab_id: str) -> None:
        """Activate the given tab and update button styling."""
        self.query_one("#tab-content", ContentSwitcher).current = tab_id
        for btn in self.query("#tab-bar Button"):
            btn.remove_class("active-tab")
            if btn.id == f"tabbtn-{tab_id}":
                btn.add_class("active-tab")

    def active_tab_focusables(self) -> list[Any]:
        """Return focusable widgets in the currently visible tab pane."""
        current = self.query_one("#tab-content", ContentSwitcher).current
        if not current:
            return []
        pane = self.query_one(f"#{current}")
        return [w for w in pane.query("Input, Select, Switch, RadioButton") if not w.disabled]

    def on_key(self, event: events.Key) -> None:
        """Arrow key navigation for tabs, fields, and buttons."""
        focused = self.focused
        tab_buttons = list(self.query("#tab-bar Button"))
        bottom_buttons = list(self.query("#bottom-bar Button"))

        if focused in tab_buttons:
            idx = tab_buttons.index(focused)
            target = None
            if event.key == "left" and idx > 0:
                target = tab_buttons[idx - 1]
            elif event.key == "right" and idx < len(tab_buttons) - 1:
                target = tab_buttons[idx + 1]
            if target is not None:
                target.focus()
                if tab_id := (target.id or "").replace("tabbtn-", ""):
                    self.switch_to_tab(tab_id)
                event.prevent_default()
            elif event.key == "down":
                focusables = self.active_tab_focusables()
                if focusables:
                    focusables[0].focus()
                event.prevent_default()
            return

        if focused in bottom_buttons:
            idx = bottom_buttons.index(focused)
            if event.key == "left" and idx > 0:
                bottom_buttons[idx - 1].focus()
                event.prevent_default()
            elif event.key == "right" and idx < len(bottom_buttons) - 1:
                bottom_buttons[idx + 1].focus()
                event.prevent_default()
            elif event.key == "up":
                focusables = self.active_tab_focusables()
                if focusables:
                    focusables[-1].focus()
                event.prevent_default()
            return

        focusables = self.active_tab_focusables()
        if focused in focusables:
            idx = focusables.index(focused)
            if event.key == "up":
                if idx > 0:
                    focusables[idx - 1].focus()
                else:
                    current = self.query_one("#tab-content", ContentSwitcher).current
                    for btn in tab_buttons:
                        if btn.id == f"tabbtn-{current}":
                            btn.focus()
                            break
                event.prevent_default()
            elif event.key == "down":
                if idx < len(focusables) - 1:
                    focusables[idx + 1].focus()
                elif bottom_buttons:
                    bottom_buttons[0].focus()
                event.prevent_default()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle save, cancel, and tab switching buttons."""
        btn_id = event.button.id or ""
        if btn_id == "save-btn":
            self.do_save()
        elif btn_id == "cancel-btn":
            self.dismiss(None)
        elif btn_id.startswith("tabbtn-"):
            tab_id = btn_id[len("tabbtn-") :]
            self.switch_to_tab(tab_id)

    def do_save(self) -> None:
        config = self.collect_config()
        self.dismiss(config)

    def collect_config(self) -> SessionConfig:
        """Read all widget values back into a :class:`SessionConfig`."""
        cfg = SessionConfig()

        if not self.is_defaults:
            cfg.name = self.query_one("#name", Input).value.strip()
            cfg.host = self.query_one("#host", Input).value.strip()
            cfg.port = int_val(self.query_one("#port", Input).value, 23)
        else:
            cfg.name = DEFAULTS_KEY

        cfg.ssl = self.query_one("#ssl", Switch).value
        cfg.ssl_no_verify = False

        cfg.last_connected = self.config.last_connected

        cfg.term = self.query_one("#term", Input).value.strip()
        cfg.encoding = self.query_one("#encoding", Select).value
        cfg.encoding_errors = self.query_one("#encoding-errors", Select).value

        if self.query_one("#mode-raw", RadioButton).value:
            cfg.mode = "raw"
        elif self.query_one("#mode-line", RadioButton).value:
            cfg.mode = "line"
        else:
            cfg.mode = "auto"

        cfg.ansi_keys = self.query_one("#ansi-keys", Switch).value
        cfg.ascii_eol = self.query_one("#ascii-eol", Switch).value

        cfg.colormatch = self.query_one("#colormatch", Select).value
        cfg.background_color = "#000000"
        cfg.ice_colors = self.query_one("#ice-colors", Switch).value

        cfg.connect_timeout = float_val(self.query_one("#connect-timeout", Input).value, 10.0)

        if self.query_one("#compress-yes", RadioButton).value:
            cfg.compression = True
        elif self.query_one("#compress-no", RadioButton).value:
            cfg.compression = False
        else:
            cfg.compression = None

        cfg.send_environ = (
            self.query_one("#send-environ", Input).value.strip()
            or "TERM,LANG,COLUMNS,LINES,COLORTERM"
        )
        cfg.always_will = self.config.always_will
        cfg.always_do = self.config.always_do
        cfg.loglevel = self.query_one("#loglevel", Select).value
        cfg.logfile = self.query_one("#logfile", Input).value.strip()
        cfg.typescript = self.query_one("#typescript", Input).value.strip()
        cfg.no_repl = not self.query_one("#use-repl", Switch).value

        return cfg


def int_val(text: str, default: int) -> int:
    try:
        return int(text.strip())
    except (ValueError, TypeError):
        return default


def float_val(text: str, default: float) -> float:
    try:
        return float(text.strip())
    except (ValueError, TypeError):
        return default


def get_help_topic(topic: str) -> str:
    """Load help text for a TUI dialog topic from bundled markdown files."""
    from telix.help import get_help  # deferred to avoid import cost

    return get_help(topic)


class HelpPane(Vertical):
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

    def compose(self) -> ComposeResult:
        content = get_help_topic(self.topic)
        with Vertical(id="help-dialog"), VerticalScroll(id="help-scroll"):
            yield Markdown(content, id="help-content")

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
            md = self.query_one("#help-content", Markdown)
            md.update(content)
        except NoMatches:
            pass


class CommandHelpScreen(Screen[None]):
    """Scrollable help screen with context-specific documentation."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Exit"),
        Binding("q", "close", "Exit", show=False),
    ]

    def __init__(self, topic: str = "macro") -> None:
        super().__init__()
        self.pane = HelpPane(topic=topic)

    def compose(self) -> ComposeResult:
        yield self.pane
        yield Footer()

    def action_close(self) -> None:
        """Dismiss the help screen."""
        self.dismiss(None)


class EditListPane(Vertical):
    """Base pane for list-editor UIs (macros, autoreplies, etc.)."""

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

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_or_close", "Cancel", priority=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("plus", "reorder_hint", "Change Priority", key_display="+/=/-", show=True),
        Binding("enter", "save_hint", "Save", show=True),
    ]

    @property
    @abstractmethod
    def prefix(self) -> str: ...

    @property
    @abstractmethod
    def noun(self) -> str:
        """Display noun for this editor, e.g. 'Macro' or 'Autoreply'."""

    @property
    def noun_plural(self) -> str:
        """Plural form of :attr:`noun`; override for irregular plurals."""
        return self.noun + "s"

    @property
    @abstractmethod
    def items(self) -> list[Any]: ...

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
        except ScreenStackError:
            self.app.exit()

    @property
    def form_visible(self) -> bool:
        return bool(self.query_one(f"#{self.prefix}-form").display)

    def fit_growable_columns(self) -> None:
        """Distribute remaining table width equally among growable columns."""
        table = self.query_one(f"#{self.prefix}-table", DataTable)
        avail = table.size.width
        if avail <= 0:
            return
        pad = table.cell_padding
        fixed_total = 0
        growable: list[Any] = []
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

    def on_resize(self, event: events.Resize) -> None:
        """Recompute growable column widths on terminal resize."""
        if self.growable_keys:
            self.call_after_refresh(self.fit_growable_columns)

    def set_action_buttons_disabled(self, disabled: bool) -> None:
        """Enable or disable the add/edit/copy buttons."""
        pfx = self.prefix
        for suffix in ("add", "edit", "copy"):
            self.query_one(f"#{pfx}-{suffix}", Button).disabled = disabled

    def hide_form(self) -> None:
        pfx = self.prefix
        self.query_one(f"#{pfx}-form").display = False
        self.query_one(f"#{pfx}-table").display = True
        try:
            self.query_one(f"#{pfx}-search", Input).display = True
        except NoMatches:
            pass
        self.editing_idx = None
        self.set_action_buttons_disabled(False)
        self.query_one(f"#{pfx}-table", DataTable).focus()

    def finalize_edit(self, entry: Any, is_valid: bool) -> None:
        """Insert or update an item, refresh, and hide the form."""
        if is_valid:
            if self.editing_idx is not None:
                self.items[self.editing_idx] = entry
                target_row = self.editing_idx
            else:
                target_row = len(self.items)
                self.items.append(entry)
            self.refresh_table()
            self.query_one(f"#{self.prefix}-table", DataTable).move_cursor(row=target_row)
        self.hide_form()

    def selected_idx(self) -> int | None:
        table = self.query_one(f"#{self.prefix}-table", DataTable)
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
            table = self.query_one(f"#{self.prefix}-table", DataTable)
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
        table = self.query_one(f"#{self.prefix}-table", DataTable)
        table.move_cursor(row=target)

    def on_input_submitted(self, event: Input.Submitted) -> None:
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

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter table when search input changes."""
        if event.input.id == f"{self.prefix}-search":
            self.search_query = event.value
            self.refresh_table()

    def on_key(self, event: events.Key) -> None:
        """Arrow/Home/End/+/- keys navigate and reorder the table."""
        pfx = self.prefix
        search_id = f"#{pfx}-search"
        try:
            search_input = self.query_one(search_id, Input)
        except NoMatches:
            search_input = None

        if search_input is not None and event.key in ("up", "down"):
            table = self.query_one(f"#{pfx}-table", DataTable)
            if self.screen.focused is search_input and event.key == "down":
                table.focus()
                event.prevent_default()
                return
            if self.screen.focused is table and event.key == "up" and table.cursor_row == 0:
                search_input.focus()
                event.prevent_default()
                return

        if event.key in ("home", "end"):
            table = self.query_one(f"#{self.prefix}-table", DataTable)
            if self.screen.focused is table and table.row_count > 0:
                row = 0 if event.key == "home" else table.row_count - 1
                table.move_cursor(row=row)
                event.prevent_default()
        elif event.key in ("up", "down", "left", "right"):
            handle_arrow_navigation(
                self.screen,
                event,
                f"#{self.prefix}-button-col",
                f"#{self.prefix}-table",
                f"#{self.prefix}-form",
            )
        elif event.key in ("plus", "minus", "equals_sign") and not self.form_visible:
            self.reorder(event.key in ("plus", "equals_sign"))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
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
                    title=f"Delete {self.noun}",
                    body=f"Delete {self.noun.lower()} '{label}'?",
                    show_dont_ask=False,
                ),
                callback=do_confirm,
            )

    def action_ok(self) -> None:
        if self.form_visible:
            self.submit_form()

    def action_save(self) -> None:
        if self.form_visible:
            self.submit_form()
        self.save_to_file()
        self.request_close(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle common list-editor button presses."""
        btn = event.button.id or ""
        pfx = self.prefix
        suffix = btn.removeprefix(pfx + "-") if btn.startswith(pfx + "-") else ""
        handlers: dict[str, Any] = {
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
        """Insert a command at the cursor position, adding ``;`` separators."""
        if self.form_visible:
            inp = self.query_one(f"#{self.text_input_id}", Input)
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

    def pick_room_for_travel(self) -> None:
        """Open room picker and insert a travel command."""
        from .client_tui_dialogs import RoomPickerScreen

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

    COMMAND_BUTTONS: ClassVar[dict[str, str]] = {
        "btn-when": "`when HP%>=99`",
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

    @abstractmethod
    def show_form(self, *args: Any) -> None: ...

    @abstractmethod
    def submit_form(self) -> None: ...

    @abstractmethod
    def refresh_table(self) -> None: ...

    def update_count_label(self) -> None:
        """Update the count label and refit growable columns after refresh."""
        n_total = len(self.items)
        n_shown = len(self.filtered_indices)
        noun = self.noun_plural
        label = self.query_one(f"#{self.prefix}-count", Static)
        if self.search_query:
            label.update(f"{n_shown:,}/{n_total:,} {noun}")
        else:
            label.update(f"{n_total:,} {noun}")
        self.call_after_refresh(self.fit_growable_columns)

    @abstractmethod
    def save_to_file(self) -> None: ...


class EditListScreen(Screen["bool | None"]):
    """Thin screen wrapper around an ``EditListPane``."""

    @property
    def pane(self) -> EditListPane:
        """Return the pane widget -- subclasses set ``self.__pane`` in __init__."""
        return self.__pane

    @pane.setter
    def pane(self, value: EditListPane) -> None:
        self.__pane = value

    def compose(self) -> ComposeResult:
        yield self.pane
        yield Footer()


# ---------------------------------------------------------------------------
# Editor app infrastructure -- used by all standalone editor entry points.
# ---------------------------------------------------------------------------


class EditorApp(App[None]):
    """Minimal Textual app for standalone macro/autoreply editing."""

    def __init__(self, screen: Screen[bool | None], session_key: str = "") -> None:
        """Initialize with the editor screen to push."""
        super().__init__()
        self.editor_screen = screen
        self.session_key = session_key

    def print_error_renderables(self) -> None:
        r"""
        Print error tracebacks to stdout after alt screen exit.

        Textual's default writes to ``error_console`` (stderr).  In the
        telix subprocess stderr may not translate ``\\n`` to ``\\r\\n``
        correctly, producing staircase output.  Writing to a fresh
        stdout-based Rich console avoids the issue.
        """
        if not self.exit_renderables:
            return
        from rich.console import Console

        console = Console(file=sys.stdout, markup=False, highlight=False)
        for renderable in self.exit_renderables:
            console.print(renderable)
        self.exit_renderables.clear()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Paste X11 primary selection on middle-click."""
        if event.button != 2:
            return
        event.stop()
        text = read_primary_selection()
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
        log = logging.getLogger(__name__)
        driver = self.driver
        log.debug(
            "EditorApp mounted: driver.mouse=%s input_tty=%s "
            "driver._file=%r driver.fileno=%s "
            "driver._mouse_pixels=%s driver._in_band_window_resize=%s",
            getattr(driver, "mouse", "?"),
            getattr(driver, "input_tty", "?"),
            getattr(driver, "file", "?"),
            getattr(driver, "fileno", "?"),
            getattr(driver, "mouse_pixels", "?"),
            getattr(driver, "in_band_window_resize", "?"),
        )
        from .rooms import load_prefs

        saved_theme = ""
        if self.session_key:
            prefs = load_prefs(self.session_key)
            saved_theme = prefs.get("tui_theme", "")
        if not saved_theme:
            saved_theme = load_prefs(DEFAULTS_KEY).get("tui_theme", "")
        if isinstance(saved_theme, str) and saved_theme:
            self.theme = saved_theme
        else:
            self.theme = "gruvbox"
        self.push_screen(self.editor_screen, callback=lambda _: self.exit())

    def watch_theme(self, old: str, new: str) -> None:
        """Persist theme choice to per-session and global preferences."""
        if not new:
            return
        from .rooms import load_prefs, save_prefs

        save_key = self.session_key or DEFAULTS_KEY
        prefs = load_prefs(save_key)
        prefs["tui_theme"] = new
        save_prefs(save_key, prefs)


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
    try:
        import textual.drivers.writer_thread as wt

        wt.MAX_QUEUED_WRITES = 0
    except (ImportError, AttributeError):
        pass


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
    import os as os
    import sys as sys
    import logging as logging

    if logfile:
        logging.basicConfig(
            filename=logfile,
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        )

    log = logging.getLogger(__name__)
    log.debug(
        "child pre-fix: fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s __stdin___isatty=%s "
        "stderr_isatty=%s __stderr___isatty=%s",
        os.get_blocking(0),
        os.get_blocking(1),
        os.get_blocking(2),
        sys.stdin.isatty(),
        sys.__stdin__.isatty(),
        sys.stderr.isatty(),
        sys.__stderr__.isatty(),
    )
    for fd in (0, 1, 2):
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass
    log.debug(
        "child post-fix: fd0_blocking=%s fd1=%s fd2=%s",
        os.get_blocking(0),
        os.get_blocking(1),
        os.get_blocking(2),
    )


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
    log.debug(
        "child env: %s terminal_size=%s fd0_blocking=%s fd2_blocking=%s",
        env,
        tsize_str,
        os.get_blocking(0),
        os.get_blocking(2),
    )
    os.environ["TEXTUAL_DEBUG"] = "1"


def run_editor_app(app: EditorApp) -> None:
    """
    Run a Textual editor app, displaying errors in the terminal on crash.

    Textual handles some exceptions internally (e.g. compose errors) -- it renders a traceback in
    the alt screen, then exits with return code 1.  Once the alt screen is torn down the traceback
    is lost.

    This wrapper catches both raised exceptions and Textual-handled errors, resets the terminal to
    normal mode, displays the traceback, and prompts the user before exiting.
    """
    try:
        app.run()
    except BaseException:
        import traceback as tb_mod

        sys.stdout.write(TERMINAL_CLEANUP)
        sys.stdout.flush()
        restore_opost()
        tb_mod.print_exc()
        pause_before_exit()
        raise
    if app.return_code and app.return_code != 0:
        sys.stdout.write(TERMINAL_CLEANUP)
        sys.stdout.flush()
        restore_opost()
        app.print_error_renderables()
        pause_before_exit()
        sys.exit(app.return_code)


def pause_before_exit() -> None:
    """Prompt user to press RETURN so they can read error output."""
    import termios

    sys.stdout.write("\r\nPress RETURN to continue...\r\n")
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    try:
        os.set_blocking(fd, True)
        old = termios.tcgetattr(fd)
        # Restore cooked mode so RETURN works and Ctrl+C raises SIGINT.
        new = termios.tcgetattr(fd)
        new[3] |= termios.ICANON | termios.ECHO | termios.ISIG
        try:
            termios.tcsetattr(fd, termios.TCSANOW, new)
            os.read(fd, 1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (OSError, termios.error, EOFError, KeyboardInterrupt):
        pass


def launch_editor(screen: Screen[Any], session_key: str = "", logfile: str = "") -> None:
    """Common bootstrap for standalone editor entry points."""
    restore_blocking_fds(logfile)
    log_child_diagnostics()
    patch_writer_thread_queue()
    app = EditorApp(screen, session_key=session_key)
    run_editor_app(app)
