"""
Textual TUI session manager for telix.

Launched when ``telix`` is invoked without a host argument.

Provides a saved-session list, per-session option editing with
fingerprint-based capability detection, and subprocess-based connection
launching.
"""

from __future__ import annotations

# std imports
import os
import sys
import json
import shutil
import logging
import datetime
import subprocess
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple
from dataclasses import asdict, fields, dataclass

if TYPE_CHECKING:
    from rich.text import Text as RichText
    from textual.widget import Widget
    from .rooms import RoomStore

# 3rd party
from textual import events
from rich.style import Style
from textual.app import App, ComposeResult, ScreenStackError
from textual.screen import Screen
from textual.binding import Binding
from textual.widgets import (
    Tree,
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
from textual.widgets._tree import TreeNode

# Reset SGR, cursor, alt-screen, mouse, and bracketed paste.
_TERMINAL_CLEANUP = (
    "\x1b[m\x1b[?25h\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l"
)

_PRIMARY_PASTE_COMMANDS = (
    ("xclip", "-selection", "primary", "-o"),
    ("xsel", "--primary", "--output"),
    ("wl-paste", "--primary", "--no-newline"),
)


def _read_primary_selection() -> str:
    """Read text from the X11/Wayland primary selection via external helper."""
    for cmd in _PRIMARY_PASTE_COMMANDS:
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=2, check=False,
            )
            if result.returncode == 0:
                return result.stdout.decode("utf-8", errors="replace")
        except FileNotFoundError:
            continue
    return ""


_ENCODINGS = (
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
from ._paths import DATA_DIR, CONFIG_DIR, SESSIONS_FILE  # noqa: E402

DEFAULTS_KEY = "__defaults__"
_BATCH_SIZE = 100


# Map CLI flag names (without leading --) to TUI widget IDs.
_FLAG_TO_WIDGET: dict[str, str] = {
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


def _handle_arrow_navigation(
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


_TOOLTIP_CACHE: dict[str, str] | None = None


def _build_tooltips() -> dict[str, str]:
    """Extract help text from argparse and return ``{widget_id: help}``."""
    global _TOOLTIP_CACHE  # noqa: PLW0603
    if _TOOLTIP_CACHE is not None:
        return _TOOLTIP_CACHE
    from telnetlib3.client import _get_argument_parser

    parser = _get_argument_parser()
    tips: dict[str, str] = {}
    for action in parser._actions:
        if not action.help:
            continue
        for opt in action.option_strings:
            flag = opt.lstrip("-")
            widget_id = _FLAG_TO_WIDGET.get(flag)
            if widget_id:
                tips[widget_id] = action.help
    _TOOLTIP_CACHE = tips
    return tips


@dataclass
class SessionConfig:
    """
    Persistent configuration for a single telnet session.

    Field defaults mirror the CLI defaults in
    :func:`telnetlib3.client._get_argument_parser`.
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


def _ensure_dirs() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def load_sessions() -> dict[str, SessionConfig]:
    """Load session configs from ``~/.config/telix/sessions.json``."""
    _ensure_dirs()
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
    from ._paths import _atomic_write

    _ensure_dirs()
    data = {key: asdict(cfg) for key, cfg in sessions.items()}
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write(str(SESSIONS_FILE), content)


_CMD_STR_FLAGS: list[tuple[str, str, object]] = [
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

_CMD_BOOL_FLAGS: list[tuple[str, str, bool]] = [
    ("ssl", "--ssl", False),
    ("ssl_no_verify", "--ssl-no-verify", False),
    ("no_repl", "--no-repl", False),
    ("ansi_keys", "--ansi-keys", False),
    ("ascii_eol", "--ascii-eol", False),
]

_CMD_NEG_BOOL_FLAGS: list[tuple[str, str, bool]] = [("ice_colors", "--no-ice-colors", True)]


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

    for attr, flag, default in _CMD_STR_FLAGS:
        val = getattr(config, attr)
        if val != default:
            cmd.extend([flag, str(val)])

    if config.mode == "raw":
        cmd.append("--raw-mode")
    elif config.mode == "line":
        cmd.append("--line-mode")

    for attr, flag, default in _CMD_BOOL_FLAGS:
        if getattr(config, attr) != default:
            cmd.append(flag)

    for attr, flag, default in _CMD_NEG_BOOL_FLAGS:
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
            if (opt := opt.strip()):
                cmd.extend([flag, opt])

    return cmd


def _relative_time(iso_str: str) -> str:
    """Return a short relative-time string like ``'5m ago'`` or ``'3d ago'``."""
    from ._util import relative_time

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
        self._sessions: dict[str, SessionConfig] = {}
        self._pending_rows: list[tuple[str, SessionConfig]] = []
        self._refresh_gen: int = 0

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
        self._sessions = load_sessions()
        if not self._sessions:
            from .directory import directory_to_sessions

            self._sessions = directory_to_sessions()
            save_sessions(self._sessions)
        table = self.query_one("#session-table", DataTable)
        table.cursor_type = "row"
        table.add_column(" ", width=4, key="icon")
        table.add_columns("Host/Name", "Port", "Enc", "Last", "Flags")
        self._refresh_table()
        if table.row_count > 0:
            table.focus()

    @staticmethod
    def _flags(cfg: SessionConfig) -> str:
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

    def _add_rows(self, table: DataTable, items: list[tuple[str, SessionConfig]]) -> None:
        """Add a list of ``(key, cfg)`` pairs as rows to *table*."""
        for key, cfg in items:
            table.add_row(
                "\u257e" if cfg.bookmarked else "",
                cfg.name or cfg.host,
                str(cfg.port),
                cfg.encoding,
                _relative_time(cfg.last_connected),
                self._flags(cfg),
                key=key,
            )

    def _refresh_table(self, search: str = "") -> None:
        """Rebuild the session table, loading rows in batches."""
        self._refresh_gen += 1
        gen = self._refresh_gen
        table = self.query_one("#session-table", DataTable)
        table.clear()
        needle = search.strip().lower()
        items = [
            (key, cfg)
            for key, cfg in self._sessions.items()
            if key != DEFAULTS_KEY
            and (not needle or needle in f"{cfg.name} {cfg.host} {cfg.port} {cfg.encoding}".lower())
        ]
        # Bookmarked first, then most recently connected first, then name.
        items.sort(key=lambda kc: (kc[1].name or kc[1].host).lower())
        items.sort(key=lambda kc: kc[1].last_connected or "", reverse=True)
        items.sort(key=lambda kc: not kc[1].bookmarked)
        first, rest = items[:_BATCH_SIZE], items[_BATCH_SIZE:]
        self._add_rows(table, first)
        self._pending_rows = rest
        if rest:
            self.call_later(self._load_next_batch, gen)

    def _load_next_batch(self, gen: int) -> None:
        """Add the next batch of rows; bail if a newer refresh has started."""
        if gen != self._refresh_gen:
            return
        table = self.query_one("#session-table", DataTable)
        batch, self._pending_rows = (
            self._pending_rows[:_BATCH_SIZE],
            self._pending_rows[_BATCH_SIZE:],
        )
        self._add_rows(table, batch)
        if self._pending_rows:
            self.call_later(self._load_next_batch, gen)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter session table when search input changes."""
        if event.input.id == "session-search":
            self._refresh_table(event.value)

    def _save(self) -> None:
        save_sessions(self._sessions)

    def _session_keys(self) -> list[str]:
        return [k for k in self._sessions if k != DEFAULTS_KEY]

    def _selected_key(self) -> str | None:
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
            _handle_arrow_navigation(self, event, "#button-col", "#session-table")

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

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        """Connect on double-click or Enter."""
        self.action_connect()

    def action_quit_app(self) -> None:
        """Exit the application."""
        self.app.exit()

    def action_new_session(self) -> None:
        """Open editor for a new session pre-filled with defaults."""
        defaults = self._sessions.get(DEFAULTS_KEY, SessionConfig())
        new_cfg = SessionConfig(**asdict(defaults))
        new_cfg.name = ""
        new_cfg.host = ""
        new_cfg.last_connected = ""
        self.app.push_screen(
            SessionEditScreen(config=new_cfg, is_new=True), callback=self._on_edit_result
        )

    def _require_selected(self) -> str | None:
        """Return selected session key, or notify and return ``None``."""
        key = self._selected_key()
        if key is None:
            self.notify("No session selected", severity="warning")
        return key

    def action_edit_session(self) -> None:
        """Open editor for the selected session."""
        old_key = self._require_selected()
        if old_key is None:
            return
        cfg = self._sessions[old_key]

        def _on_edit(config: SessionConfig | None) -> None:
            if config is None:
                return
            new_key = config.name or config.host
            if not new_key:
                return
            if new_key != old_key and old_key in self._sessions:
                del self._sessions[old_key]
            self._sessions[new_key] = config
            self._save()
            self._refresh_table()
            self._select_row(new_key)

        self.app.push_screen(SessionEditScreen(config=cfg), callback=_on_edit)

    def action_delete_session(self) -> None:
        """Delete the selected session after confirmation."""
        key = self._require_selected()
        if key is None:
            return

        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                del self._sessions[key]
                self._save()
                self._refresh_table()
                self.notify(f"Deleted {key}")

        self.app.push_screen(
            _ConfirmDialogScreen(title="Delete Session", body=f"Delete session '{key}'?"),
            callback=_on_confirm,
        )

    def action_toggle_bookmark(self) -> None:
        """Toggle bookmark on the selected session and re-sort."""
        key = self._require_selected()
        if key is None:
            return
        cfg = self._sessions[key]
        cfg.bookmarked = not cfg.bookmarked
        self._save()
        self._refresh_table()
        self._select_row(key)

    def action_connect(self) -> None:
        """Launch a telnet connection to the selected session."""
        key = self._require_selected()
        if key is None:
            return
        cfg = self._sessions[key]
        if not cfg.host:
            self.notify("No host configured", severity="error")
            return

        cfg.last_connected = datetime.datetime.now().isoformat()
        self._save()

        cmd = build_command(cfg)
        with self.app.suspend():
            # Move to bottom-right and print newline so the TUI
            # scrolls cleanly off screen before the client starts.
            _tsize = os.get_terminal_size()
            sys.stdout.write(f"\x1b[{_tsize.lines};{_tsize.columns}H\r\n")
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
                sys.stdout.write(_TERMINAL_CLEANUP)
                sys.stdout.flush()
        self._refresh_table()
        self._select_row(key)

    def _on_edit_result(self, config: SessionConfig | None) -> None:
        if config is None:
            return
        key = config.name or config.host
        if not key:
            return
        self._sessions[key] = config
        self._save()
        self._refresh_table()
        self._select_row(key)

    def _select_row(self, key: str) -> None:
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
        self._config = config
        self._is_defaults = is_defaults
        self._is_new = is_new

    _TAB_IDS: ClassVar[list[tuple[str, str]]] = [
        ("Connection", "tab-connection"),
        ("Terminal", "tab-terminal"),
        ("Display", "tab-display"),
        ("Advanced", "tab-advanced"),
    ]

    @staticmethod
    def _field_row(label: str, *widgets: "Widget", row_class: str = "field-row") -> Horizontal:
        """Return a ``Horizontal`` row with a label and widgets."""
        return Horizontal(Label(label, classes="field-label"), *widgets, classes=row_class)

    def _compose_connection_tab(self, cfg: SessionConfig) -> ComposeResult:
        """Yield widgets for the Connection tab pane."""
        if not self._is_defaults:
            yield self._field_row(
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

    def _compose_terminal_tab(self, cfg: SessionConfig) -> ComposeResult:
        """Yield widgets for the Terminal tab pane."""
        yield self._field_row(
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
            with Vertical(id="repl-col"):
                with Horizontal(classes="switch-row"):
                    _repl_dim = "" if cfg.mode != "raw" else " dimmed"
                    yield Label("Advanced REPL", id="repl-label", classes=f"field-label{_repl_dim}")
                    yield Switch(value=not cfg.no_repl, id="use-repl", disabled=cfg.mode == "raw")
        _enc = cfg.encoding or "utf-8"
        _is_retro = _enc.lower() in ("atascii", "petscii")
        with Horizontal(classes="field-row"):
            yield Label("Encoding", id="enc-label")
            yield Select(
                [(e, e) for e in _ENCODINGS],
                value=_enc if _enc in _ENCODINGS else "utf-8",
                id="encoding",
                allow_blank=False,
            )
            yield Label("Errors", id="enc-errors-label")
            yield Select(
                [(v, v) for v in ("replace", "ignore", "strict")],
                value=cfg.encoding_errors,
                id="encoding-errors",
            )
        _dim = "" if _is_retro else " dimmed"
        with Horizontal(id="keys-eol-row"):
            with Horizontal(classes="switch-row"):
                yield Label("ANSI Keys", id="ansi-keys-label", classes=f"field-label{_dim}")
                yield Switch(value=cfg.ansi_keys, id="ansi-keys", disabled=not _is_retro)
            with Horizontal(classes="switch-row"):
                yield Label("ASCII EOL", id="ascii-eol-label", classes=f"field-label{_dim}")
                yield Switch(value=cfg.ascii_eol, id="ascii-eol", disabled=not _is_retro)

    def _compose_display_tab(self, cfg: SessionConfig) -> ComposeResult:
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

    def _compose_advanced_tab(self, cfg: SessionConfig) -> ComposeResult:
        """Yield widgets for the Advanced tab pane."""
        yield self._field_row(
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
        yield self._field_row(
            "Typescript",
            Input(value=cfg.typescript, placeholder="path", id="typescript", classes="field-input"),
        )

    def compose(self) -> ComposeResult:
        """Build the tabbed session editor layout."""
        cfg = self._config
        with Vertical(id="edit-panel"):
            with Horizontal(id="tab-bar"):
                for i, (label, tab_id) in enumerate(self._TAB_IDS):
                    btn = Button(label, id=f"tabbtn-{tab_id}")
                    if i == 0:
                        btn.add_class("active-tab")
                    yield btn

            with ContentSwitcher(id="tab-content", initial="tab-connection"):
                with Vertical(id="tab-connection", classes="tab-pane"):
                    yield from self._compose_connection_tab(cfg)
                with Vertical(id="tab-terminal", classes="tab-pane"):
                    yield from self._compose_terminal_tab(cfg)
                with Vertical(id="tab-display", classes="tab-pane"):
                    yield from self._compose_display_tab(cfg)
                with Vertical(id="tab-advanced", classes="tab-pane"):
                    yield from self._compose_advanced_tab(cfg)

            with Horizontal(id="bottom-bar"):
                yield Button("Cancel", variant="error", id="cancel-btn")
                yield Button("Save", variant="success", id="save-btn")

    def on_mount(self) -> None:
        """Apply argparse-derived tooltips to form widgets."""
        tips = _build_tooltips()
        for widget_id, help_text in tips.items():
            try:
                widget = self.query_one(f"#{widget_id}")
                widget.tooltip = help_text
            except NoMatches:
                pass
        self._update_palette_preview()
        for radio_set in self.query(RadioSet):
            idx = radio_set.pressed_index
            if idx >= 0:
                radio_set._selected = idx

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Handle radio-set changes for server type and terminal mode."""
        if event.radio_set.id == "server-type-radio":
            self._apply_server_type(event.pressed.id)
        elif event.radio_set.id == "mode-radio":
            is_raw = event.pressed.id == "mode-raw"
            repl_switch = self.query_one("#use-repl", Switch)
            repl_switch.disabled = is_raw
            self.query_one("#repl-label", Label).set_class(is_raw, "dimmed")

    def _select_radio(self, radio_set_id: str, button_id: str) -> None:
        """Select a radio button by deselecting all siblings first."""
        radio_set = self.query_one(f"#{radio_set_id}", RadioSet)
        for btn in radio_set.query(RadioButton):
            btn.value = False
        self.query_one(f"#{button_id}", RadioButton).value = True

    def _apply_server_type(self, button_id: str) -> None:
        """Apply preset field values for BBS or MUD server type."""
        if button_id == "type-bbs":
            self.query_one("#colormatch", Select).value = "vga"
            self.query_one("#ice-colors", Switch).value = True
            self._select_radio("mode-radio", "mode-raw")
            self.query_one("#use-repl", Switch).value = False
            self.query_one("#use-repl", Switch).disabled = True
            self.query_one("#repl-label", Label).set_class(True, "dimmed")
            self._select_radio("compression-radio", "compress-passive")
            self._update_palette_preview()
            self.notify(
                "BBS: Color Palette vga, iCE Colors on, Raw mode,"
                " REPL off, MCCP Compression passive"
            )
        elif button_id == "type-mud":
            self._select_radio("compression-radio", "compress-yes")
            self._select_radio("mode-radio", "mode-line")
            self.query_one("#use-repl", Switch).value = True
            self.query_one("#use-repl", Switch).disabled = False
            self.query_one("#repl-label", Label).set_class(False, "dimmed")
            self.query_one("#colormatch", Select).value = "none"
            self.query_one("#ice-colors", Switch).value = False
            self._update_palette_preview()
            self.notify(
                "MUD: MCCP Compression yes, Line mode,"
                " REPL on, Color Palette none, iCE Colors off"
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        """React to Select widget changes."""
        if event.select.id == "colormatch":
            self._update_palette_preview()
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
            self._update_palette_preview()

    def _update_palette_preview(self) -> None:
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

    def _switch_to_tab(self, tab_id: str) -> None:
        """Activate the given tab and update button styling."""
        self.query_one("#tab-content", ContentSwitcher).current = tab_id
        for btn in self.query("#tab-bar Button"):
            btn.remove_class("active-tab")
            if btn.id == f"tabbtn-{tab_id}":
                btn.add_class("active-tab")

    def _active_tab_focusables(self) -> list[Any]:
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
                if (tab_id := (target.id or "").replace("tabbtn-", "")):
                    self._switch_to_tab(tab_id)
                event.prevent_default()
            elif event.key == "down":
                focusables = self._active_tab_focusables()
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
                focusables = self._active_tab_focusables()
                if focusables:
                    focusables[-1].focus()
                event.prevent_default()
            return

        focusables = self._active_tab_focusables()
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
            self._on_save()
        elif btn_id == "cancel-btn":
            self.dismiss(None)
        elif btn_id.startswith("tabbtn-"):
            tab_id = btn_id[len("tabbtn-") :]
            self._switch_to_tab(tab_id)

    def _on_save(self) -> None:
        config = self._collect_config()
        self.dismiss(config)

    def _collect_config(self) -> SessionConfig:
        """Read all widget values back into a :class:`SessionConfig`."""
        cfg = SessionConfig()

        if not self._is_defaults:
            cfg.name = self.query_one("#name", Input).value.strip()
            cfg.host = self.query_one("#host", Input).value.strip()
            cfg.port = _int_val(self.query_one("#port", Input).value, 23)
        else:
            cfg.name = DEFAULTS_KEY

        cfg.ssl = self.query_one("#ssl", Switch).value
        cfg.ssl_no_verify = False

        cfg.last_connected = self._config.last_connected

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

        cfg.connect_timeout = _float_val(self.query_one("#connect-timeout", Input).value, 10.0)

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
        cfg.always_will = self._config.always_will
        cfg.always_do = self._config.always_do
        cfg.loglevel = self.query_one("#loglevel", Select).value
        cfg.logfile = self.query_one("#logfile", Input).value.strip()
        cfg.typescript = self.query_one("#typescript", Input).value.strip()
        cfg.no_repl = not self.query_one("#use-repl", Switch).value

        return cfg


def _int_val(text: str, default: int) -> int:
    try:
        return int(text.strip())
    except (ValueError, TypeError):
        return default


def _float_val(text: str, default: float) -> float:
    try:
        return float(text.strip())
    except (ValueError, TypeError):
        return default


def _get_help_topic(topic: str) -> str:
    """Load help text for a TUI dialog topic from bundled markdown files."""
    from telix.help import get_help  # noqa: E501  # deferred to avoid import cost

    return get_help(topic)


class _HelpPane(Vertical):
    """Widget containing help content -- embeddable in a tab or standalone screen."""

    DEFAULT_CSS = """
    _HelpPane {
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
        self._topic = topic

    def compose(self) -> ComposeResult:
        content = _get_help_topic(self._topic)
        with Vertical(id="help-dialog"):
            with VerticalScroll(id="help-scroll"):
                yield Markdown(content, id="help-content")


class _CommandHelpScreen(Screen[None]):
    """Scrollable help screen with context-specific documentation."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Exit"),
        Binding("q", "close", "Exit", show=False),
    ]

    def __init__(self, topic: str = "macro") -> None:
        super().__init__()
        self._pane = _HelpPane(topic=topic)

    def compose(self) -> ComposeResult:
        yield self._pane
        yield Footer()

    def action_close(self) -> None:
        """Dismiss the help screen."""
        self.dismiss(None)


class _EditListPane(Vertical):
    """Base pane for list-editor UIs (macros, autoreplies, etc.)."""

    DEFAULT_CSS = """
    _EditListPane {
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
    def _prefix(self) -> str: ...

    @property
    @abstractmethod
    def _noun(self) -> str:
        """Display noun for this editor, e.g. 'Macro' or 'Autoreply'."""

    @property
    def _noun_plural(self) -> str:
        """Plural form of :attr:`_noun`; override for irregular plurals."""
        return self._noun + "s"

    @property
    @abstractmethod
    def _items(self) -> list[Any]: ...

    def _item_label(self, idx: int) -> str:
        """Return a display label for the item at *idx*."""
        return str(self._items[idx][0]) if idx < len(self._items) else ""

    _growable_keys: list[str] = []
    """Column keys (from ``add_column(key=…)``) that should expand to fill space."""

    def __init__(self) -> None:
        super().__init__()
        self._editing_idx: int | None = None
        self._filtered_indices: list[int] = []
        self._search_query: str = ""

    def _request_close(self, result: "bool | None" = None) -> None:
        """Dismiss the parent screen or exit the app."""
        try:
            self.screen.dismiss(result)
        except ScreenStackError:
            self.app.exit()

    @property
    def _form_visible(self) -> bool:
        return bool(self.query_one(f"#{self._prefix}-form").display)

    def _fit_growable_columns(self) -> None:
        """Distribute remaining table width equally among growable columns."""
        table = self.query_one(f"#{self._prefix}-table", DataTable)
        avail = table.size.width
        if avail <= 0:
            return
        pad = table.cell_padding
        fixed_total = 0
        growable: list[Any] = []
        for col in table.ordered_columns:
            if str(col.key) in self._growable_keys:
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
        if self._growable_keys:
            self.call_after_refresh(self._fit_growable_columns)

    def _set_action_buttons_disabled(self, disabled: bool) -> None:
        """Enable or disable the add/edit/copy buttons."""
        pfx = self._prefix
        for suffix in ("add", "edit", "copy"):
            self.query_one(f"#{pfx}-{suffix}", Button).disabled = disabled

    def _hide_form(self) -> None:
        pfx = self._prefix
        self.query_one(f"#{pfx}-form").display = False
        self.query_one(f"#{pfx}-table").display = True
        try:
            self.query_one(f"#{pfx}-search", Input).display = True
        except NoMatches:
            pass
        self._editing_idx = None
        self._set_action_buttons_disabled(False)
        self.query_one(f"#{pfx}-table", DataTable).focus()

    def _finalize_edit(self, entry: Any, is_valid: bool) -> None:
        """Insert or update an item, refresh, and hide the form."""
        if is_valid:
            if self._editing_idx is not None:
                self._items[self._editing_idx] = entry
                target_row = self._editing_idx
            else:
                target_row = len(self._items)
                self._items.append(entry)
            self._refresh_table()
            self.query_one(f"#{self._prefix}-table", DataTable).move_cursor(row=target_row)
        self._hide_form()

    def _selected_idx(self) -> int | None:
        table = self.query_one(f"#{self._prefix}-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        row_pos = int(str(row_key.value))
        if self._filtered_indices:
            if row_pos < len(self._filtered_indices):
                return self._filtered_indices[row_pos]
            return None
        return row_pos

    def _edit_selected(self) -> None:
        idx = self._selected_idx()
        if idx is not None and idx < len(self._items):
            self._editing_idx = idx
            self._show_form(*self._items[idx])

    def _copy_selected(self) -> None:
        idx = self._selected_idx()
        if idx is not None and idx < len(self._items):
            self._items.insert(idx + 1, self._items[idx])
            self._refresh_table()
            table = self.query_one(f"#{self._prefix}-table", DataTable)
            table.move_cursor(row=idx + 1)

    def _reorder(self, move_down: bool) -> None:
        idx = self._selected_idx()
        if idx is None:
            return
        items = self._items
        target = idx + 1 if move_down else idx - 1
        if target < 0 or target >= len(items):
            return
        items[idx], items[target] = items[target], items[idx]
        self._refresh_table()
        table = self.query_one(f"#{self._prefix}-table", DataTable)
        table.move_cursor(row=target)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit the form when Enter is pressed in an input field."""
        if self._form_visible:
            event.stop()
            self._submit_form()

    def action_cancel_or_close(self) -> None:
        """Cancel form editing or close the screen."""
        if self._form_visible:
            self._hide_form()
        else:
            self._request_close(None)

    def action_reorder_hint(self) -> None:
        """Placeholder for reorder key binding hint."""

    def action_save_hint(self) -> None:
        """Placeholder for save key binding hint."""

    def action_show_help(self) -> None:
        """Open the context-sensitive help screen."""
        self.app.push_screen(_CommandHelpScreen(topic=self._prefix))

    def _matches_search(self, idx: int, query: str) -> bool:
        """Return True if item at *idx* matches the search *query*."""
        return True

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter table when search input changes."""
        if event.input.id == f"{self._prefix}-search":
            self._search_query = event.value
            self._refresh_table()

    def on_key(self, event: events.Key) -> None:
        """Arrow/Home/End/+/- keys navigate and reorder the table."""
        pfx = self._prefix
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
            if (self.screen.focused is table
                    and event.key == "up" and table.cursor_row == 0):
                search_input.focus()
                event.prevent_default()
                return

        if event.key in ("home", "end"):
            table = self.query_one(f"#{self._prefix}-table", DataTable)
            if self.screen.focused is table and table.row_count > 0:
                row = 0 if event.key == "home" else table.row_count - 1
                table.move_cursor(row=row)
                event.prevent_default()
        elif event.key in ("up", "down", "left", "right"):
            _handle_arrow_navigation(
                self.screen,
                event,
                f"#{self._prefix}-button-col",
                f"#{self._prefix}-table",
                f"#{self._prefix}-form",
            )
        elif event.key in ("plus", "minus", "equals_sign") and not self._form_visible:
            self._reorder(event.key in ("plus", "equals_sign"))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Double-click or Enter on a table row opens it for editing."""
        row_pos = int(str(event.row_key.value))
        if self._filtered_indices:
            if row_pos < len(self._filtered_indices):
                idx = self._filtered_indices[row_pos]
            else:
                return
        else:
            idx = row_pos
        if idx < len(self._items):
            self._editing_idx = idx
            self._show_form(*self._items[idx])

    def _action_add(self) -> None:
        self._editing_idx = None
        self._show_form()

    def _action_delete(self) -> None:
        if self._form_visible:
            self._hide_form()
        idx = self._selected_idx()
        if idx is not None and idx < len(self._items):
            label = self._item_label(idx)
            safe_idx: int = idx

            def _on_confirm(confirmed: bool, _idx: int = safe_idx) -> None:
                if confirmed and _idx < len(self._items):
                    self._items.pop(_idx)
                    self._refresh_table()

            self.app.push_screen(
                _ConfirmDialogScreen(
                    title=f"Delete {self._noun}",
                    body=f"Delete {self._noun.lower()} '{label}'?",
                    show_dont_ask=False,
                ),
                callback=_on_confirm,
            )

    def _action_ok(self) -> None:
        if self._form_visible:
            self._submit_form()

    def _action_save(self) -> None:
        if self._form_visible:
            self._submit_form()
        self._save_to_file()
        self._request_close(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle common list-editor button presses."""
        btn = event.button.id or ""
        pfx = self._prefix
        suffix = btn.removeprefix(pfx + "-") if btn.startswith(pfx + "-") else ""
        handlers: dict[str, Any] = {
            "add": self._action_add,
            "edit": self._edit_selected,
            "copy": self._copy_selected,
            "delete": self._action_delete,
            "ok": self._action_ok,
            "cancel-form": self._hide_form,
            "save": self._action_save,
            "close": lambda: self._request_close(None),
            "help": lambda: self.app.push_screen(
                _CommandHelpScreen(topic=self._prefix)
            ),
        }
        handler = handlers.get(suffix)
        if handler:
            handler()
        elif suffix:
            self._on_extra_button(suffix, btn)

    @property
    def _text_input_id(self) -> str:
        """ID of the text/reply Input widget for command insertion."""
        return f"{self._prefix}-text"

    def _insert_command(self, cmd: str) -> None:
        """Insert a command at the cursor position, adding ``;`` separators."""
        if self._form_visible:
            inp = self.query_one(f"#{self._text_input_id}", Input)
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
            self._editing_idx = None
            self._show_form()

    _rooms_path: str = ""
    _current_room_path: str = ""

    def _pick_room_for_travel(self) -> None:
        """Open room picker and insert a travel command."""
        rooms_file = self._rooms_path
        if not rooms_file or not os.path.exists(rooms_file):
            return

        def _on_pick(room_id: "str | None") -> None:
            if room_id is None:
                return
            cmd = f"`travel {room_id}`"
            self._insert_command(cmd)

        kwargs: dict[str, str] = {
            "rooms_path": rooms_file,
            "session_key": self._session_key,
        }
        if self._current_room_path:
            kwargs["current_room_file"] = self._current_room_path
        self.app.push_screen(RoomPickerScreen(**kwargs), callback=_on_pick)

    _COMMAND_BUTTONS: ClassVar[dict[str, str]] = {
        "btn-when": "`when HP%>=99`",
        "btn-until": "`until 10 pattern`",
        "btn-delay": "`delay 1s`",
        "delay": "`delay 1s`",
        "btn-randomwalk": "`randomwalk`",
        "return": "`return`",
        "autodiscover": "`autodiscover`",
    }

    def _on_extra_button(self, suffix: str, btn: str) -> None:
        """Handle shared command-builder buttons; override for extras."""
        cmd = self._COMMAND_BUTTONS.get(suffix)
        if cmd is not None:
            self._insert_command(cmd)

    @abstractmethod
    def _show_form(self, *args: Any) -> None: ...

    @abstractmethod
    def _submit_form(self) -> None: ...

    @abstractmethod
    def _refresh_table(self) -> None: ...

    def _update_count_label(self) -> None:
        """Update the count label and refit growable columns after refresh."""
        n_total = len(self._items)
        n_shown = len(self._filtered_indices)
        noun = self._noun_plural
        label = self.query_one(f"#{self._prefix}-count", Static)
        if self._search_query:
            label.update(f"{n_shown:,}/{n_total:,} {noun}")
        else:
            label.update(f"{n_total:,} {noun}")
        self.call_after_refresh(self._fit_growable_columns)

    @abstractmethod
    def _save_to_file(self) -> None: ...


class _EditListScreen(Screen["bool | None"]):
    """Thin screen wrapper around an ``_EditListPane``."""

    @property
    def _pane(self) -> _EditListPane:
        """Return the pane widget -- subclasses set ``self.__pane`` in __init__."""
        return self.__pane

    @_pane.setter
    def _pane(self, value: _EditListPane) -> None:
        self.__pane = value

    def compose(self) -> ComposeResult:
        yield self._pane
        yield Footer()


class MacroEditPane(_EditListPane):
    """Pane widget for macro key binding editing."""

    _growable_keys: list[str] = ["text", "toggle-text"]

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_or_close", "Cancel", priority=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("plus", "reorder_hint", "Change Priority", key_display="+/=/-", show=True),
        Binding("enter", "save_hint", "Save", show=True),
        Binding("l", "sort_last", "Recent", show=True),
    ]

    DEFAULT_CSS = _EditListPane.DEFAULT_CSS + """
    #macro-form { padding: 0; }
    #macro-text-row { margin: 1 0; }
    #macro-toggle-text-row { margin: 0 0 1 0; }
    #macro-form .switch-row { height: 3; margin: 0; }
    #macro-key-label {
        width: 16; height: 1; padding: 0 1;
        margin: 1 0 0 1;
        background: $surface-darken-1; color: $text;
    }
    #macro-key-label.capturing { color: $warning; }
    #macro-capture { width: auto; min-width: 13; margin-left: 1; }
    #macro-capture-status { width: 1fr; height: 1; color: $error; padding: 0 1; }
    #macro-form .form-gap { width: 10; }
    """

    def __init__(
        self, path: str, session_key: str = "", rooms_file: str = "",
        current_room_file: str = "",
    ) -> None:
        """Initialize macro editor with file path and session key."""
        super().__init__()
        self._path = path
        self._session_key = session_key
        self._rooms_path = rooms_file
        self._current_room_path = current_room_file
        self._macros: list[tuple[str, str, bool, str, bool, str]] = []
        self._capturing: bool = False
        self._sort_mode: str = ""
        self._capture_escape_pending: bool = False
        self._captured_key: str = ""

    @property
    def _prefix(self) -> str:
        return "macro"

    @property
    def _noun(self) -> str:
        return "Macro"

    @property
    def _items(self) -> list[Any]:
        return self._macros

    def compose(self) -> ComposeResult:
        """Build the macro editor layout."""
        with Vertical(id="macro-panel", classes="edit-panel"):
            with Horizontal(id="macro-body", classes="edit-body"):
                with Vertical(id="macro-button-col", classes="edit-button-col"):
                    yield Button("Add", variant="success", id="macro-add")
                    yield Button("Edit", variant="warning", id="macro-edit")
                    yield Button("Copy", id="macro-copy", classes="edit-copy")
                    yield Button("Delete", variant="error", id="macro-delete")
                    yield Button("Help", variant="success", id="macro-help")
                    yield Button("Save", variant="primary", id="macro-save")
                    yield Button("Cancel", id="macro-close")
                with Vertical(id="macro-right", classes="edit-right"):
                    yield Input(
                        placeholder="Search macros\u2026", id="macro-search", classes="edit-search"
                    )
                    yield DataTable(id="macro-table", classes="edit-table")
                    with Vertical(id="macro-form", classes="edit-form"):
                        with Horizontal(classes="field-row"):
                            yield Label("Enabled:", classes="toggle-label")
                            yield Switch(value=True, id="macro-enabled")
                            yield Label("", classes="form-gap")
                            yield Label("Key", classes="form-label-mid")
                            yield Button("Capture", id="macro-capture")
                            yield Static("(none)", id="macro-key-label")
                            yield Static("", id="macro-capture-status")
                        with Horizontal(id="macro-text-row", classes="field-row"):
                            yield Label("Text", classes="form-label", id="macro-text-label")
                            yield Input(placeholder="text with ; separators", id="macro-text")
                        with Horizontal(id="macro-toggle-row", classes="field-row"):
                            yield Label("Toggle:", classes="toggle-label")
                            yield Switch(value=False, id="macro-toggle")
                        with Horizontal(
                            id="macro-toggle-text-row", classes="field-row"
                        ):
                            yield Label("Off command", classes="form-label")
                            yield Input(
                                placeholder="off command with ; separators",
                                id="macro-toggle-text",
                            )
                        with Horizontal(classes="field-row"):
                            yield Button(
                                "Travel", id="macro-fast-travel", classes="insert-btn"
                            )
                            yield Button(
                                "Return", id="macro-return", classes="insert-btn"
                            )
                        with Horizontal(classes="field-row"):
                            yield Button(
                                "Autodiscover", id="macro-autodiscover", classes="insert-btn"
                            )
                            yield Button(
                                "Random Walk", id="macro-btn-randomwalk", classes="insert-btn"
                            )
                            yield Button("Delay", id="macro-delay", classes="insert-btn")
                            yield Button("When", id="macro-btn-when", classes="insert-btn")
                            yield Button("Until", id="macro-btn-until", classes="insert-btn")
                        with Horizontal(id="macro-form-buttons", classes="edit-form-buttons"):
                            yield Label(" ", classes="form-btn-spacer")
                            yield Button("Cancel", variant="default", id="macro-cancel-form")
                            yield Button("OK", variant="success", id="macro-ok")
                    yield Static("", id="macro-count")

    def on_mount(self) -> None:
        """Load macros from file and populate table."""
        table = self.query_one("#macro-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Key", width=14, key="key")
        table.add_column("Command Text", key="text")
        table.add_column("Last", width=8, key="last")
        self._load_from_file()
        self._refresh_table()
        self.query_one("#macro-form").display = False

    def _load_from_file(self) -> None:
        if not os.path.exists(self._path):
            return
        from .macros import load_macros

        try:
            macros = load_macros(self._path, self._session_key)
            self._macros = [
                (m.key, m.text, m.enabled, m.last_used, m.toggle, m.toggle_text)
                for m in macros
            ]
        except (ValueError, FileNotFoundError):
            pass

    def _matches_search(self, idx: int, query: str) -> bool:
        """Match macro key, text, or toggle_text against search query."""
        key, text, _enabled, _lu, _toggle, toggle_text = self._macros[idx]
        q = query.lower()
        return q in key.lower() or q in text.lower() or q in toggle_text.lower()

    def _refresh_table(self) -> None:
        table = self.query_one("#macro-table", DataTable)
        table.clear()
        q = self._search_query
        self._filtered_indices = []
        order = list(range(len(self._macros)))
        if self._sort_mode == "last_used":
            order.sort(key=lambda i: _invert_ts(self._macros[i][3]))
        for i in order:
            key, text, enabled, last_used, toggle, _toggle_text = self._macros[i]
            if q and not self._matches_search(i, q):
                continue
            status = "" if enabled else " (off)"
            if toggle:
                status += " (toggle)"
            lu = _relative_time(last_used) if last_used else ""
            self._filtered_indices.append(i)
            table.add_row(key, text + status, lu, key=str(len(self._filtered_indices) - 1))
        self._update_count_label()

    def action_sort_last(self) -> None:
        """Toggle sorting macros by last used time."""
        self._sort_mode = "last_used" if self._sort_mode != "last_used" else ""
        self._refresh_table()

    def _show_form(
        self, key_val: str = "", text_val: str = "", enabled: bool = True,
        last_used: str = "", toggle: bool = False, toggle_text: str = "",
    ) -> None:
        self._captured_key = key_val
        self._capturing = False
        self._capture_escape_pending = False
        label = self.query_one("#macro-key-label", Static)
        display = self._blessed_display(key_val) if key_val else "(none)"
        label.update(display)
        label.remove_class("capturing")
        self.query_one("#macro-capture-status", Static).update("")
        self.query_one("#macro-text", Input).value = text_val
        self.query_one("#macro-enabled", Switch).value = enabled
        self.query_one("#macro-toggle", Switch).value = toggle
        self.query_one("#macro-toggle-text", Input).value = toggle_text
        text_label = self.query_one("#macro-text-label", Label)
        text_label.update("On command" if toggle else "Text")
        self.query_one("#macro-toggle-text-row").display = toggle
        self.query_one("#macro-search", Input).display = False
        self.query_one("#macro-table").display = False
        self.query_one("#macro-form").display = True
        self._set_action_buttons_disabled(True)
        self.query_one("#macro-text", Input).focus()

    def _hide_form(self) -> None:
        self._capturing = False
        self._capture_escape_pending = False
        super()._hide_form()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Show/hide toggle text row when the toggle switch changes."""
        if event.switch.id == "macro-toggle":
            on = event.value
            text_label = self.query_one("#macro-text-label", Label)
            text_label.update("On command" if on else "Text")
            self.query_one("#macro-toggle-text-row").display = on

    def _submit_form(self) -> None:
        """Accept the current inline form values."""
        key_val = self._captured_key.strip()
        text_val = self.query_one("#macro-text", Input).value
        enabled = self.query_one("#macro-enabled", Switch).value
        toggle = self.query_one("#macro-toggle", Switch).value
        toggle_text = self.query_one("#macro-toggle-text", Input).value
        lu = self._macros[self._editing_idx][3] if self._editing_idx is not None else ""
        self._finalize_edit(
            (key_val, text_val, enabled, lu, toggle, toggle_text), bool(key_val)
        )

    _REPL_RESERVED_KEYS: ClassVar[frozenset[str]] = frozenset({
        "KEY_F1", "KEY_F3", "KEY_F4", "KEY_F5", "KEY_F6", "KEY_F7",
        "KEY_F8", "KEY_F9", "KEY_F10", "KEY_F11", "KEY_F18", "KEY_F21",
    })

    @staticmethod
    def _blessed_display(blessed_name: str) -> str:
        """Format a blessed key name for display (strip KEY\\_ prefix)."""
        if blessed_name.startswith("KEY_"):
            return blessed_name[4:]
        return blessed_name

    def _finish_capture(self, blessed_key: str, display: str) -> None:
        """Accept a captured key and update the form."""
        from blessed.line_editor import DEFAULT_KEYMAP

        if blessed_key in DEFAULT_KEYMAP:
            self._reject_capture(f"Rejected: {display} -- reserved by line editor")
            return
        if blessed_key in self._REPL_RESERVED_KEYS:
            self._reject_capture(f"Rejected: {display} -- reserved by REPL")
            return
        self._capturing = False
        self._capture_escape_pending = False
        self._captured_key = blessed_key
        label = self.query_one("#macro-key-label", Static)
        label.update(display)
        label.remove_class("capturing")
        self.query_one("#macro-capture-status", Static).update("")

    def _reject_capture(self, reason: str) -> None:
        """Show a rejection message and stay in capture mode."""
        self.query_one("#macro-capture-status", Static).update(reason)

    def on_key(self, event: events.Key) -> None:
        """Handle key capture mode, then delegate to base navigation."""
        if self._capturing:
            event.stop()
            event.prevent_default()
            key = event.key

            if self._capture_escape_pending:
                self._capture_escape_pending = False
                if key == "escape":
                    blessed_key = "KEY_ESCAPE"
                    self._finish_capture(blessed_key, "ESCAPE")
                elif len(key) == 1 and key.isalpha():
                    blessed_key = "KEY_ALT_" + key.upper()
                    self._finish_capture(blessed_key, "ALT_" + key.upper())
                else:
                    self._reject_capture(
                        f"Rejected: escape+{key} -- use Esc then a letter"
                    )
                return

            if key == "escape":
                self._capture_escape_pending = True
                self.query_one("#macro-capture-status", Static).update(
                    "Esc pressed -- now press a letter for Alt combo, "
                    "or Esc again for plain Escape"
                )
                return

            if key.startswith("f") and key[1:].isdigit():
                blessed_key = "KEY_" + key.upper()
                self._finish_capture(blessed_key, key.upper())
                return

            if key.startswith("ctrl+"):
                letter = key[5:]
                if len(letter) == 1 and letter.isalpha():
                    blessed_key = "KEY_CTRL_" + letter.upper()
                    self._finish_capture(blessed_key, "CTRL_" + letter.upper())
                    return

            if key.startswith("alt+"):
                letter = key[4:]
                if len(letter) == 1 and letter.isalpha():
                    blessed_key = "KEY_ALT_" + letter.upper()
                    self._finish_capture(blessed_key, "ALT_" + letter.upper())
                    return

            self._reject_capture(
                f"Rejected: {key} -- use F-keys, Ctrl+key, or Alt+key"
            )
            return

        super().on_key(event)

    def action_cancel_or_close(self) -> None:
        """Cancel key capture or close the screen."""
        if self._capturing:
            return
        super().action_cancel_or_close()

    def _on_extra_button(self, suffix: str, btn: str) -> None:
        """Handle macro-specific buttons (travel, capture, etc.)."""
        if suffix == "fast-travel":
            self._pick_room_for_travel()
        elif suffix == "capture":
            self._capturing = True
            self._capture_escape_pending = False
            label = self.query_one("#macro-key-label", Static)
            label.update("press keystroke to capture ...")
            label.add_class("capturing")
            self.query_one("#macro-capture-status", Static).update("")
        else:
            super()._on_extra_button(suffix, btn)

    def _save_to_file(self) -> None:
        from .macros import Macro, save_macros

        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        macros = [
            Macro(key=k, text=t, enabled=ena, last_used=lu, toggle=tog, toggle_text=tt)
            for k, t, ena, lu, tog, tt in self._macros
        ]
        save_macros(self._path, macros, self._session_key)


class MacroEditScreen(_EditListScreen):
    """Thin screen wrapper for the macro editor."""

    def __init__(
        self, path: str, session_key: str = "", rooms_file: str = "",
        current_room_file: str = "",
    ) -> None:
        super().__init__()
        self._pane = MacroEditPane(
            path=path, session_key=session_key,
            rooms_file=rooms_file, current_room_file=current_room_file,
        )


class _AutoreplyTuple(NamedTuple):
    """Lightweight tuple for autoreply rules in the TUI editor."""

    pattern: str
    reply: str
    always: bool = False
    enabled: bool = True
    when: dict[str, str] | None = None
    immediate: bool = False
    last_fired: str = ""
    case_sensitive: bool = False


class AutoreplyEditPane(_EditListPane):
    """Pane widget for autoreply rule editing."""

    _growable_keys: list[str] = ["pattern", "reply"]

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_or_close", "Cancel", priority=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("plus", "reorder_hint", "Change Priority", key_display="+/=/-", show=True),
        Binding("enter", "save_hint", "Save", show=True),
        Binding("l", "sort_last", "Recent", show=True),
    ]

    DEFAULT_CSS = _EditListPane.DEFAULT_CSS + """
    #autoreply-form { padding: 0 0 0 4; }
    #autoreply-form .form-label { width: 12; }
    #autoreply-form .form-label-mid { width: 9; }
    #autoreply-form .insert-btn { margin: 0; padding: 0 1; }
    #autoreply-cond-vital { width: 14; }
    #autoreply-cond-op { width: 8; }
    #autoreply-cond-val { width: 9; border: tall grey; }
    #autoreply-cond-val:focus { border: tall $accent; }
    """

    def __init__(self, path: str, session_key: str = "", select_pattern: str = "") -> None:
        """Initialize autoreply editor with file path and session key."""
        super().__init__()
        self._path = path
        self._session_key = session_key
        self._select_pattern = select_pattern
        self._rules: list[_AutoreplyTuple] = []
        self._sort_mode: str = ""
        self._rooms_path = os.path.join(os.path.dirname(path), "rooms.json")

    @property
    def _prefix(self) -> str:
        return "autoreply"

    @property
    def _noun(self) -> str:
        return "Autoreply"

    @property
    def _noun_plural(self) -> str:
        return "Autoreplies"

    @property
    def _items(self) -> list[Any]:
        return self._rules

    @property
    def _text_input_id(self) -> str:
        return "autoreply-reply"

    def compose(self) -> ComposeResult:
        """Build the autoreply editor layout."""
        with Vertical(id="autoreply-panel", classes="edit-panel"):
            with Horizontal(id="autoreply-body", classes="edit-body"):
                with Vertical(id="autoreply-button-col", classes="edit-button-col"):
                    yield Button("Add", variant="success", id="autoreply-add")
                    yield Button("Edit", variant="warning", id="autoreply-edit")
                    yield Button("Copy", id="autoreply-copy", classes="edit-copy")
                    yield Button("Delete", variant="error", id="autoreply-delete")
                    yield Button("Help", variant="success", id="autoreply-help")
                    yield Button("Save", variant="primary", id="autoreply-save")
                    yield Button("Cancel", id="autoreply-close")
                with Vertical(id="autoreply-right", classes="edit-right"):
                    yield Input(
                        placeholder="Search autoreplies\u2026",
                        id="autoreply-search",
                        classes="edit-search",
                    )
                    yield DataTable(id="autoreply-table", classes="edit-table")
                    with Vertical(id="autoreply-form", classes="edit-form"):
                        with Horizontal(classes="field-row"):
                            yield Label("Enabled:", classes="toggle-label")
                            yield Switch(value=True, id="autoreply-enabled")
                            yield Label("", classes="toggle-gap")
                            alw = Switch(value=False, id="autoreply-always")
                            alw.tooltip = "Match even while another rule's chain is active"
                            yield Label("Always:", classes="toggle-label")
                            yield alw
                            yield Label("", classes="toggle-gap")
                            imm = Switch(value=False, id="autoreply-immediate")
                            imm.tooltip = "Reply immediately without waiting for prompt"
                            yield Label("Immediate:", classes="toggle-label")
                            yield imm
                            yield Label("", classes="toggle-gap")
                            cs = Switch(value=False, id="autoreply-case-sensitive")
                            cs.tooltip = "Case-sensitive pattern matching"
                            yield Label("Case Sensitive:", classes="toggle-label")
                            yield cs
                        with Horizontal(classes="field-row"):
                            yield Label("Pattern", classes="form-label-short")
                            yield Input(placeholder="regex pattern", id="autoreply-pattern")
                        with Horizontal(classes="field-row"):
                            yield Label("Reply", classes="form-label-short")
                            yield Input(
                                placeholder=r"reply with \1 refs, ;/: seps", id="autoreply-reply"
                            )
                        with Horizontal(classes="field-row"):
                            yield Label("Condition", classes="form-label-short")
                            yield Select(
                                [
                                    ("(none)", ""),
                                    ("HP%", "HP%"),
                                    ("MP%", "MP%"),
                                    ("HP", "HP"),
                                    ("MP", "MP"),
                                ],
                                value="",
                                allow_blank=False,
                                id="autoreply-cond-vital",
                            )
                            yield Select(
                                [(">", ">"), ("<", "<"), (">=", ">="), ("<=", "<="), ("=", "=")],
                                value=">",
                                allow_blank=False,
                                id="autoreply-cond-op",
                            )
                            yield Input(value="99", placeholder="99", id="autoreply-cond-val")
                            yield Label("(as percent)", classes="form-label-pct")
                        with Horizontal(classes="field-row"):
                            yield Button("When", id="autoreply-btn-when", classes="insert-btn")
                            yield Button("Until", id="autoreply-btn-until", classes="insert-btn")
                            yield Button("Delay", id="autoreply-btn-delay", classes="insert-btn")
                            yield Button(
                                "Travel", id="autoreply-fast-travel", classes="insert-btn"
                            )
                            yield Button(
                                "Return", id="autoreply-return", classes="insert-btn"
                            )
                        with Horizontal(classes="field-row"):
                            yield Button(
                                "Autodiscover", id="autoreply-autodiscover", classes="insert-btn"
                            )
                            yield Button(
                                "Random Walk", id="autoreply-btn-randomwalk", classes="insert-btn"
                            )
                        with Horizontal(id="autoreply-form-buttons", classes="edit-form-buttons"):
                            yield Label(" ", classes="form-btn-spacer")
                            yield Button("Cancel", variant="default", id="autoreply-cancel-form")
                            yield Button("OK", variant="success", id="autoreply-ok")
                    yield Static("", id="autoreply-count")

    def on_mount(self) -> None:
        """Load autoreplies from file and populate table."""
        table = self.query_one("#autoreply-table", DataTable)
        table.cursor_type = "row"
        term_w = self.app.size.width
        col_w = max(15, 15 + (term_w - 80) // 2) if term_w > 80 else 15
        table.add_column("#", width=4, key="num")
        table.add_column("Pattern", width=col_w, key="pattern")
        table.add_column("Reply", width=col_w, key="reply")
        table.add_column("Flags", width=8, key="flags")
        table.add_column("Last", width=8, key="last")
        self._load_from_file()
        self._refresh_table()
        self.query_one("#autoreply-form").display = False
        if self._select_pattern:
            for i, (pattern, *_rest) in enumerate(self._rules):
                if pattern == self._select_pattern:
                    table.move_cursor(row=i)
                    break

    def _load_from_file(self) -> None:
        if not os.path.exists(self._path):
            return
        from .autoreply import load_autoreplies

        try:
            rules = load_autoreplies(self._path, self._session_key)
            self._rules = [
                _AutoreplyTuple(
                    r.pattern.pattern,
                    r.reply,
                    r.always,
                    r.enabled,
                    dict(r.when) or None,
                    r.immediate,
                    r.last_fired,
                    r.case_sensitive,
                )
                for r in rules
            ]
        except (ValueError, FileNotFoundError):
            pass

    def _matches_search(self, idx: int, query: str) -> bool:
        """Match autoreply pattern or reply against search query."""
        rule = self._rules[idx]
        q = query.lower()
        return q in rule.pattern.lower() or q in rule.reply.lower()

    def _refresh_table(self) -> None:
        table = self.query_one("#autoreply-table", DataTable)
        table.clear()
        q = self._search_query
        self._filtered_indices = []
        order = list(range(len(self._rules)))
        if self._sort_mode == "last_fired":
            order.sort(key=lambda i: _invert_ts(self._rules[i].last_fired))
        for i in order:
            rule = self._rules[i]
            if q and not self._matches_search(i, q):
                continue
            self._filtered_indices.append(i)
            flags = " ".join(filter(None, [
                "X" if not rule.enabled else "",
                "A" if rule.always else "",
                "I" if rule.immediate else "",
                "C" if rule.case_sensitive else "",
                "W" if rule.when else "",
            ]))
            lf = _relative_time(rule.last_fired) if rule.last_fired else ""
            row_pos = len(self._filtered_indices) - 1
            table.add_row(str(i + 1), rule.pattern, rule.reply, flags.strip(), lf, key=str(row_pos))
        self._update_count_label()

    def action_sort_last(self) -> None:
        """Toggle sorting autoreplies by last fired time."""
        self._sort_mode = "last_fired" if self._sort_mode != "last_fired" else ""
        self._refresh_table()

    def _show_form(
        self,
        pattern_val: str = "",
        reply_val: str = "",
        always: bool = False,
        enabled: bool = True,
        when: dict[str, str] | None = None,
        immediate: bool = False,
        last_fired: str = "",
        case_sensitive: bool = False,
    ) -> None:
        self.query_one("#autoreply-pattern", Input).value = pattern_val
        self.query_one("#autoreply-reply", Input).value = reply_val
        self.query_one("#autoreply-always", Switch).value = always
        self.query_one("#autoreply-enabled", Switch).value = enabled
        self.query_one("#autoreply-immediate", Switch).value = immediate
        self.query_one("#autoreply-case-sensitive", Switch).value = case_sensitive
        cond_vital, cond_op, cond_val = "", ">", "99"
        if when:
            vital = next(iter(when), "")
            expr = when.get(vital, ">99")
            import re as _re

            if (m := _re.match(r"^(>=|<=|>|<|=)(\d+)$", expr)):
                cond_vital, cond_op, cond_val = vital, m.group(1), m.group(2)
        self.query_one("#autoreply-cond-vital", Select).value = cond_vital
        self.query_one("#autoreply-cond-op", Select).value = cond_op
        self.query_one("#autoreply-cond-val", Input).value = cond_val
        cond_none = not when
        self.query_one("#autoreply-cond-op", Select).disabled = cond_none
        self.query_one("#autoreply-cond-val", Input).disabled = cond_none
        self.query_one("#autoreply-search", Input).display = False
        self.query_one("#autoreply-table").display = False
        self.query_one("#autoreply-form").display = True
        self._set_action_buttons_disabled(True)
        self.query_one("#autoreply-pattern", Input).focus()

    def _submit_form(self) -> None:
        """Accept the current inline form values."""
        pattern_val = self.query_one("#autoreply-pattern", Input).value.strip()
        reply_val = self.query_one("#autoreply-reply", Input).value
        always = self.query_one("#autoreply-always", Switch).value
        enabled = self.query_one("#autoreply-enabled", Switch).value
        immediate = self.query_one("#autoreply-immediate", Switch).value
        case_sensitive = self.query_one("#autoreply-case-sensitive", Switch).value
        cond_vital = self.query_one("#autoreply-cond-vital", Select).value
        cond_op = self.query_one("#autoreply-cond-op", Select).value
        cond_val = self.query_one("#autoreply-cond-val", Input).value.strip()
        when: dict[str, str] | None = None
        if cond_vital and isinstance(cond_vital, str) and cond_vital in ("HP%", "MP%", "HP", "MP"):
            try:
                int(cond_val or "99")
            except ValueError:
                cond_val = "99"
            when = {cond_vital: f"{cond_op}{cond_val or '99'}"}
        if pattern_val:
            import re

            try:
                re.compile(pattern_val)
            except re.error as exc:
                self.notify(f"Invalid regex: {exc}", severity="error")
                return
        lf = self._rules[self._editing_idx].last_fired if self._editing_idx is not None else ""
        entry = _AutoreplyTuple(
            pattern_val, reply_val, always, enabled, when, immediate, lf, case_sensitive
        )
        self._finalize_edit(entry, bool(pattern_val))

    def on_select_changed(self, event: Select.Changed) -> None:
        """Disable operator/value fields when condition vital is '(none)'."""
        if event.select.id == "autoreply-cond-vital":
            disabled = not event.value or event.value is Select.BLANK
            self.query_one("#autoreply-cond-op", Select).disabled = disabled
            self.query_one("#autoreply-cond-val", Input).disabled = disabled

    def _on_extra_button(self, suffix: str, btn: str) -> None:
        """Handle autoreply-specific buttons (travel, etc.)."""
        if suffix == "fast-travel":
            self._pick_room_for_travel()
        else:
            super()._on_extra_button(suffix, btn)

    def _save_to_file(self) -> None:
        import re

        from .autoreply import AutoreplyRule, save_autoreplies

        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        rules = []
        for t in self._rules:
            flags = re.MULTILINE | re.DOTALL
            if not t.case_sensitive:
                flags |= re.IGNORECASE
            rules.append(
                AutoreplyRule(
                    pattern=re.compile(t.pattern, flags),
                    reply=t.reply,
                    always=t.always,
                    enabled=t.enabled,
                    when=t.when or {},
                    immediate=t.immediate,
                    last_fired=t.last_fired,
                    case_sensitive=t.case_sensitive,
                )
            )
        save_autoreplies(self._path, rules, self._session_key)


class AutoreplyEditScreen(_EditListScreen):
    """Thin screen wrapper for the autoreply editor."""

    def __init__(self, path: str, session_key: str = "", select_pattern: str = "") -> None:
        super().__init__()
        self._pane = AutoreplyEditPane(
            path=path, session_key=session_key, select_pattern=select_pattern,
        )


class _HighlightTuple(NamedTuple):
    """Lightweight tuple for highlight rules in the TUI editor."""

    pattern: str
    highlight: str
    enabled: bool = True
    stop_movement: bool = False
    builtin: bool = False
    case_sensitive: bool = False
    multiline: bool = False
    captured: bool = False
    capture_name: str = "captures"
    captures: tuple[dict[str, str], ...] = ()


class HighlightEditPane(_EditListPane):
    """Pane widget for highlight rule editing."""

    _growable_keys: list[str] = ["pattern"]

    DEFAULT_CSS = _EditListPane.DEFAULT_CSS + """
    #highlight-form { padding: 0 0 0 4; }
    #highlight-form .form-label { width: 12; }
    """

    def __init__(self, path: str, session_key: str = "") -> None:
        """Initialize highlight editor for *path*."""
        super().__init__()
        self._path = path
        self._session_key = session_key
        self._rules: list[_HighlightTuple] = []

    @property
    def _prefix(self) -> str:
        return "highlight"

    @property
    def _noun(self) -> str:
        return "Highlight"

    @property
    def _items(self) -> list[Any]:
        return self._rules

    def _item_label(self, idx: int) -> str:
        if idx < len(self._rules):
            return self._rules[idx].pattern
        return ""

    def compose(self) -> ComposeResult:
        """Build the highlight editor widget tree."""
        with Vertical(id="highlight-panel", classes="edit-panel"):
            with Horizontal(id="highlight-body", classes="edit-body"):
                with Vertical(id="highlight-button-col", classes="edit-button-col"):
                    yield Button("Add", variant="success", id="highlight-add")
                    yield Button("Edit", variant="warning", id="highlight-edit")
                    yield Button("Copy", id="highlight-copy", classes="edit-copy")
                    yield Button("Delete", variant="error", id="highlight-delete")
                    yield Button("Help", variant="success", id="highlight-help")
                    yield Button("Save", variant="primary", id="highlight-save")
                    yield Button("Cancel", id="highlight-close")
                with Vertical(id="highlight-right", classes="edit-right"):
                    yield Input(
                        placeholder="Search highlights\u2026",
                        id="highlight-search",
                        classes="edit-search",
                    )
                    yield DataTable(id="highlight-table", classes="edit-table")
                    with Vertical(id="highlight-form", classes="edit-form"):
                        with Horizontal(classes="field-row"):
                            yield Label("Enabled:", classes="toggle-label")
                            yield Switch(value=True, id="highlight-enabled")
                            yield Label("", classes="toggle-gap")
                            sm = Switch(value=False, id="highlight-stop-movement")
                            sm.tooltip = "Cancel autodiscover/randomwalk when matched"
                            yield Label("Stop:", classes="toggle-label")
                            yield sm
                            yield Label("", classes="toggle-gap")
                            cs = Switch(value=False, id="highlight-case-sensitive")
                            cs.tooltip = "Case-sensitive pattern matching"
                            yield Label("Case Sensitive:", classes="toggle-label")
                            yield cs
                            yield Label("", classes="toggle-gap")
                            ml = Switch(value=False, id="highlight-multiline")
                            ml.tooltip = "Match pattern across multiple lines"
                            yield Label("Multiline:", classes="toggle-label")
                            yield ml
                        with Horizontal(classes="field-row"):
                            cap_sw = Switch(value=False, id="highlight-captured")
                            cap_sw.tooltip = "Capture regex groups into variables"
                            yield Label("Captured:", classes="toggle-label")
                            yield cap_sw
                            yield Label("", classes="toggle-gap")
                            yield Label("Capture Name:", classes="toggle-label")
                            yield Input(
                                value="captures",
                                placeholder="channel name",
                                id="highlight-capture-name",
                            )
                        with Horizontal(classes="field-row"):
                            yield Label("Pattern", classes="form-label-short")
                            yield Input(placeholder="regex pattern", id="highlight-pattern")
                        with Horizontal(classes="field-row"):
                            yield Label("Highlight", classes="form-label-short")
                            yield Input(
                                placeholder="eg. blink_black_on_yellow", id="highlight-style"
                            )
                        with Vertical(id="highlight-capture-fields"):
                            with Vertical(id="highlight-captures-container"):
                                pass
                            yield Button(
                                "Add Capture", variant="default", id="highlight-add-capture"
                            )
                        with Horizontal(id="highlight-form-buttons", classes="edit-form-buttons"):
                            yield Label(" ", classes="form-btn-spacer")
                            yield Button("Cancel", variant="default", id="highlight-cancel-form")
                            yield Button("OK", variant="success", id="highlight-ok")
                    yield Static("", id="highlight-count")

    def on_mount(self) -> None:
        """Configure highlight table columns and load rules from file."""
        table = self.query_one("#highlight-table", DataTable)
        table.cursor_type = "row"
        term_w = shutil.get_terminal_size(fallback=(80, 24)).columns
        pat_w = max(15, 15 + (term_w - 80))
        table.add_column("#", width=4, key="num")
        table.add_column("Pattern", width=pat_w, key="pattern")
        table.add_column("Highlight", width=24, key="highlight")
        table.add_column("Flags", width=6, key="flags")
        self._load_from_file()
        self._refresh_table()
        self.query_one("#highlight-form").display = False
        self.query_one("#highlight-capture-fields").display = False

    _AUTOREPLY_PATTERN = "<Autoreply pattern>"

    def _ensure_builtin(self) -> None:
        """Ensure the builtin autoreply highlight rule exists."""
        from .highlighter import _DEFAULT_AUTOREPLY_HIGHLIGHT

        if not any(r.builtin for r in self._rules):
            self._rules.insert(
                0,
                _HighlightTuple(
                    self._AUTOREPLY_PATTERN,
                    _DEFAULT_AUTOREPLY_HIGHLIGHT,
                    enabled=True,
                    stop_movement=False,
                    builtin=True,
                ),
            )

    def _load_from_file(self) -> None:
        if not os.path.exists(self._path):
            self._ensure_builtin()
            return
        from .highlighter import load_highlights

        try:
            rules = load_highlights(self._path, self._session_key)
            self._rules = [
                _HighlightTuple(
                    r.pattern.pattern,
                    r.highlight,
                    r.enabled,
                    r.stop_movement,
                    r.builtin,
                    r.case_sensitive,
                    r.multiline,
                    r.captured,
                    r.capture_name,
                    tuple(r.captures),
                )
                for r in rules
            ]
        except (ValueError, FileNotFoundError):
            pass
        self._ensure_builtin()

    def _matches_search(self, idx: int, query: str) -> bool:
        rule = self._rules[idx]
        q = query.lower()
        return q in rule.pattern.lower() or q in rule.highlight.lower()

    def _refresh_table(self) -> None:
        table = self.query_one("#highlight-table", DataTable)
        table.clear()
        q = self._search_query
        self._filtered_indices = []
        for i, rule in enumerate(self._rules):
            if q and not self._matches_search(i, q):
                continue
            self._filtered_indices.append(i)
            flags = " ".join(filter(None, [
                "X" if not rule.enabled else "",
                "S" if rule.stop_movement else "",
                "CS" if rule.case_sensitive else "",
                "M" if rule.multiline else "",
                "C" if rule.captured else "",
            ]))
            row_pos = len(self._filtered_indices) - 1
            table.add_row(str(i + 1), rule.pattern, rule.highlight, flags.strip(), key=str(row_pos))
        self._update_count_label()

    def _show_form(
        self,
        pattern_val: str = "",
        highlight_val: str = "",
        enabled: bool = True,
        stop_movement: bool = False,
        builtin: bool = False,
        case_sensitive: bool = False,
        multiline: bool = False,
        captured: bool = False,
        capture_name: str = "captures",
        captures: tuple[dict[str, str], ...] = (),
    ) -> None:
        pat_input = self.query_one("#highlight-pattern", Input)
        pat_input.value = pattern_val
        pat_input.disabled = builtin
        self.query_one("#highlight-style", Input).value = highlight_val
        self.query_one("#highlight-enabled", Switch).value = enabled
        stop_sw = self.query_one("#highlight-stop-movement", Switch)
        stop_sw.value = stop_movement
        stop_sw.disabled = builtin
        cs_sw = self.query_one("#highlight-case-sensitive", Switch)
        cs_sw.value = case_sensitive
        cs_sw.disabled = builtin
        ml_sw = self.query_one("#highlight-multiline", Switch)
        ml_sw.value = multiline
        ml_sw.disabled = builtin
        cap_sw = self.query_one("#highlight-captured", Switch)
        cap_sw.value = captured
        cap_sw.disabled = builtin
        self.query_one("#highlight-capture-name", Input).value = capture_name
        container = self.query_one("#highlight-captures-container", Vertical)
        container.remove_children()
        for cap in captures:
            self._add_capture_row(container, cap.get("key", "KeyName"), cap.get("value", r"\1"))
        cap_fields = self.query_one("#highlight-capture-fields", Vertical)
        cap_fields.display = captured
        self.query_one("#highlight-search", Input).display = False
        self.query_one("#highlight-table").display = False
        self.query_one("#highlight-form").display = True
        self._set_action_buttons_disabled(True)
        if builtin:
            self.query_one("#highlight-style", Input).focus()
        else:
            pat_input.focus()

    def _add_capture_row(
        self, container: Vertical, key: str = "KeyName", value: str = r"\1"
    ) -> None:
        """Append a key/value capture row to the captures container."""
        row_id = f"cap-row-{id(container)}-{len(container.children)}"
        btn = Button("X", variant="error", classes="capture-remove")
        btn.styles.width = 5
        row = Horizontal(
            Input(value=key, placeholder="Key", classes="capture-key"),
            Input(value=value, placeholder="Value", classes="capture-value"),
            btn,
            id=row_id,
            classes="capture-row",
        )
        container.mount(row)

    def _on_captured_toggle(self, value: bool) -> None:
        """Show or hide capture fields when Captured switch changes."""
        cap_fields = self.query_one("#highlight-capture-fields", Vertical)
        cap_fields.display = value

    def _submit_form(self) -> None:
        import re as _re

        pattern_val = self.query_one("#highlight-pattern", Input).value.strip()
        highlight_val = self.query_one("#highlight-style", Input).value.strip()
        enabled = self.query_one("#highlight-enabled", Switch).value
        stop_movement = self.query_one("#highlight-stop-movement", Switch).value
        case_sensitive = self.query_one("#highlight-case-sensitive", Switch).value
        multiline = self.query_one("#highlight-multiline", Switch).value
        if pattern_val:
            try:
                _re.compile(pattern_val)
            except _re.error as exc:
                self.notify(f"Invalid regex: {exc}", severity="error")
                return
            for i, existing in enumerate(self._rules):
                if (
                    i != self._editing_idx
                    and not existing.builtin
                    and existing.pattern == pattern_val
                ):
                    self.notify(f"Duplicate pattern: {pattern_val!r}", severity="error")
                    return
        if highlight_val:
            from .client_repl import _get_term
            from .highlighter import validate_highlight

            try:
                term = _get_term()
            except ImportError:
                term = None
            if term is not None and not validate_highlight(term, highlight_val):
                self.notify(f"Invalid highlight style: {highlight_val!r}", severity="error")
                return
        captured = self.query_one("#highlight-captured", Switch).value
        capture_name = self.query_one("#highlight-capture-name", Input).value.strip()
        if not capture_name:
            capture_name = "captures"
        captures_list: list[dict[str, str]] = []
        container = self.query_one("#highlight-captures-container", Vertical)
        for row in container.children:
            inputs = list(row.query(Input))
            if len(inputs) >= 2:
                k = inputs[0].value.strip()
                v = inputs[1].value.strip()
                if k and v:
                    captures_list.append({"key": k, "value": v})
        builtin = False
        if self._editing_idx is not None:
            builtin = self._rules[self._editing_idx].builtin
        entry = _HighlightTuple(
            pattern_val,
            highlight_val,
            enabled,
            stop_movement,
            builtin,
            case_sensitive,
            multiline,
            captured,
            capture_name,
            tuple(captures_list),
        )
        self._finalize_edit(entry, bool(pattern_val and highlight_val))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Toggle capture fields visibility when Captured switch changes."""
        if event.switch.id == "highlight-captured":
            self._on_captured_toggle(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses for add, edit, delete, save, and cancel."""
        btn = event.button.id or ""
        if btn == "highlight-delete":
            idx = self._selected_idx()
            if idx is not None and idx < len(self._rules) and self._rules[idx].builtin:
                self.notify("Cannot delete the builtin autoreply highlight rule.")
                return
        if btn == "highlight-add-capture":
            container = self.query_one("#highlight-captures-container", Vertical)
            n = len(container.children)
            self._add_capture_row(container, "KeyName", f"\\{n + 1}")
            return
        if "capture-remove" in (event.button.classes or set()):
            row = event.button.parent
            if row is not None:
                row.remove()
            return
        super().on_button_pressed(event)

    def _save_to_file(self) -> None:
        import re as _re

        from .highlighter import HighlightRule, save_highlights

        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        rules = []
        for t in self._rules:
            flags = _re.MULTILINE | _re.DOTALL
            if not t.case_sensitive:
                flags |= _re.IGNORECASE
            rules.append(
                HighlightRule(
                    pattern=_re.compile(t.pattern, flags),
                    highlight=t.highlight,
                    enabled=t.enabled,
                    stop_movement=t.stop_movement,
                    builtin=t.builtin,
                    case_sensitive=t.case_sensitive,
                    multiline=t.multiline,
                    captured=t.captured,
                    capture_name=t.capture_name,
                    captures=list(t.captures),
                )
            )
        save_highlights(self._path, rules, self._session_key)


class HighlightEditScreen(_EditListScreen):
    """Thin screen wrapper for the highlight editor."""

    def __init__(self, path: str, session_key: str = "") -> None:
        super().__init__()
        self._pane = HighlightEditPane(path=path, session_key=session_key)


class _ProgressBarTuple(NamedTuple):
    """Lightweight tuple for progress bar configs in the TUI editor."""

    name: str = ""
    gmcp_package: str = ""
    value_field: str = ""
    max_field: str = ""
    enabled: bool = True
    color_mode: str = "theme"
    color_name_max: str = "success"
    color_name_min: str = "error"
    color_path: str = "shortest"
    text_color_fill: str = "auto"
    text_color_empty: str = "auto"
    display_order: int = 0
    side: str = "left"


class ProgressBarEditPane(_EditListPane):
    """Pane widget for GMCP progress bar configuration."""

    _growable_keys: list[str] = ["name"]

    DEFAULT_CSS = _EditListPane.DEFAULT_CSS + """
    #progressbar-form { padding: 0 0 0 4; }
    #progressbar-form .form-label { width: 12; }
    #progressbar-form Input { max-width: 50; }
    #progressbar-form Select { max-width: 50; }
    #pb-gmcp-select { max-width: 18; }
    #pb-value-select { max-width: 18; }
    #pb-max-select { max-width: 18; }
    #pb-color-swatch-max { width: 4; height: 1; padding-top: 1; }
    #pb-color-swatch-min { width: 4; height: 1; padding-top: 1; }
    #pb-color-row1 { height: auto; margin: 0; }
    #pb-color-row2 { height: auto; margin: 0; }
    #pb-color-row3 { height: auto; margin: 0; }
    #pb-color-mode { max-width: 20; }
    #pb-color-max { width: 28; }
    #pb-color-min { width: 28; }
    #pb-text-min { width: 28; }
    #pb-text-max { width: 28; }
    #pb-color-path { max-width: 20; }
    #pb-side { max-width: 20; }
    #pb-preview-bar { height: 1; margin: 0 0 0 12; }
    #pb-preview-gradient { height: 1; margin: 0 0 0 12; }
    """

    def __init__(self, path: str, session_key: str = "", gmcp_snapshot_path: str = "") -> None:
        """Initialize progress bar editor."""
        super().__init__()
        self._path = path
        self._session_key = session_key
        self._gmcp_snapshot_path = gmcp_snapshot_path
        self._bars: list[_ProgressBarTuple] = []
        self._gmcp_packages: list[str] = []
        self._gmcp_fields: dict[str, list[str]] = {}
        self._preview_timer: Any = None
        self._preview_phase: float = 0.0
        self._populating_form: bool = False
        self._form_source_pkg: str = ""
        self._form_color_mode: str = ""

    @property
    def _prefix(self) -> str:
        return "progressbar"

    @property
    def _noun(self) -> str:
        return "Progress Bar"

    @property
    def _noun_plural(self) -> str:
        return "Progress Bars"

    @property
    def _items(self) -> list[Any]:
        return self._bars

    def _item_label(self, idx: int) -> str:
        if idx < len(self._bars):
            return self._bars[idx].name
        return ""

    def _load_gmcp_packages(self) -> None:
        """Load GMCP package names and field names from the snapshot file."""
        if not self._gmcp_snapshot_path:
            return
        if not os.path.exists(self._gmcp_snapshot_path):
            return
        with open(self._gmcp_snapshot_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        packages = data.get("packages", {}) if isinstance(data, dict) else {}
        if packages:
            self._gmcp_packages = sorted(packages.keys())
            for pkg_name, pkg_info in packages.items():
                pkg_data = pkg_info.get("data", {}) if isinstance(pkg_info, dict) else {}
                if isinstance(pkg_data, dict):
                    self._gmcp_fields[pkg_name] = sorted(pkg_data.keys())

    def compose(self) -> ComposeResult:
        """Build the progress bar editor widget tree."""
        pfx = "progressbar"
        with Vertical(id=f"{pfx}-panel", classes="edit-panel"):
            with Horizontal(id=f"{pfx}-body", classes="edit-body"):
                with Vertical(id=f"{pfx}-button-col", classes="edit-button-col"):
                    yield Button("Add", variant="success", id=f"{pfx}-add")
                    yield Button("Edit", variant="warning", id=f"{pfx}-edit")
                    yield Button("Copy", id=f"{pfx}-copy", classes="edit-copy")
                    yield Button("Detect", variant="primary", id=f"{pfx}-detect")
                    yield Button("Help", variant="success", id=f"{pfx}-help")
                    yield Button("Save", variant="primary", id=f"{pfx}-save")
                    yield Button("Cancel", id=f"{pfx}-close")
                with Vertical(id=f"{pfx}-right", classes="edit-right"):
                    yield Input(
                        placeholder="Search bars\u2026", id=f"{pfx}-search", classes="edit-search"
                    )
                    yield DataTable(id=f"{pfx}-table", classes="edit-table")
                    with VerticalScroll(id=f"{pfx}-form", classes="edit-form"):
                        with Horizontal(classes="field-row"):
                            yield Label("Name", classes="form-label")
                            yield Input(placeholder="bar name", id="pb-name")
                            yield Label("", classes="toggle-gap")
                            yield Label("Enabled:", classes="toggle-label")
                            yield Switch(value=True, id="pb-enabled")
                        with Horizontal(classes="field-row"):
                            yield Label("Source", classes="form-label")
                            yield Select[str](
                                [], id="pb-gmcp-select", allow_blank=True,
                                prompt="Select source\u2026",
                            )
                        with Horizontal(classes="field-row"):
                            yield Label("Val/Max", classes="form-label")
                            yield Select[str](
                                [], id="pb-value-select", allow_blank=True,
                                prompt="Value field\u2026",
                            )
                            yield Label("", classes="form-gap")
                            yield Select[str](
                                [], id="pb-max-select", allow_blank=True,
                                prompt="Max field\u2026",
                            )
                        with Horizontal(id="pb-color-row1", classes="field-row"):
                            yield Label("Color Mode", classes="form-label")
                            with RadioSet(id="pb-color-mode"):
                                yield RadioButton("Theme", id="pb-mode-theme", value=True)
                                yield RadioButton("Custom", id="pb-mode-custom")
                            yield Label("", id="pb-color-gap1", classes="form-gap")
                            yield Label("Path", classes="form-label")
                            with RadioSet(id="pb-color-path"):
                                yield RadioButton(
                                    "Shortest", id="pb-path-shortest", value=True,
                                )
                                yield RadioButton("Longest", id="pb-path-longest")
                        with Horizontal(classes="field-row"):
                            yield Label("Side", classes="form-label")
                            with RadioSet(id="pb-side"):
                                yield RadioButton("Left", id="pb-side-left", value=True)
                                yield RadioButton("Right", id="pb-side-right")
                        with Horizontal(id="pb-color-row2", classes="field-row"):
                            yield Label("Min:", classes="form-label")
                            yield Select[str](
                                self._theme_color_options(),
                                id="pb-color-min",
                                value="error",
                                allow_blank=False,
                            )
                            yield Static("", id="pb-color-swatch-min")
                            yield Select[str](
                                self._text_color_options(False),
                                id="pb-text-min",
                                value="auto",
                                allow_blank=False,
                            )
                        with Horizontal(id="pb-color-row3", classes="field-row"):
                            yield Label("Max:", classes="form-label")
                            yield Select[str](
                                self._theme_color_options(),
                                id="pb-color-max",
                                value="success",
                                allow_blank=False,
                            )
                            yield Static("", id="pb-color-swatch-max")
                            yield Select[str](
                                self._text_color_options(False),
                                id="pb-text-max",
                                value="auto",
                                allow_blank=False,
                            )
                        yield Static("", id="pb-preview-bar")
                        yield Static("", id="pb-preview-gradient")
                        with Horizontal(id=f"{pfx}-form-buttons", classes="edit-form-buttons"):
                            yield Label(" ", classes="form-btn-spacer")
                            yield Button("Cancel", variant="default", id=f"{pfx}-cancel-form")
                            yield Button("OK", variant="success", id=f"{pfx}-ok")
                    yield Static("", id=f"{pfx}-count")

    @staticmethod
    def _color_options() -> "list[tuple[RichText, str]]":
        """Build Select options from all Rich named colors with swatches."""
        from rich.text import Text

        from .progressbars import CURATED_COLORS

        options: list[tuple[Text, str]] = []
        for c in CURATED_COLORS:
            label = Text()
            label.append("\u2588 ", style=c)
            label.append(c)
            options.append((label, c))
        return options

    @staticmethod
    def _theme_color_options() -> "list[tuple[RichText, str]]":
        """Build Select options from Textual theme colors with swatches."""
        from rich.text import Text

        from .progressbars import get_theme_colors

        options: list[tuple[Text, str]] = []
        for name, hex_val in get_theme_colors().items():
            label = Text()
            label.append("\u2588 ", style=hex_val[:7])
            label.append(name)
            options.append((label, name))
        return options

    @staticmethod
    def _text_color_options(is_custom: bool) -> "list[tuple[RichText, str]]":
        """Build Select options for text color: ``auto`` plus bar color options."""
        from rich.text import Text

        auto_label = Text()
        auto_label.append("auto")
        options: list[tuple[Text, str]] = [(auto_label, "auto")]
        if is_custom:
            options.extend(ProgressBarEditPane._color_options())
        else:
            options.extend(ProgressBarEditPane._theme_color_options())
        return options

    def on_mount(self) -> None:
        """Configure table columns and load bars from file."""
        table = self.query_one("#progressbar-table", DataTable)
        table.cursor_type = "row"
        term_w = shutil.get_terminal_size(fallback=(80, 24)).columns
        name_w = min(20, max(12, 12 + (term_w - 80)))
        table.add_column("#", width=4, key="num")
        table.add_column("Name", width=name_w, key="name")
        table.add_column("Source", width=20, key="source")
        table.add_column("Value", width=14, key="value")
        table.add_column("Max", width=14, key="max")
        table.add_column("Enabled", width=8, key="enabled")
        table.add_column("Color", width=8, key="color")
        self._load_gmcp_packages()
        self._load_from_file()
        self._do_detect(silent=True)
        self._refresh_table()
        self.query_one("#progressbar-form").display = False

    def _load_from_file(self) -> None:
        if not os.path.exists(self._path):
            return
        from .progressbars import load_progressbars

        bars = load_progressbars(self._path, self._session_key)
        self._bars = [_ProgressBarTuple(*b) for b in bars]

    def _matches_search(self, idx: int, query: str) -> bool:
        bar = self._bars[idx]
        q = query.lower()
        return q in bar.name.lower() or q in bar.gmcp_package.lower()

    @staticmethod
    def _color_swatch(bar: _ProgressBarTuple) -> "RichText":
        """Build a gradient-colored label for the Color column."""
        from rich.text import Text

        from .progressbars import BarConfig, bar_color_at

        cfg = BarConfig(
            bar.name, bar.gmcp_package, bar.value_field, bar.max_field,
            color_mode=bar.color_mode, color_name_max=bar.color_name_max,
            color_name_min=bar.color_name_min, color_path=bar.color_path,
        )
        label = " theme " if bar.color_mode == "theme" else " custom"
        swatch = Text()
        for i, ch in enumerate(label):
            f = i / max(1, len(label) - 1)
            c = bar_color_at(f, cfg)
            swatch.append(ch, style=f"bold {c}")
        return swatch

    def _refresh_table(self) -> None:
        table = self.query_one("#progressbar-table", DataTable)
        table.clear()
        q = self._search_query
        self._filtered_indices = []
        for i, bar in enumerate(self._bars):
            if q and not self._matches_search(i, q):
                continue
            self._filtered_indices.append(i)
            enabled = "Yes" if bar.enabled else "No"
            display_name = bar.name if len(bar.name) <= 19 else bar.name[:18] + "\u2026"
            val_f = (bar.value_field if len(bar.value_field) <= 13
                     else bar.value_field[:12] + "\u2026")
            max_f = bar.max_field if len(bar.max_field) <= 13 else bar.max_field[:12] + "\u2026"
            swatch = self._color_swatch(bar)
            row_pos = len(self._filtered_indices) - 1
            table.add_row(
                str(i + 1), display_name, bar.gmcp_package,
                val_f, max_f,
                enabled, swatch, key=str(row_pos),
            )
        self._update_count_label()

    def _show_form(
        self,
        name: str = "",
        gmcp_package: str = "",
        value_field: str = "",
        max_field: str = "",
        enabled: bool = True,
        color_mode: str = "theme",
        color_name_max: str = "green",
        color_name_min: str = "red",
        color_path: str = "shortest",
        text_color_fill: str = "auto",
        text_color_empty: str = "auto",
        display_order: int = 0,
        side: str = "left",
    ) -> None:
        from .progressbars import TRAVEL_BAR_NAME

        is_travel = name == TRAVEL_BAR_NAME or (name and not gmcp_package)
        self._populating_form = True
        self.query_one("#pb-name", Input).value = name
        self.query_one("#pb-enabled", Switch).value = enabled
        # Populate source dropdown
        pkg_options = [(p, p) for p in self._gmcp_packages]
        src_sel = self.query_one("#pb-gmcp-select", Select)
        src_sel.set_options(pkg_options)
        if gmcp_package and gmcp_package in self._gmcp_packages:
            src_sel.value = gmcp_package
        elif gmcp_package:
            src_sel.set_options(pkg_options + [(gmcp_package, gmcp_package)])
            src_sel.value = gmcp_package
        # Disable source fields for Travel bar
        src_sel.disabled = is_travel
        val_sel = self.query_one("#pb-value-select", Select)
        max_sel = self.query_one("#pb-max-select", Select)
        val_sel.disabled = is_travel
        max_sel.disabled = is_travel
        # Populate value/max field dropdowns
        self._form_source_pkg = gmcp_package
        self._populate_field_selects(gmcp_package, value_field, max_field)
        self._populating_form = False
        # Color mode -- set the desired button to on; RadioSet will turn the
        # other off via its internal _on_radio_button_changed handler.
        is_custom = color_mode == "custom"
        self._form_color_mode = color_mode
        target = "#pb-mode-custom" if is_custom else "#pb-mode-theme"
        self.query_one(target, RadioButton).value = True
        # Color selects -- populate with theme or custom options
        self._swap_color_options(
            is_custom, preserve_max=color_name_max, preserve_min=color_name_min,
            preserve_text_fill=text_color_fill, preserve_text_empty=text_color_empty,
        )
        # Values already set by _swap_color_options with proper fallbacks.
        # Color path
        is_longest = color_path == "longest"
        path_target = "#pb-path-longest" if is_longest else "#pb-path-shortest"
        self.query_one(path_target, RadioButton).value = True
        # Side
        side_target = "#pb-side-right" if side == "right" else "#pb-side-left"
        self.query_one(side_target, RadioButton).value = True
        # Show form + start preview animation
        self.query_one("#progressbar-search", Input).display = False
        self.query_one("#progressbar-table").display = False
        self.query_one("#progressbar-form").display = True
        self._set_action_buttons_disabled(True)
        self._preview_phase = 0.0
        self._start_preview_timer()
        self.query_one("#pb-name", Input).focus()

    def _submit_form(self) -> None:
        name = self.query_one("#pb-name", Input).value.strip()
        if not name:
            self.notify("Name is required.", severity="error")
            return
        enabled = self.query_one("#pb-enabled", Switch).value
        gmcp_pkg = self._get_select_value("#pb-gmcp-select", "")
        value_field = self._get_select_value("#pb-value-select", "")
        max_field = self._get_select_value("#pb-max-select", "")
        color_mode = self._get_color_mode()
        color_name_max = self._get_select_value("#pb-color-max", "green")
        color_name_min = self._get_select_value("#pb-color-min", "red")
        color_path = (
            "longest" if self.query_one("#pb-path-longest", RadioButton).value else "shortest"
        )
        text_color_fill = self._get_select_value("#pb-text-min", "auto")
        text_color_empty = self._get_select_value("#pb-text-max", "auto")
        side = (
            "right" if self.query_one("#pb-side-right", RadioButton).value else "left"
        )
        order = 0
        if self._editing_idx is not None:
            order = self._bars[self._editing_idx].display_order
        entry = _ProgressBarTuple(
            name=name,
            gmcp_package=gmcp_pkg,
            value_field=value_field,
            max_field=max_field,
            enabled=enabled,
            color_mode=color_mode,
            color_name_max=color_name_max,
            color_name_min=color_name_min,
            color_path=color_path,
            text_color_fill=text_color_fill,
            text_color_empty=text_color_empty,
            display_order=order,
            side=side,
        )
        self._finalize_edit(entry, True)

    def _get_color_mode(self) -> str:
        """Return the selected color mode string."""
        if self.query_one("#pb-mode-custom", RadioButton).value:
            return "custom"
        return "theme"

    def _get_select_value(self, sel_id: str, default: str) -> str:
        """Return a Select widget's value as str."""
        sel = self.query_one(sel_id, Select)
        val = sel.value
        if isinstance(val, str):
            return val
        return default

    def _update_swatch(self, widget_id: str, color_name: str) -> None:
        """Update a color swatch Static with a colored block."""
        try:
            swatch = self.query_one(widget_id, Static)
            swatch.update(f"[on {color_name}]    [/]")
        except NoMatches:
            pass

    def _hide_form(self) -> None:
        self._stop_preview_timer()
        self._form_source_pkg = ""
        self._form_color_mode = ""
        super()._hide_form()

    def _start_preview_timer(self) -> None:
        """Start the animated preview at ~15 fps."""
        self._stop_preview_timer()
        self._preview_phase = 0.0
        self._preview_timer = self.set_interval(1.0 / 15.0, self._tick_preview)

    def _stop_preview_timer(self) -> None:
        """Stop the animated preview."""
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None

    def _tick_preview(self) -> None:
        """Advance the preview animation phase and redraw."""
        self._preview_phase += (1.0 / 15.0) / 13.5
        if self._preview_phase >= 1.0:
            self._preview_phase -= 1.0
        self._update_preview()

    @staticmethod
    def _ease_in_out(t: float) -> float:
        """Smooth ease-in-out: very slow at ends, faster in middle."""
        # Quintic smootherstep for pronounced dwell at 0% and 100%
        return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)

    def _update_preview(self) -> None:
        """Render animated preview bars from current form settings."""
        from rich.text import Text

        from .progressbars import BarConfig, bar_color_at, resolve_text_color_hex
        from .client_repl_render import _vital_bar

        # Triangular wave 0→1→0 with ease-in-out
        t = self._preview_phase * 2.0
        if t > 1.0:
            t = 2.0 - t
        fraction = self._ease_in_out(t)

        try:
            w = self.query_one("#pb-preview-bar", Static).size.width
            bar_w = w - 2 if w > 12 else 24
        except NoMatches:
            bar_w = 24
        cur = int(fraction * 1000)
        mx = 1000
        name = self.query_one("#pb-name", Input).value.strip() or "bar"

        # Build BarConfig from form
        color_mode = self._get_color_mode()
        max_c = self._get_select_value("#pb-color-max", "success")
        min_c = self._get_select_value("#pb-color-min", "error")
        path = (
            "longest"
            if self.query_one("#pb-path-longest", RadioButton).value
            else "shortest"
        )
        cfg = BarConfig(
            name, "", "", "", color_mode=color_mode,
            color_name_max=max_c, color_name_min=min_c, color_path=path,
        )

        text_fill_val = self._get_select_value("#pb-text-min", "auto")
        text_empty_val = self._get_select_value("#pb-text-max", "auto")
        text_fill = resolve_text_color_hex(text_fill_val)
        text_empty = resolve_text_color_hex(text_empty_val)

        # 1) Toolbar-style bar via _vital_bar (SGR → ANSI → Rich Text)
        kind = name.lower()
        color = bar_color_at(fraction, cfg)
        fragments = _vital_bar(
            cur, mx, bar_w, kind, color_override=color,
            text_fill_color=text_fill, text_empty_color=text_empty,
        )
        ansi_str = "".join(sgr + text for sgr, text in fragments) + "\x1b[0m"
        try:
            bar_text = Text.from_ansi(ansi_str)
            self.query_one("#pb-preview-bar", Static).update(bar_text)
        except NoMatches:
            pass

        # 2) Static color gradient showing the full min→max spectrum
        steps = bar_w + 2
        gradient_parts: list[str] = []
        for i in range(steps):
            f = i / max(1, steps - 1)
            c = bar_color_at(f, cfg)
            gradient_parts.append(f"[on {c}] [/]")
        try:
            self.query_one("#pb-preview-gradient", Static).update(
                "".join(gradient_parts)
            )
        except NoMatches:
            pass

    def _swap_color_options(
        self, is_custom: bool,
        preserve_max: str = "", preserve_min: str = "",
        preserve_text_fill: str = "auto", preserve_text_empty: str = "auto",
    ) -> None:
        """Swap color dropdown options for theme vs custom mode."""
        options = self._color_options() if is_custom else self._theme_color_options()
        max_sel = self.query_one("#pb-color-max", Select)
        min_sel = self.query_one("#pb-color-min", Select)
        max_sel.set_options(options)
        min_sel.set_options(options)
        if preserve_max:
            vals = {v for _, v in options}
            if preserve_max in vals:
                max_sel.value = preserve_max
            else:
                max_sel.value = "success" if not is_custom else "green"
        if preserve_min:
            vals = {v for _, v in options}
            if preserve_min in vals:
                min_sel.value = preserve_min
            else:
                min_sel.value = "error" if not is_custom else "red"
        text_options = self._text_color_options(is_custom)
        text_min_sel = self.query_one("#pb-text-min", Select)
        text_max_sel = self.query_one("#pb-text-max", Select)
        text_min_sel.set_options(text_options)
        text_max_sel.set_options(text_options)
        text_vals = {v for _, v in text_options}
        text_min_sel.value = preserve_text_fill if preserve_text_fill in text_vals else "auto"
        text_max_sel.value = preserve_text_empty if preserve_text_empty in text_vals else "auto"

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Toggle custom color fields and swap color options when mode changes."""
        if event.radio_set.id == "pb-color-mode":
            is_custom = self.query_one("#pb-mode-custom", RadioButton).value
            new_mode = "custom" if is_custom else "theme"
            if new_mode == self._form_color_mode:
                return
            self._form_color_mode = new_mode
            self._swap_color_options(
                is_custom,
                preserve_max=self._get_select_value("#pb-color-max", ""),
                preserve_min=self._get_select_value("#pb-color-min", ""),
                preserve_text_fill=self._get_select_value("#pb-text-min", "auto"),
                preserve_text_empty=self._get_select_value("#pb-text-max", "auto"),
            )

    def _populate_field_selects(
        self, pkg_name: str, value_field: str = "", max_field: str = "",
    ) -> None:
        """Populate value/max field Select widgets from GMCP snapshot fields."""
        fields = self._gmcp_fields.get(pkg_name, [])
        field_opts = [(f, f) for f in fields]
        val_sel = self.query_one("#pb-value-select", Select)
        max_sel = self.query_one("#pb-max-select", Select)
        # Add current values if not already in the list
        val_opts = list(field_opts)
        max_opts = list(field_opts)
        if value_field and value_field not in fields:
            val_opts.append((value_field, value_field))
        if max_field and max_field not in fields:
            max_opts.append((max_field, max_field))
        val_sel.set_options(val_opts)
        max_sel.set_options(max_opts)
        if value_field:
            val_sel.value = value_field
        if max_field:
            max_sel.value = max_field

    def on_select_changed(self, event: Select.Changed) -> None:
        """Update swatch when a color changes, update fields when source changes."""
        sel_id = event.select.id
        if sel_id == "pb-color-max":
            val = event.value
            if isinstance(val, str):
                self._update_swatch("#pb-color-swatch-max", val)
        elif sel_id == "pb-color-min":
            val = event.value
            if isinstance(val, str):
                self._update_swatch("#pb-color-swatch-min", val)
        elif sel_id == "pb-gmcp-select":
            if self._populating_form:
                return
            val = event.value
            if isinstance(val, str) and val != self._form_source_pkg:
                self._form_source_pkg = val
                self._populate_field_selects(val)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes."""
        if event.input.id == f"{self._prefix}-search":
            self._search_query = event.value
            self._refresh_table()

    def _on_extra_button(self, suffix: str, btn: str) -> None:
        """Handle the Detect button."""
        if suffix == "detect":
            self._do_detect()

    def _do_detect(self, silent: bool = False) -> None:
        """Auto-detect progress bars from GMCP snapshot."""
        if not self._gmcp_snapshot_path:
            if not silent:
                self.notify("No GMCP snapshot available.", severity="warning")
            return
        if not os.path.exists(self._gmcp_snapshot_path):
            if not silent:
                self.notify("GMCP snapshot file not found.", severity="warning")
            return
        with open(self._gmcp_snapshot_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        packages = raw.get("packages", {}) if isinstance(raw, dict) else {}
        gmcp_data = {
            pkg: info.get("data", info) for pkg, info in packages.items() if isinstance(info, dict)
        }
        from .progressbars import detect_progressbars

        detected = detect_progressbars(gmcp_data)
        existing = {(b.gmcp_package, b.value_field, b.max_field) for b in self._bars}
        existing_names = {b.name for b in self._bars}
        added = 0
        for bar in detected:
            if not bar.gmcp_package and bar.name in existing_names:
                continue
            key = (bar.gmcp_package, bar.value_field, bar.max_field)
            if key not in existing:
                self._bars.append(_ProgressBarTuple(*bar))
                existing.add(key)
                added += 1
        if added:
            self._refresh_table()
            if not silent:
                self.notify(f"Detected {added} new bar(s).")
        elif not silent:
            self.notify("No new bars detected.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to base class."""
        super().on_button_pressed(event)

    def _save_to_file(self) -> None:
        from .progressbars import BarConfig, save_progressbars

        bars = [BarConfig(*t) for t in self._bars]
        save_progressbars(self._path, self._session_key, bars)


_NAME_COL_BASE = 17
_ID_COL_BASE = 10

# Colors for room tree decorations.
_BOOKMARK_STYLE = "#ffffff"


class ProgressBarEditScreen(_EditListScreen):
    """Thin screen wrapper for the progress bar editor."""

    def __init__(
        self, path: str, session_key: str = "", gmcp_snapshot_path: str = "",
    ) -> None:
        super().__init__()
        self._pane = ProgressBarEditPane(
            path=path, session_key=session_key,
            gmcp_snapshot_path=gmcp_snapshot_path,
        )


_ARROW_STYLE = "#5b8def"  # blue, matching slow-travel button
_HOME_STYLE = "#ffffff"
_BLOCKED_STYLE = "#e04040"  # red
_MARKED_STYLE = "#ffffff"


class _RoomTree(Tree[str]):
    """Room tree with aligned icon+arrow prefix columns."""

    ICON_NODE = "\u25c2 "  # ◂
    ICON_NODE_EXPANDED = "\u25be "  # ▾

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._bookmarked: set[str] = set()
        self._blocked: set[str] = set()
        self._home: set[str] = set()
        self._marked: set[str] = set()

    def render_label(self, node: TreeNode[str], base_style: Style, style: Style) -> "RichText":
        """Render label with fixed icon+arrow prefix columns."""
        from rich.text import Text as RichText

        room_num = node.data
        is_child = node.parent is not None and node.parent.parent is not None

        # Icon column (2 chars): priority -- home > blocked > marked > bookmark
        if room_num and room_num in self._home:
            icon = RichText("\u2302 ", style=_HOME_STYLE)
        elif room_num and room_num in self._blocked:
            icon = RichText("\u2300 ", style=_BLOCKED_STYLE)
        elif room_num and room_num in self._marked:
            icon = RichText("\u27bd ", style=_MARKED_STYLE)
        elif room_num and room_num in self._bookmarked:
            icon = RichText("\u257e ", style=_BOOKMARK_STYLE)
        else:
            icon = RichText("  ")

        # Arrow column (2 chars: arrow + space) -- only for expandable nodes
        if node._allow_expand:
            arrow_char = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
            arrow = RichText(arrow_char, style=_ARROW_STYLE)
        elif not is_child:
            arrow = RichText("  ")
        else:
            arrow = RichText("")

        node_label = node._label.copy()
        node_label.stylize(style)

        text = RichText.assemble(icon, arrow, node_label)
        return text


def _invert_ts(iso_str: str) -> str:
    """
    Return a sort key that orders ISO timestamps most-recent-first.

    Empty strings sort last (after any real timestamp).
    """
    if not iso_str:
        return "\xff"
    digits = "".join(c for c in iso_str if c.isdigit())
    return "".join(chr(ord("9") - ord(c) + ord("0")) for c in digits)


class RoomBrowserPane(Vertical):
    """Pane widget for GMCP room graph browsing."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", priority=True),
        Binding("f1", "show_help", "Help", show=True),
        Binding("enter", "fast_travel", "Travel", show=True),
        Binding(
            "asterisk", "toggle_bookmark", "Bookmark", key_display="*", show=True, priority=True
        ),
        Binding("b", "toggle_block", "Block", show=True),
        Binding("h", "toggle_home", "Home", show=True),
        Binding("m", "toggle_mark", "Mark", show=True),
        Binding("n", "sort_name", "Name sort", show=True),
        Binding("i", "sort_id", "ID sort", show=True),
        Binding("d", "sort_distance", "Dist sort", show=True),
        Binding("l", "sort_last", "Recent", show=True),
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
    #room-tree > .tree--guides { color: #5b8def; }
    #room-tree > .tree--guides-hover { color: #5b8def; }
    #room-tree > .tree--guides-selected { color: #5b8def; }
    #room-heading { height: 1; text-style: bold; }
    #room-status { height: 1; margin-top: 0; }
    #room-count { width: auto; }
    #room-exits { height: 1; width: 100%; }
    #room-distance { width: auto; text-align: right; }
    #room-marker-bar { height: auto; }
    #room-marker-bar Button { width: 14; min-width: 0; margin-right: 1; }
    #room-total { height: 1; margin-top: 0; }
    Footer FooterLabel { margin: 0; }
    """

    def __init__(
        self,
        rooms_path: str,
        session_key: str = "",
        current_room_file: str = "",
        fasttravel_file: str = "",
    ) -> None:
        """Initialize room browser."""
        super().__init__()
        self._rooms_path = rooms_path
        self._session_key = session_key
        self._current_room_file = current_room_file
        self._fasttravel_file = fasttravel_file
        self._all_rooms: list[tuple[str, str, str, int, bool, str, bool, bool, bool]] = []
        self._current_area: str = ""
        self._graph: "RoomStore | None" = None
        self._mounted = False
        self._sort_mode: str = "name"
        self._distances: dict[str, int] = {}
        self._last_visited: dict[str, str] = {}
        self._name_col: int = _NAME_COL_BASE
        self._id_width: int = _ID_COL_BASE + 20
        self._muted_style: str = "dim"

    def _heading_text(self) -> str:
        """Return the column heading string for the tree."""
        info_label = "[Last]" if self._sort_mode == "last_visited" else "[Dist]"
        nc = self._name_col
        return f"      {'Name'.ljust(nc)} {'(N)'.rjust(6)} {info_label.rjust(8)}  #ID"

    def compose(self) -> ComposeResult:
        """Build the room browser layout."""
        with Vertical(id="room-panel"):
            with Horizontal(id="room-body"):
                with Vertical(id="room-button-col"):
                    travel_btn = Button("Travel", variant="success", id="room-travel")
                    travel_btn.tooltip = "Travel to the selected room"
                    yield travel_btn
                    yield Button("Help", variant="success", id="room-help")
                    yield Button("Close", id="room-close")
                    with Vertical(id="room-area-frame"):
                        yield Static("Area:")
                        yield Select[str](
                            [], id="room-area-select", allow_blank=True, prompt="All Areas"
                        )
                        yield Static("", id="room-total")
                with Vertical(id="room-right"):
                    yield Input(placeholder="Search rooms\u2026", id="room-search")
                    yield Static(self._heading_text(), id="room-heading")
                    yield _RoomTree("Rooms", id="room-tree")
                    with Horizontal(id="room-status"):
                        yield Static("", id="room-count")
                        yield Static("", id="room-distance")
                    yield Static("", id="room-exits")
                    with Horizontal(id="room-marker-bar"):
                        yield Button("Bookmark \u257e", variant="warning", id="room-bookmark")
                        yield Button("Block \u2300", variant="error", id="room-block")
                        yield Button("Home \u2302", variant="primary", id="room-home")
                        yield Button("Mark \u27bd", variant="default", id="room-mark")

    def _request_close(self, result: "bool | None" = None) -> None:
        """Dismiss the parent screen or exit the app."""
        try:
            self.screen.dismiss(result)
        except ScreenStackError:
            self.app.exit()

    def on_mount(self) -> None:
        """Load rooms from file and populate tree."""
        css_vars = self.app.get_css_variables()
        fg_muted = css_vars.get("foreground-muted", "")
        if fg_muted:
            self._muted_style = fg_muted[:7] if len(fg_muted) >= 7 else fg_muted
        term_w = self.app.size.width
        extra = max(0, term_w - 80)
        self._name_col = _NAME_COL_BASE + extra
        self._id_width = _ID_COL_BASE + 5
        tree = self.query_one("#room-tree", Tree)
        tree.show_root = False
        tree.guide_depth = 3
        self._load_rooms()
        self._compute_distances()
        self._populate_area_dropdown()
        self._sort_rooms()
        self._refresh_tree()
        self._mounted = True
        self.call_after_refresh(self._select_current_room)

    def _select_room_node(self, room_num: str) -> bool:
        """
        Select the tree node matching *room_num*.

        Expands parent groups as needed and forces a tree rebuild
        so that line numbers are valid before scrolling.

        Return True on success.
        """
        tree = self.query_one("#room-tree", Tree)
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
        # Force tree line rebuild so node._line values are current.
        _ = tree._tree_lines  # noqa: SLF001
        tree.select_node(target)
        tree.focus()
        return True

    def _select_current_room(self) -> None:
        """Move cursor to the current room node, if known."""
        if not self._current_room_file:
            return
        from telix.rooms import read_current_room

        current = read_current_room(self._current_room_file)
        if not current:
            return
        self._select_room_node(current)

    def _load_rooms(self) -> None:
        """Load room data from SQLite database."""
        from telix.rooms import RoomStore, read_current_room

        graph = RoomStore(self._rooms_path, read_only=True)
        self._graph = graph
        self._all_rooms = graph.room_summaries()
        self._last_visited = {num: lv for num, _, _, _, _, lv, _, _, _ in self._all_rooms if lv}
        if self._current_room_file:
            current = read_current_room(self._current_room_file)
            if current:
                self._current_area = graph.room_area(current)

    def _populate_area_dropdown(self) -> None:
        """Populate the area dropdown from loaded rooms."""
        areas: set[str] = set()
        for _, _, area, _, _, _, _, _, _ in self._all_rooms:
            if area:
                areas.add(area)
        sorted_areas = sorted(areas, key=str.lower)
        options = [(a, a) for a in sorted_areas]
        select = self.query_one("#room-area-select", Select)
        select.set_options(options)
        if self._current_area and self._current_area in areas:
            select.value = self._current_area

    def _compute_distances(self) -> None:
        """Compute BFS distances from the current room."""
        self._distances = {}
        if not self._current_room_file or self._graph is None:
            return
        from telix.rooms import read_current_room

        current = read_current_room(self._current_room_file)
        if current:
            self._distances = self._graph.bfs_distances(current)

    @staticmethod
    def _priority(
        r: tuple[str, str, str, int, bool, str, bool, bool, bool],
    ) -> tuple[bool, bool, bool, bool]:
        """Return sort priority: home, blocked, bookmarked, marked first."""
        # r[7]=home, r[6]=blocked, r[4]=bookmarked, r[8]=marked
        return (not r[7], not r[6], not r[4], not r[8])

    def _sort_rooms(self) -> None:
        """Sort ``_all_rooms`` according to ``_sort_mode``."""
        if self._sort_mode == "distance":
            self._all_rooms.sort(
                key=lambda r: (
                    *self._priority(r),
                    self._distances.get(r[0], float("inf")),
                    r[2].lower(),
                    r[1].lower(),
                )
            )
        elif self._sort_mode == "last_visited":
            self._all_rooms.sort(key=lambda r: (*self._priority(r), _invert_ts(r[5]), r[1].lower()))
        elif self._sort_mode == "id":
            self._all_rooms.sort(key=lambda r: (*self._priority(r), r[0].lower()))
        else:
            self._all_rooms.sort(key=lambda r: (*self._priority(r), r[2].lower(), r[1].lower()))

    def _short_id(self, num: str) -> str:
        """Truncate room ID to the configured width with ellipsis."""
        width = self._id_width
        if len(num) <= width:
            return num
        return num[: width - 1] + "\u2026"

    @staticmethod
    def _fit_name(name: str, width: int) -> str:
        """Left-justify *name* to *width*, adding ``\u2026`` if truncated."""
        if len(name) <= width:
            return name.ljust(width)
        return name[: width - 1] + "\u2026"

    def _room_label(self, num: str, name: str = "") -> "RichText":
        """Format a child leaf label with muted name, aligned dist/time + id."""
        from rich.text import Text as RichText

        if self._sort_mode == "last_visited":
            lv = self._last_visited.get(num, "")
            info_part = _relative_time(lv).rjust(8) if lv else "".rjust(8)
        else:
            dist = self._distances.get(num)
            info_part = f"[{dist}]".rjust(5) if dist is not None else "     "
        id_part = f" #{self._short_id(num)}"
        child_col = self._name_col - 1
        name_part = self._fit_name(name, child_col)
        label = RichText(f"{name_part} {''.rjust(6)} {info_part}{id_part}")
        if name:
            label.stylize(self._muted_style, 0, len(name_part))
        return label

    def _refresh_tree(self, query: str = "") -> None:
        """Rebuild tree nodes, grouping rooms with the same name."""
        from rich.text import Text as RichText

        from telix.rooms import strip_exit_dirs

        tree = self.query_one("#room-tree", Tree)
        tree.clear()
        q = query.lower()
        select = self.query_one("#room-area-select", Select)
        area_filter = select.value if isinstance(select.value, str) else None

        groups: dict[str, list[tuple[str, str, int, bool, str]]] = {}
        group_order: list[str] = []
        for num, name, area, exits, bookmarked, lv, bl, hm, mk in self._all_rooms:
            if area_filter and area != area_filter:
                continue
            display_name = strip_exit_dirs(name)
            if q and (q not in display_name.lower()
                      and q not in area.lower() and q not in num.lower()):
                continue
            if display_name not in groups:
                groups[display_name] = []
                group_order.append(display_name)
            groups[display_name].append((num, area, exits, bookmarked, lv))

        # Populate icon sets for the _RoomTree prefix renderer.
        if isinstance(tree, _RoomTree):
            tree._bookmarked = {num for num, _, _, _, bm, _, _, _, _ in self._all_rooms if bm}
            tree._blocked = {num for num, _, _, _, _, _, bl, _, _ in self._all_rooms if bl}
            tree._home = {num for num, _, _, _, _, _, _, hm, _ in self._all_rooms if hm}
            tree._marked = {num for num, _, _, _, _, _, _, _, mk in self._all_rooms if mk}

        n_shown = 0
        with self.app.batch_update():
            for name in group_order:
                members = groups[name]
                n_shown += len(members)
                show_time = self._sort_mode == "last_visited"
                if len(members) == 1:
                    num, _area, _exits, bookmarked, lv = members[0]
                    name_part = self._fit_name(name, self._name_col)
                    count_part = "(1)".rjust(6)
                    if show_time:
                        info_part = _relative_time(lv).rjust(8) if lv else "".rjust(8)
                    else:
                        dist = self._distances.get(num)
                        info_part = f"[{dist}]".rjust(5) if dist is not None else "     "
                    id_part = f" #{self._short_id(num)}"
                    label = f"{name_part} {count_part} {info_part}{id_part}"
                    tree.root.add_leaf(RichText(label), data=num)
                else:
                    name_part = self._fit_name(name, self._name_col)
                    count_part = f"({len(members)})".rjust(6)
                    if show_time:
                        newest = max((m[4] for m in members if m[4]), default="")
                        info_part = _relative_time(newest).rjust(8) if newest else "".rjust(8)
                    else:
                        nearest = min(
                            (self._distances.get(m[0], float("inf")) for m in members),
                            default=float("inf"),
                        )
                        info_part = (
                            f"[{int(nearest)}]".rjust(5) if nearest != float("inf") else "     "
                        )
                    label = f"{name_part} {count_part} {info_part}"
                    parent = tree.root.add(RichText(label), data=None)
                    if show_time:
                        members.sort(key=lambda m: m[4] or "", reverse=True)
                    else:
                        members.sort(key=lambda m: self._distances.get(m[0], float("inf")))
                    for num, _area, _exits, bookmarked, _lv in members:
                        parent.add_leaf(self._room_label(num, name), data=num)

        count_label = self.query_one("#room-count", Static)
        n_total = len(self._all_rooms)
        count_label.update(f"{n_shown:,} Rooms")
        total_label = self.query_one("#room-total", Static)
        total_label.update(f"{n_total:,} Rooms Total")

    def _get_selected_room_num(self) -> "str | None":
        """Return the room number of the currently highlighted tree node."""
        tree = self.query_one("#room-tree", Tree)
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

    def on_select_changed(self, event: Select.Changed) -> None:
        """Re-filter tree when area dropdown changes."""
        if event.select.id == "room-area-select" and self._mounted:
            search_val = self.query_one("#room-search", Input).value
            self._refresh_tree(search_val)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter tree when search input changes."""
        if event.input.id == "room-search":
            self._refresh_tree(event.value)

    def _exits_text(self, room_num: str) -> str:
        """Build an exits summary like ``Exits: north[Town Square], east[Shop]``."""
        if self._graph is None:
            return ""
        adj = self._graph._adj.get(room_num, {})
        if not adj:
            return ""
        parts: list[str] = []
        for direction, dst_num in adj.items():
            room = self._graph.get_room(dst_num)
            name = room.name if room else dst_num[:8]
            parts.append(f"{direction}[{name}]")
        return "Exits: " + ", ".join(parts)

    def _set_travel_buttons_disabled(self, disabled: bool) -> None:
        """Enable or disable the Travel button."""
        try:
            self.query_one("#room-travel", Button).disabled = disabled
        except NoMatches:
            pass

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[str]) -> None:
        """Update distance and exits labels when tree cursor moves."""
        dist_label = self.query_one("#room-distance", Static)
        exits_label = self.query_one("#room-exits", Static)
        node = event.node
        room_num = node.data if node.data is not None else None
        if room_num is None and node.children:
            first = node.children[0]
            if first.data is not None:
                room_num = first.data
        if room_num is None:
            dist_label.update("")
            exits_label.update("")
            self._set_travel_buttons_disabled(True)
            return
        exits_label.update(self._exits_text(room_num))
        if not self._current_room_file or self._graph is None:
            dist_label.update("")
            self._set_travel_buttons_disabled(True)
            return
        from telix.rooms import read_current_room

        current = read_current_room(self._current_room_file)
        if not current:
            dist_label.update("")
            self._set_travel_buttons_disabled(True)
            return
        if current == room_num:
            dist_label.update("Distance: 0 turns")
            self._set_travel_buttons_disabled(False)
            return
        path = self._graph.find_path(current, room_num)
        if path is None:
            dist_label.update("Distance: \u2014")
            self._set_travel_buttons_disabled(True)
        else:
            n = len(path)
            dist_label.update(f"Distance: {n} turn{'s' if n != 1 else ''}")
            self._set_travel_buttons_disabled(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        handlers: dict[str, Any] = {
            "room-close": lambda: self._request_close(None),
            "room-travel": self._do_travel,
            "room-bookmark": self._do_toggle_bookmark,
            "room-block": self._do_toggle_block,
            "room-home": self._do_toggle_home,
            "room-mark": self._do_toggle_mark,
            "room-help": lambda: self.app.push_screen(
                _CommandHelpScreen(topic="room")
            ),
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    def action_show_help(self) -> None:
        """Open the room browser help screen."""
        self.app.push_screen(_CommandHelpScreen(topic="room"))

    def on_key(self, event: events.Key) -> None:
        """Arrow keys navigate between search, buttons, and the room tree."""
        if event.key not in ("up", "down", "left", "right"):
            return
        focused = self.screen.focused
        search = self.query_one("#room-search", Input)
        tree = self.query_one("#room-tree", Tree)
        buttons = list(self.query("#room-button-col Button"))
        if focused is search:
            if event.key == "down":
                tree.focus()
                event.stop()
            elif event.key == "left" and buttons:
                buttons[0].focus()
                event.prevent_default()
            return
        area_select = self.query_one("#room-area-select", Select)
        if isinstance(focused, Button) and focused in buttons:
            idx = buttons.index(focused)
            if event.key == "up" and idx > 0:
                buttons[idx - 1].focus()
            elif event.key == "down":
                (buttons[idx + 1] if idx < len(buttons) - 1
                 else area_select).focus()
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
        self._request_close(None)

    def action_fast_travel(self) -> None:
        """Initiate travel to the selected room."""
        self._do_travel()

    def action_sort_name(self) -> None:
        """Sort rooms by name."""
        self._sort_mode = "name"
        self._apply_sort()

    def action_sort_id(self) -> None:
        """Sort rooms by ID."""
        self._sort_mode = "id"
        self._apply_sort()

    def action_sort_distance(self) -> None:
        """Sort rooms by distance from current room."""
        self._sort_mode = "distance"
        self._compute_distances()
        self._apply_sort()

    def action_sort_last(self) -> None:
        """Sort rooms by last visited time, most recent first."""
        self._sort_mode = "last_visited"
        self._apply_sort()

    def _apply_sort(self, select_num: str | None = None) -> None:
        """Re-sort rooms and refresh the tree, preserving selection."""
        if select_num is None:
            select_num = self._get_selected_room_num()
        self._sort_rooms()
        try:
            heading = self.query_one("#room-heading", Static)
            heading.update(self._heading_text())
        except NoMatches:
            pass
        search_val = self.query_one("#room-search", Input).value
        self._refresh_tree(search_val)
        if select_num:
            self._select_room_node(select_num)

    def action_toggle_bookmark(self) -> None:
        """Toggle bookmark on the selected room."""
        self._do_toggle_bookmark()

    def action_toggle_block(self) -> None:
        """Toggle block on the selected room."""
        self._do_toggle_block()

    def action_toggle_home(self) -> None:
        """Toggle home on the selected room."""
        self._do_toggle_home()

    def action_toggle_mark(self) -> None:
        """Toggle mark on the selected room."""
        self._do_toggle_mark()

    def _do_toggle_marker(self, marker: str) -> None:
        """Toggle an exclusive marker on the currently selected room."""
        num = self._get_selected_room_num()
        if num is None:
            return

        from telix.rooms import RoomStore

        store = RoomStore(self._rooms_path)
        store.set_marker(num, marker)
        self._all_rooms = store.room_summaries()
        store.close()
        self._apply_sort(select_num=num)

    def _do_toggle_bookmark(self) -> None:
        """Toggle bookmark on the currently selected room."""
        self._do_toggle_marker("bookmarked")

    def _do_toggle_block(self) -> None:
        """Toggle blocked on the currently selected room."""
        self._do_toggle_marker("blocked")

    def _do_toggle_home(self) -> None:
        """Toggle home on the currently selected room."""
        self._do_toggle_marker("home")

    def _do_toggle_mark(self) -> None:
        """Toggle mark on the currently selected room."""
        self._do_toggle_marker("marked")

    def _do_travel(self) -> None:
        """Calculate path and write fast travel file."""
        dst_num = self._get_selected_room_num()
        if dst_num is None:
            return

        from telix.rooms import RoomStore, write_fasttravel, read_current_room

        current = read_current_room(self._current_room_file)
        if not current:
            count = self.query_one("#room-count", Static)
            count.update("No current room \u2014 move first")
            return

        if current == dst_num:
            count = self.query_one("#room-count", Static)
            count.update("Already in this room")
            return

        graph = RoomStore(self._rooms_path, read_only=True)
        try:
            path = graph.find_path_with_rooms(current, dst_num)
        finally:
            graph.close()
        if path is None:
            dst_name = ""
            for rnum, name, *_ in self._all_rooms:
                if rnum == dst_num:
                    dst_name = name
                    break
            count = self.query_one("#room-count", Static)
            count.update(f"No path found to {dst_name or dst_num}")
            return

        write_fasttravel(self._fasttravel_file, path)
        self._request_close(True)


class RoomBrowserScreen(Screen["bool | None"]):
    """Thin screen wrapper for the room browser."""

    def __init__(
        self,
        rooms_path: str,
        session_key: str = "",
        current_room_file: str = "",
        fasttravel_file: str = "",
    ) -> None:
        super().__init__()
        self._pane = RoomBrowserPane(
            rooms_path=rooms_path, session_key=session_key,
            current_room_file=current_room_file,
            fasttravel_file=fasttravel_file,
        )

    def compose(self) -> ComposeResult:
        yield self._pane
        yield Footer()


class _RoomPickerPane(RoomBrowserPane):
    """Pane variant for room picking with Select/Cancel buttons."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", priority=True),
        Binding("enter", "select_room", "Select", show=True),
        Binding("n", "sort_name", "Name sort", show=True),
        Binding("i", "sort_id", "ID sort", show=True),
        Binding("d", "sort_distance", "Dist sort", show=True),
        Binding("l", "sort_last", "Recent", show=True),
    ]

    def compose(self) -> ComposeResult:
        """Build the room picker layout with Select/Cancel buttons only."""
        with Vertical(id="room-panel"):
            with Horizontal(id="room-body"):
                with Vertical(id="room-button-col"):
                    yield Button("Select", variant="success", id="room-select")
                    yield Button("Cancel", id="room-close")
                    with Vertical(id="room-area-frame"):
                        yield Static("Area:")
                        yield Select[str](
                            [], id="room-area-select", allow_blank=True, prompt="All Areas"
                        )
                with Vertical(id="room-right"):
                    yield Input(placeholder="Search rooms\u2026", id="room-search")
                    yield _RoomTree("Rooms", id="room-tree")
                    with Horizontal(id="room-status"):
                        yield Static("", id="room-count")
                        yield Static("", id="room-distance")
                    yield Static("", id="room-exits")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Select/Cancel button presses."""
        if event.button.id == "room-close":
            self._request_close(None)
        elif event.button.id == "room-select":
            self._do_select()

    def action_select_room(self) -> None:
        """Select the highlighted room."""
        self._do_select()

    def _do_select(self) -> None:
        """Dismiss with the selected room ID string."""
        num = self._get_selected_room_num()
        if num is None:
            return
        self._request_close(num)


class RoomPickerScreen(Screen["str | None"]):
    """Thin screen wrapper for the room picker."""

    def __init__(
        self,
        rooms_path: str,
        session_key: str = "",
        current_room_file: str = "",
    ) -> None:
        super().__init__()
        self._pane = _RoomPickerPane(
            rooms_path=rooms_path, session_key=session_key,
            current_room_file=current_room_file,
        )

    def compose(self) -> ComposeResult:
        yield self._pane
        yield Footer()


class _EditorApp(App[None]):
    """Minimal Textual app for standalone macro/autoreply editing."""

    def __init__(self, screen: Screen["bool | None"], session_key: str = "") -> None:
        """Initialize with the editor screen to push."""
        super().__init__()
        self._editor_screen = screen
        self._session_key = session_key

    def _print_error_renderables(self) -> None:
        """Print error tracebacks to stdout after alt screen exit.

        Textual's default writes to ``error_console`` (stderr).  In the
        telix subprocess stderr may not translate ``\\n`` to ``\\r\\n``
        correctly, producing staircase output.  Writing to a fresh
        stdout-based Rich console avoids the issue.
        """
        if not self._exit_renderables:
            return
        from rich.console import Console

        console = Console(file=sys.stdout, markup=False, highlight=False)
        for renderable in self._exit_renderables:
            console.print(renderable)
        self._exit_renderables.clear()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Paste X11 primary selection on middle-click."""
        if event.button != 2:
            return
        event.stop()
        text = _read_primary_selection()
        if not text:
            return
        focused = self.focused
        if focused is not None and hasattr(focused, "insert_text_at_cursor"):
            focused.insert_text_at_cursor(text)

    def _set_pointer_shape(self, shape: str) -> None:
        """
        Disable pointer shape changes to prevent WriterThread deadlock.

        Textual writes escape sequences to set cursor shape on mouse move.
        When the PTY output buffer is full, ``WriterThread.write()`` blocks,
        and the bounded queue causes ``queue.put()`` to block the main
        asyncio thread, freezing the entire app.
        """

    def on_mount(self) -> None:
        """Push the editor screen."""
        _log = logging.getLogger(__name__)
        driver = self._driver
        _log.debug(
            "EditorApp mounted: driver._mouse=%s input_tty=%s "
            "driver._file=%r driver.fileno=%s "
            "driver._mouse_pixels=%s driver._in_band_window_resize=%s",
            getattr(driver, "_mouse", "?"),
            getattr(driver, "input_tty", "?"),
            getattr(driver, "_file", "?"),
            getattr(driver, "fileno", "?"),
            getattr(driver, "_mouse_pixels", "?"),
            getattr(driver, "_in_band_window_resize", "?"),
        )
        from .rooms import load_prefs

        saved_theme = ""
        if self._session_key:
            prefs = load_prefs(self._session_key)
            saved_theme = prefs.get("tui_theme", "")
        if not saved_theme:
            saved_theme = load_prefs(DEFAULTS_KEY).get("tui_theme", "")
        if isinstance(saved_theme, str) and saved_theme:
            self.theme = saved_theme
        self.push_screen(self._editor_screen, callback=lambda _: self.exit())

    def watch_theme(self, old: str, new: str) -> None:
        """Persist theme choice to per-session and global preferences."""
        if not new:
            return
        from .rooms import load_prefs, save_prefs

        save_key = self._session_key or DEFAULTS_KEY
        prefs = load_prefs(save_key)
        prefs["tui_theme"] = new
        save_prefs(save_key, prefs)


def _patch_writer_thread_queue() -> None:
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
        import textual.drivers._writer_thread as _wt

        _wt.MAX_QUEUED_WRITES = 0
    except (ImportError, AttributeError):
        pass


def _restore_blocking_fds(logfile: str = "") -> None:
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
    import os as _os
    import sys as _sys
    import logging as _logging

    if logfile:
        _logging.basicConfig(
            filename=logfile,
            level=_logging.DEBUG,
            format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        )

    _log = _logging.getLogger(__name__)
    _log.debug(
        "child pre-fix: fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s __stdin___isatty=%s "
        "stderr_isatty=%s __stderr___isatty=%s",
        _os.get_blocking(0),
        _os.get_blocking(1),
        _os.get_blocking(2),
        _sys.stdin.isatty(),
        _sys.__stdin__.isatty(),
        _sys.stderr.isatty(),
        _sys.__stderr__.isatty(),
    )
    for fd in (0, 1, 2):
        try:
            _os.set_blocking(fd, True)
        except OSError:
            pass
    _log.debug(
        "child post-fix: fd0_blocking=%s fd1=%s fd2=%s",
        _os.get_blocking(0),
        _os.get_blocking(1),
        _os.get_blocking(2),
    )


def _log_child_diagnostics() -> None:
    """Log environment and terminal diagnostics in the child subprocess."""
    _log = logging.getLogger(__name__)
    _env_keys = ("TERM", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE")
    _env = {k: os.environ.get(k, "") for k in _env_keys}
    try:
        _tsize = os.get_terminal_size()
        _tsize_str = f"{_tsize.columns}x{_tsize.lines}"
    except OSError:
        _tsize_str = "?"
    _log.debug(
        "child env: %s terminal_size=%s fd0_blocking=%s fd2_blocking=%s",
        _env,
        _tsize_str,
        os.get_blocking(0),
        os.get_blocking(2),
    )
    os.environ["TEXTUAL_DEBUG"] = "1"


def _run_editor_app(app: _EditorApp) -> None:
    """
    Run a Textual editor app, displaying errors in the terminal on crash.

    Textual handles some exceptions internally (e.g. compose errors) --
    it renders a traceback in the alt screen, then exits with return
    code 1.  Once the alt screen is torn down the traceback is lost.

    This wrapper catches both raised exceptions and Textual-handled
    errors, resets the terminal to normal mode, displays the traceback,
    and prompts the user before exiting.
    """
    try:
        app.run()
    except BaseException:
        import traceback as tb_mod

        sys.stdout.write(_TERMINAL_CLEANUP)
        sys.stdout.flush()
        tb_mod.print_exc()
        _pause_before_exit()
        raise
    if app.return_code and app.return_code != 0:
        sys.stdout.write(_TERMINAL_CLEANUP)
        sys.stdout.flush()
        app._print_error_renderables()
        _pause_before_exit()
        sys.exit(app.return_code)


def _pause_before_exit() -> None:
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


def _launch_editor(
    screen: Screen[Any], session_key: str = "", logfile: str = ""
) -> None:
    """Common bootstrap for standalone editor entry points."""
    _restore_blocking_fds(logfile)
    _log_child_diagnostics()
    _patch_writer_thread_queue()
    app = _EditorApp(screen, session_key=session_key)
    _run_editor_app(app)


def edit_macros_main(
    path: str,
    session_key: str = "",
    rooms_file: str = "",
    current_room_file: str = "",
    logfile: str = "",
) -> None:
    """Launch standalone macro editor TUI."""
    _launch_editor(
        MacroEditScreen(
            path=path, session_key=session_key,
            rooms_file=rooms_file, current_room_file=current_room_file,
        ),
        session_key=session_key, logfile=logfile,
    )


def edit_autoreplies_main(
    path: str, session_key: str = "", select_pattern: str = "", logfile: str = ""
) -> None:
    """Launch standalone autoreply editor TUI."""
    _launch_editor(
        AutoreplyEditScreen(path=path, session_key=session_key, select_pattern=select_pattern),
        session_key=session_key, logfile=logfile,
    )


def edit_highlights_main(path: str, session_key: str = "", logfile: str = "") -> None:
    """Launch standalone highlight editor TUI."""
    _launch_editor(
        HighlightEditScreen(path=path, session_key=session_key),
        session_key=session_key, logfile=logfile,
    )


def edit_progressbars_main(
    path: str, session_key: str = "", gmcp_snapshot_path: str = "", logfile: str = ""
) -> None:
    """Launch standalone progress bar editor TUI."""
    _launch_editor(
        ProgressBarEditScreen(
            path=path, session_key=session_key, gmcp_snapshot_path=gmcp_snapshot_path,
        ),
        session_key=session_key, logfile=logfile,
    )


def edit_rooms_main(
    rooms_path: str,
    session_key: str = "",
    current_room_file: str = "",
    fasttravel_file: str = "",
    logfile: str = "",
) -> None:
    """Launch standalone room browser TUI."""
    _launch_editor(
        RoomBrowserScreen(
            rooms_path=rooms_path, session_key=session_key,
            current_room_file=current_room_file, fasttravel_file=fasttravel_file,
        ),
        session_key=session_key, logfile=logfile,
    )


def show_help_main(topic: str = "keybindings", logfile: str = "") -> None:
    """Launch standalone help viewer TUI."""
    _launch_editor(_CommandHelpScreen(topic=topic), logfile=logfile)


class _CapsPane(Vertical):
    """Pane widget for captures and chats viewing."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("f10", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
        Binding("f1", "toggle_keys", "Keys", show=True),
        Binding("tab", "next_channel", "Next Channel", show=True),
        Binding("shift+tab", "prev_channel", "Prev Channel", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    _CapsPane {
        width: 100%; height: 100%;
    }
    #chat-header {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    #chat-channel-bar {
        height: 1;
    }
    #chat-log {
        height: 1fr;
        background: $surface;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        chat_file: str,
        session_key: str = "",
        initial_channel: str = "",
        capture_file: str = "",
    ) -> None:
        """
        Initialize with path to chat history file.

        :param chat_file: Path to the chat JSON file.
        :param session_key: Session identifier.
        :param initial_channel: Channel to select on open (most recent activity).
        :param capture_file: Path to a JSON file with capture data.
        """
        super().__init__()
        self._chat_file = chat_file
        self._session_key = session_key
        self._initial_channel = initial_channel
        self._capture_file = capture_file
        self._messages: list[dict[str, Any]] = []
        self._channels: list[str] = []
        self._filter_idx: int = 0
        self._captures: dict[str, int] = {}
        self._capture_log: dict[str, list[dict[str, Any]]] = {}

    def compose(self) -> ComposeResult:
        """Build the chat viewer layout."""
        from textual.widgets import RichLog

        with Vertical(id="chat-header"):
            yield Static("", id="chat-channel-bar")
        yield RichLog(highlight=False, markup=False, wrap=True, id="chat-log")

    def on_mount(self) -> None:
        """Load chat file, select initial channel, and populate the log."""
        self._load_messages()
        if self._initial_channel and self._initial_channel in self._channels:
            self._filter_idx = self._channels.index(self._initial_channel) + 1
        self._update_channel_bar()
        self._populate_log()

    def _load_messages(self) -> None:
        """Read messages from chat JSON and capture data files."""
        if self._chat_file and os.path.exists(self._chat_file):
            with open(self._chat_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                self._messages = data
        if self._capture_file and os.path.exists(self._capture_file):
            with open(self._capture_file, "r", encoding="utf-8") as fh:
                cap_data = json.load(fh)
            if isinstance(cap_data, dict):
                self._captures = cap_data.get("captures", {})
                self._capture_log = cap_data.get("capture_log", {})
        channels: set[str] = set()
        for msg in self._messages:
            ch = msg.get("channel", "")
            if ch:
                channels.add(ch)
        for ch in self._capture_log:
            channels.add(ch)
        self._channels = sorted(channels)

    def _channel_labels(self) -> list[str]:
        """Return ``["all", ...channels]`` list used for cycling."""
        return ["all"] + self._channels

    def _active_filter(self) -> str:
        """Return the current channel filter string, or ``""`` for all."""
        labels = self._channel_labels()
        if self._filter_idx == 0 or self._filter_idx >= len(labels):
            return ""
        return labels[self._filter_idx]

    def _update_channel_bar(self) -> None:
        """Rebuild the horizontal channel bar with the active channel highlighted."""
        from rich.text import Text as RichText

        labels = self._channel_labels()
        channel_bar = RichText("Channel (TAB changes): ")
        for i, name in enumerate(labels):
            if i > 0:
                channel_bar.append("  ")
            if i == self._filter_idx:
                channel_bar.append(f" {name} ", style="reverse bold")
            else:
                channel_bar.append(name, style="dim")
        self.query_one("#chat-channel-bar", Static).update(channel_bar)

    def _populate_log(self, channel_filter: str = "") -> None:
        """Fill the RichLog with chat and capture messages, optionally filtered."""
        from rich.text import Text as RichText
        from textual.widgets import RichLog

        if not channel_filter:
            channel_filter = self._active_filter()
        log_widget: RichLog = self.query_one("#chat-log", RichLog)
        log_widget.clear()

        # Show captures key/value table when viewing the "captures" channel
        if channel_filter == "captures" and self._captures:
            header = RichText("Current Captures:", style="bold underline")
            log_widget.write(header)
            for k, v in sorted(self._captures.items()):
                log_widget.write(RichText(f"  {k}: {v}"))
            log_widget.write(RichText(""))

        # Merge GMCP chat messages and capture log entries by timestamp
        all_entries: list[tuple[str, str, dict[str, Any]]] = []
        for msg in self._messages:
            ch = msg.get("channel", "")
            if channel_filter and ch != channel_filter:
                continue
            all_entries.append((msg.get("ts", ""), "chat", msg))
        for ch, entries in self._capture_log.items():
            if channel_filter and ch != channel_filter:
                continue
            for entry in entries:
                all_entries.append((entry.get("ts", ""), "capture", {**entry, "channel": ch}))
        all_entries.sort(key=lambda e: e[0])

        for ts_val, source, msg in all_entries:
            ch = msg.get("channel", "")
            line = RichText()
            if ts_val:
                short_ts = ts_val[11:16] if len(ts_val) >= 16 else ts_val
                line.append(f"{short_ts} ", style="dim")
            if not channel_filter:
                channel_ansi = msg.get("channel_ansi", "")
                if channel_ansi:
                    line.append_text(RichText.from_ansi(channel_ansi.rstrip()))
                    line.append(" ")
                else:
                    line.append(f"[{ch}] ", style="bold cyan")
            if source == "chat":
                talker = msg.get("talker", "")
                text_raw = msg.get("text", "").rstrip("\n")
                body = text_raw
                if talker:
                    for prefix in (f"{talker} : ", f"{talker}: ", f"{talker} "):
                        if body.startswith(prefix):
                            body = body[len(prefix) :]
                            break
                    line.append(f"{talker}: ", style="bold")
                line.append_text(RichText.from_ansi(body))
            else:
                line_text = msg.get("line", "")
                hl_style = msg.get("highlight", "")
                if hl_style:
                    line.append(line_text, style=hl_style.replace("_", " "))
                else:
                    line.append_text(RichText.from_ansi(line_text))
            log_widget.write(line)
        log_widget.scroll_end(animate=False)

    def action_close(self) -> None:
        """Dismiss the chat viewer."""
        self.app.exit()

    def action_toggle_keys(self) -> None:
        """Toggle the Textual keys help panel."""
        if self.app.help_panel:
            self.app.action_hide_help_panel()
        else:
            self.app.action_show_help_panel()

    def action_next_channel(self) -> None:
        """Cycle forward through channel filters."""
        if not self._channels:
            return
        labels = self._channel_labels()
        self._filter_idx = (self._filter_idx + 1) % len(labels)
        self._update_channel_bar()
        self._populate_log()

    def action_prev_channel(self) -> None:
        """Cycle backward through channel filters."""
        if not self._channels:
            return
        labels = self._channel_labels()
        self._filter_idx = (self._filter_idx - 1) % len(labels)
        self._update_channel_bar()
        self._populate_log()


class _CapsScreen(Screen[None]):
    """Thin screen wrapper for the captures and chats viewer."""

    def __init__(
        self,
        chat_file: str,
        session_key: str = "",
        initial_channel: str = "",
        capture_file: str = "",
    ) -> None:
        super().__init__()
        self._pane = _CapsPane(
            chat_file=chat_file, session_key=session_key,
            initial_channel=initial_channel, capture_file=capture_file,
        )

    def compose(self) -> ComposeResult:
        yield self._pane
        yield Footer()


# Keep backwards-compatible alias.
_ChatViewerScreen = _CapsScreen


def chat_viewer_main(
    chat_file: str,
    session_key: str = "",
    initial_channel: str = "",
    logfile: str = "",
    capture_file: str = "",
) -> None:
    """Launch standalone Capture Window TUI."""
    _restore_blocking_fds(logfile)
    _log_child_diagnostics()
    _patch_writer_thread_queue()
    app = _EditorApp(
        _ChatViewerScreen(
            chat_file=chat_file,
            session_key=session_key,
            initial_channel=initial_channel,
            capture_file=capture_file,
        ),
        session_key=session_key,
    )
    app.run(mouse=False)


# ---------------------------------------------------------------------------
# Tabbed editor: combines all 7 editor panes into a single TUI subprocess.
# ---------------------------------------------------------------------------

# Tab definitions: (label, tab_id, pane_factory_name)
_EDITOR_TABS: list[tuple[str, str]] = [
    ("F1 Help", "help"),
    ("F6 Highlights", "highlights"),
    ("F7 Rooms", "rooms"),
    ("F8 Macros", "macros"),
    ("F9 Autoreplies", "autoreplies"),
    ("F10 Caps & Chats", "captures"),
    ("F11 Bars", "bars"),
]


class _TabbedEditorScreen(Screen[None]):
    """Full-screen tabbed editor combining all 7 editor panes."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close_or_back", "Close", priority=True),
        Binding("f1", "switch_tab('help')", "Help", show=False),
        Binding("f6", "switch_tab('highlights')", "Highlights", show=False),
        Binding("f7", "switch_tab('rooms')", "Rooms", show=False),
        Binding("f8", "switch_tab('macros')", "Macros", show=False),
        Binding("f9", "switch_tab('autoreplies')", "Autoreplies", show=False),
        Binding("f10", "switch_tab('captures')", "Caps", show=False),
        Binding("f11", "switch_tab('bars')", "Bars", show=False),
    ]

    CSS = """
    _TabbedEditorScreen {
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

    def __init__(self, params: dict[str, Any]) -> None:
        """Initialize tabbed editor from a parameters dict.

        :param params: Dict with keys for each pane's constructor args, plus
            ``initial_tab`` and ``initial_channel``.
        """
        super().__init__()
        self._params = params
        self._initial_tab = params.get("initial_tab", "help")
        self._panes: dict[str, Any] = {}
        self._loaded: set[str] = set()
        self._dirty: set[str] = set()

    def compose(self) -> ComposeResult:
        """Build tab bar and content switcher with lazy-loaded panes."""
        with Horizontal(id="te-tab-bar"):
            for label, tab_id in _EDITOR_TABS:
                btn = Button(label, id=f"te-btn-{tab_id}")
                if tab_id == self._initial_tab:
                    btn.add_class("active-tab")
                yield btn

        with ContentSwitcher(id="te-content", initial=self._initial_tab):
            for _label, tab_id in _EDITOR_TABS:
                pane = self._create_pane(tab_id)
                self._panes[tab_id] = pane
                yield pane

        yield Footer()

    _PANE_FACTORIES: ClassVar[dict[str, tuple[type, dict[str, str]]]] = {
        "help": (_HelpPane, {"topic": ""}),
        "highlights": (HighlightEditPane, {
            "path": "highlights_file", "session_key": "session_key",
        }),
        "rooms": (RoomBrowserPane, {
            "rooms_path": "rooms_file", "session_key": "session_key",
            "current_room_file": "current_room_file",
            "fasttravel_file": "fasttravel_file",
        }),
        "macros": (MacroEditPane, {
            "path": "macros_file", "session_key": "session_key",
            "rooms_file": "rooms_file",
            "current_room_file": "current_room_file",
        }),
        "autoreplies": (AutoreplyEditPane, {
            "path": "autoreplies_file", "session_key": "session_key",
            "select_pattern": "select_pattern",
        }),
        "captures": (_CapsPane, {
            "chat_file": "chat_file", "session_key": "session_key",
            "initial_channel": "initial_channel",
            "capture_file": "capture_file",
        }),
        "bars": (ProgressBarEditPane, {
            "path": "progressbars_file", "session_key": "session_key",
            "gmcp_snapshot_path": "gmcp_snapshot_file",
        }),
    }

    def _create_pane(self, tab_id: str) -> Vertical:
        """Instantiate the pane widget for *tab_id*."""
        cls, param_map = self._PANE_FACTORIES[tab_id]
        if tab_id == "help":
            pane = cls(topic="keybindings")
        else:
            kwargs = {k: self._params.get(v, "") for k, v in param_map.items()}
            pane = cls(**kwargs)
        pane.id = tab_id
        return pane

    def on_mount(self) -> None:
        """Mark the initial tab as loaded."""
        self._loaded.add(self._initial_tab)

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch to *tab_id*, loading it lazily if needed."""
        switcher = self.query_one("#te-content", ContentSwitcher)
        if switcher.current == tab_id:
            return
        switcher.current = tab_id
        for btn in self.query("#te-tab-bar Button"):
            btn.remove_class("active-tab")
            if btn.id == f"te-btn-{tab_id}":
                btn.add_class("active-tab")
        if tab_id not in self._loaded:
            self._loaded.add(tab_id)
            pane = self._panes.get(tab_id)
            if pane and hasattr(pane, "_load_from_file"):
                pane._load_from_file()
                if hasattr(pane, "_refresh_table"):
                    pane._refresh_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle tab bar button clicks."""
        btn_id = event.button.id or ""
        if btn_id.startswith("te-btn-"):
            tab_id = btn_id[len("te-btn-"):]
            self.action_switch_tab(tab_id)

    def action_close_or_back(self) -> None:
        """Close form if visible in an editor pane, otherwise exit app."""
        current = self.query_one("#te-content", ContentSwitcher).current
        pane = self._panes.get(current or "")
        if pane and hasattr(pane, "_form_visible") and pane._form_visible:
            if hasattr(pane, "action_cancel_or_close"):
                pane.action_cancel_or_close()
            return
        self._save_all_dirty()
        self.app.exit()

    def _save_all_dirty(self) -> None:
        """Auto-save any panes that have been loaded and have a save method."""
        for tab_id in self._loaded:
            pane = self._panes.get(tab_id)
            if pane and hasattr(pane, "_save_to_file"):
                pane._save_to_file()

    def _handle_travel(self) -> None:
        """Save all editors and exit when room travel is requested."""
        self._save_all_dirty()
        self.app.exit()


def unified_editor_main() -> None:
    """Launch the tabbed editor TUI subprocess.

    Reads a single JSON blob from ``sys.argv[1]`` containing all parameters
    for every pane. Called from the REPL via ``_launch_unified_editor()``.
    """
    params = json.loads(sys.argv[1])
    logfile = params.get("logfile", "")
    _restore_blocking_fds(logfile)
    _log_child_diagnostics()
    _patch_writer_thread_queue()

    screen = _TabbedEditorScreen(params)
    session_key = params.get("session_key", "")
    app = _EditorApp(screen, session_key=session_key)
    _run_editor_app(app)


class _ConfirmDialogScreen(Screen[bool]):
    """Confirmation dialog with optional warning."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    _ConfirmDialogScreen {
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
        self,
        title: str,
        body: str,
        warning: str = "",
        result_file: str = "",
        show_dont_ask: bool = True,
    ) -> None:
        """Initialize confirm dialog with title, body, and optional warning."""
        super().__init__()
        self._title = title
        self._body = body
        self._warning = warning
        self._result_file = result_file
        self._show_dont_ask = show_dont_ask

    def compose(self) -> ComposeResult:
        """Build the confirm dialog layout."""
        with Vertical(id="confirm-dialog"):
            yield Static(self._title, id="confirm-title")
            yield Static(self._body, id="confirm-body")
            if self._warning:
                yield Static(self._warning, id="confirm-warning")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", variant="error", id="confirm-cancel")
                yield Button("OK", variant="success", id="confirm-ok")

    def on_mount(self) -> None:
        """Focus OK button on mount."""
        self.query_one("#confirm-ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle OK/Cancel button presses."""
        if event.button.id == "confirm-ok":
            self._write_result(True)
            self.dismiss(True)
        elif event.button.id == "confirm-cancel":
            self._write_result(False)
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Handle Escape key."""
        self._write_result(False)
        self.dismiss(False)

    def _write_result(self, confirmed: bool) -> None:
        """Write result to file for the parent process to read."""
        if not self._result_file:
            return
        result = json.dumps({"confirmed": confirmed})
        with open(self._result_file, "w", encoding="utf-8") as f:
            f.write(result)


def confirm_dialog_main(
    title: str, body: str, warning: str = "", result_file: str = "", logfile: str = ""
) -> None:
    """Launch standalone confirm dialog TUI."""
    _restore_blocking_fds(logfile)
    _log_child_diagnostics()
    _patch_writer_thread_queue()
    screen = _ConfirmDialogScreen(title=title, body=body, warning=warning, result_file=result_file)
    app = _EditorApp(screen)
    app.run()


class _RandomwalkDialogScreen(Screen[bool]):
    """Random walk confirmation dialog with visit-level parameter."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("f1", "show_help", "Help", show=False),
    ]

    DEFAULT_CSS = """
    _RandomwalkDialogScreen {
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
        self._result_file = result_file
        self._default_visit_level = default_visit_level
        self._default_auto_search = default_auto_search
        self._default_auto_evaluate = default_auto_evaluate
        self._default_auto_survey = default_auto_survey
        self._default_autoreplies = default_autoreplies

    def compose(self) -> ComposeResult:
        with Vertical(id="rw-dialog"):
            yield Static("Random Walk", id="rw-title")
            yield Static(
                "Random walk explores rooms by picking random exits, "
                "preferring unvisited rooms. It never returns through "
                "the entrance you came from. Autoreplies fire in each "
                "room. Stops when all reachable rooms are visited the "
                "required number of times.",
                id="rw-body",
            )
            with Vertical(id="rw-options-col"):
                with Horizontal(classes="rw-option"):
                    lbl = Label("Visit level:")
                    lbl.tooltip = (
                        "Minimum number of times each reachable room must be "
                        "visited before the walk stops."
                    )
                    yield lbl
                    yield Input(
                        value=str(self._default_visit_level),
                        id="rw-visit-level", type="integer",
                    )
            with Vertical(id="rw-switches"):
                with Horizontal(classes="rw-switch-row"):
                    with Horizontal(classes="rw-switch-cell"):
                        yield Label("Auto search:")
                        yield Switch(
                            value=self._default_auto_search,
                            id="rw-auto-search",
                        )
                    with Horizontal(classes="rw-switch-cell"):
                        yield Label("Auto consider:")
                        yield Switch(
                            value=self._default_auto_evaluate,
                            id="rw-auto-consider",
                            disabled=not self._default_autoreplies,
                        )
                with Horizontal(classes="rw-switch-row"):
                    with Horizontal(classes="rw-switch-cell"):
                        yield Label("Auto survey:")
                        yield Switch(
                            value=self._default_auto_survey,
                            id="rw-auto-survey",
                            disabled=not self._default_autoreplies,
                        )
                    with Horizontal(classes="rw-switch-cell"):
                        yield Label("Autoreplies:")
                        yield Switch(
                            value=self._default_autoreplies,
                            id="rw-autoreplies",
                        )
            yield Static("", id="rw-error")
            with Horizontal(id="rw-buttons"):
                yield Button("Help", variant="primary", id="rw-help")
                yield Button("Cancel", variant="error", id="rw-cancel")
                yield Button("OK", variant="success", id="rw-ok")

    def on_mount(self) -> None:
        """Focus the OK button on mount."""
        self.query_one("#rw-ok", Button).focus()

    def action_show_help(self) -> None:
        """Show room-mapping help screen."""
        self.app.push_screen(_CommandHelpScreen(topic="room-mapping"))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Disable consider/survey switches when autoreplies is OFF."""
        if event.switch.id == "rw-autoreplies":
            self.query_one("#rw-auto-consider", Switch).disabled = not event.value
            self.query_one("#rw-auto-survey", Switch).disabled = not event.value

    def _validate_and_dismiss(self) -> None:
        raw = self.query_one("#rw-visit-level", Input).value.strip()
        try:
            level = int(raw)
        except ValueError:
            self.query_one("#rw-error", Static).update("Visit level must be a number.")
            return
        if level < 1:
            self.query_one("#rw-error", Static).update("Visit level must be at least 1.")
            return
        auto_search = self.query_one("#rw-auto-search", Switch).value
        auto_evaluate = self.query_one("#rw-auto-consider", Switch).value
        auto_survey = self.query_one("#rw-auto-survey", Switch).value
        autoreplies = self.query_one("#rw-autoreplies", Switch).value
        self._write_result(
            True, level, auto_search, auto_evaluate, auto_survey, autoreplies,
        )
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle OK, Cancel, and Help button presses."""
        if event.button.id == "rw-help":
            self.action_show_help()
        elif event.button.id == "rw-ok":
            self._validate_and_dismiss()
        elif event.button.id == "rw-cancel":
            self._write_result(
                False,
                self._default_visit_level,
                self._default_auto_search,
                self._default_auto_evaluate,
                self._default_auto_survey,
                self._default_autoreplies,
            )
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Cancel the dialog and write default values."""
        self._write_result(
            False,
            self._default_visit_level,
            self._default_auto_search,
            self._default_auto_evaluate,
            self._default_auto_survey,
            self._default_autoreplies,
        )
        self.dismiss(False)

    def _write_result(
        self,
        confirmed: bool,
        visit_level: int,
        auto_search: bool = False,
        auto_evaluate: bool = False,
        auto_survey: bool = False,
        autoreplies: bool = True,
    ) -> None:
        if not self._result_file:
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
        with open(self._result_file, "w", encoding="utf-8") as f:
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
    _restore_blocking_fds(logfile)
    _log_child_diagnostics()
    _patch_writer_thread_queue()
    screen = _RandomwalkDialogScreen(
        result_file=result_file,
        default_visit_level=int(default_visit_level),
        default_auto_search=default_auto_search == "1",
        default_auto_evaluate=default_auto_evaluate == "1",
        default_auto_survey=default_auto_survey == "1",
        default_autoreplies=default_autoreplies == "1",
    )
    app = _EditorApp(screen)
    app.run()


class _AutodiscoverDialogScreen(Screen[bool]):
    """Autodiscover confirmation dialog with BFS/DFS strategy selection."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("f1", "show_help", "Help", show=False),
    ]

    DEFAULT_CSS = """
    _AutodiscoverDialogScreen {
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
        self._result_file = result_file
        self._default_strategy = default_strategy
        self._default_auto_search = default_auto_search
        self._default_auto_evaluate = default_auto_evaluate
        self._default_auto_survey = default_auto_survey
        self._default_autoreplies = default_autoreplies

    def compose(self) -> ComposeResult:
        with Vertical(id="ad-dialog"):
            yield Static("Autodiscover", id="ad-title")
            yield Static(
                "Autodiscover explores exits from nearby rooms "
                "that lead to unvisited places. It will travel "
                "to each frontier exit, check the room, then "
                "return before trying the next branch.",
                id="ad-body",
            )
            yield Static(
                "WARNING: This can lead to dangerous areas, "
                "death traps, or aggressive monsters! Your "
                "character may die. Use with caution.",
                id="ad-warning",
            )
            with Horizontal(id="ad-strategy-row"):
                with RadioSet(id="ad-strategy-set"):
                    yield RadioButton(
                        "BFS: explore nearest exits first",
                        id="ad-bfs",
                        value=(self._default_strategy == "bfs"),
                    )
                    yield RadioButton(
                        "DFS: explore farthest exits first",
                        id="ad-dfs",
                        value=(self._default_strategy == "dfs"),
                    )
            with Vertical(id="ad-switches"):
                with Horizontal(classes="ad-switch-row"):
                    with Horizontal(classes="ad-switch-cell"):
                        yield Label("Auto search:")
                        yield Switch(
                            value=self._default_auto_search,
                            id="ad-auto-search",
                        )
                    with Horizontal(classes="ad-switch-cell"):
                        yield Label("Auto consider:")
                        yield Switch(
                            value=self._default_auto_evaluate,
                            id="ad-auto-consider",
                            disabled=not self._default_autoreplies,
                        )
                with Horizontal(classes="ad-switch-row"):
                    with Horizontal(classes="ad-switch-cell"):
                        yield Label("Auto survey:")
                        yield Switch(
                            value=self._default_auto_survey,
                            id="ad-auto-survey",
                            disabled=not self._default_autoreplies,
                        )
                    with Horizontal(classes="ad-switch-cell"):
                        yield Label("Autoreplies:")
                        yield Switch(
                            value=self._default_autoreplies,
                            id="ad-autoreplies",
                        )
            with Horizontal(id="ad-buttons"):
                yield Button("Help", variant="primary", id="ad-help")
                yield Button("Cancel", variant="error", id="ad-cancel")
                yield Button("OK", variant="success", id="ad-ok")

    def on_mount(self) -> None:
        """Focus the OK button on mount."""
        self.query_one("#ad-ok", Button).focus()

    def action_show_help(self) -> None:
        """Show room-mapping help screen."""
        self.app.push_screen(_CommandHelpScreen(topic="room-mapping"))

    def _get_strategy(self) -> str:
        """Return the selected strategy string."""
        if self.query_one("#ad-dfs", RadioButton).value:
            return "dfs"
        return "bfs"

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Disable consider/survey switches when autoreplies is OFF."""
        if event.switch.id == "ad-autoreplies":
            self.query_one("#ad-auto-consider", Switch).disabled = not event.value
            self.query_one("#ad-auto-survey", Switch).disabled = not event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle OK and Cancel button presses."""
        if event.button.id == "ad-help":
            self.action_show_help()
        elif event.button.id == "ad-ok":
            auto_search = self.query_one("#ad-auto-search", Switch).value
            auto_evaluate = self.query_one("#ad-auto-consider", Switch).value
            auto_survey = self.query_one("#ad-auto-survey", Switch).value
            autoreplies = self.query_one("#ad-autoreplies", Switch).value
            self._write_result(
                True, self._get_strategy(),
                auto_search, auto_evaluate, auto_survey, autoreplies,
            )
            self.dismiss(True)
        elif event.button.id == "ad-cancel":
            self._write_result(
                False, self._default_strategy,
                self._default_auto_search, self._default_auto_evaluate,
                self._default_auto_survey, self._default_autoreplies,
            )
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Cancel the dialog and write default values."""
        self._write_result(
            False, self._default_strategy,
            self._default_auto_search, self._default_auto_evaluate,
            self._default_auto_survey, self._default_autoreplies,
        )
        self.dismiss(False)

    def _write_result(
        self,
        confirmed: bool,
        strategy: str,
        auto_search: bool = False,
        auto_evaluate: bool = False,
        auto_survey: bool = False,
        autoreplies: bool = True,
    ) -> None:
        """Write result JSON to disk for the parent process."""
        if not self._result_file:
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
        result = json.dumps({
            "confirmed": confirmed,
            "strategy": strategy,
            "auto_search": auto_search,
            "auto_evaluate": auto_evaluate,
            "auto_survey": auto_survey,
            "autoreplies": autoreplies,
            "command": cmd,
        })
        with open(self._result_file, "w", encoding="utf-8") as f:
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
    _restore_blocking_fds(logfile)
    _log_child_diagnostics()
    _patch_writer_thread_queue()
    screen = _AutodiscoverDialogScreen(
        result_file=result_file,
        default_strategy=default_strategy,
        default_auto_search=default_auto_search == "1",
        default_auto_evaluate=default_auto_evaluate == "1",
        default_auto_survey=default_auto_survey == "1",
        default_autoreplies=default_autoreplies == "1",
    )
    app = _EditorApp(screen)
    app.run()


class TelnetSessionApp(App[None]):
    """Textual TUI for managing telix client sessions."""

    TITLE = "telix Session Manager"

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Paste X11 primary selection on middle-click."""
        if event.button != 2:
            return
        event.stop()
        text = _read_primary_selection()
        if not text:
            return
        focused = self.focused
        if focused is not None and hasattr(focused, "insert_text_at_cursor"):
            focused.insert_text_at_cursor(text)

    def _set_pointer_shape(self, shape: str) -> None:
        """Disable pointer shape changes to prevent WriterThread deadlock."""

    def on_mount(self) -> None:
        """Push the session list screen on startup."""
        from .rooms import load_prefs

        prefs = load_prefs(DEFAULTS_KEY)
        saved_theme = prefs.get("tui_theme")
        if isinstance(saved_theme, str) and saved_theme:
            self.theme = saved_theme
        self.push_screen(SessionListScreen())

    def watch_theme(self, old: str, new: str) -> None:
        """Persist theme choice to global preferences."""
        if new:
            from .rooms import load_prefs, save_prefs

            prefs = load_prefs(DEFAULTS_KEY)
            prefs["tui_theme"] = new
            save_prefs(DEFAULTS_KEY, prefs)


def tui_main() -> None:
    """Launch the Textual TUI session manager."""
    app = TelnetSessionApp()
    app.run()
    # Move cursor to bottom-right corner and print a newline while still in
    # the alternate screen, then exit fullscreen and print another newline
    # so the shell prompt appears on a clean line.
    sys.stdout.write("\x1b[999;999H\n" + _TERMINAL_CLEANUP + "\n")
    sys.stdout.flush()
