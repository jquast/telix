"""
Session management layer for the Textual TUI.

Provides session configuration, persistence, command building, the session
list/edit/theme screens, and related helper functions.  No imports from other
``client_tui_*`` files except ``client_tui_base`` (for base editor classes
and help screen).
"""

# std imports
import os
import sys
import json
import time
import shlex
import codecs
import typing
import logging
import datetime
import subprocess
import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.widget import Widget

# 3rd party
import textual.app
import textual.events
import textual.screen
import textual.binding
import textual.widgets
import textual.css.query
import textual.containers

# local
from . import util, paths, terminal

log = logging.getLogger(__name__)


PRIMARY_PASTE_COMMANDS = (
    ("xclip", "-selection", "primary", "-o"),
    ("xsel", "--primary", "--output"),
    ("wl-paste", "--primary", "--no-newline"),
)


def read_primary_selection() -> str:
    """Read text from the X11/Wayland primary selection via external helper."""
    for cmd in PRIMARY_PASTE_COMMANDS:
        try:
            log.debug("launch: %s", shlex.join(cmd))
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
    "big5bbs",
    "gbk",
    "euc-kr",
    "shift-jis",
    "atascii",
    "petscii",
)

# Map Python canonical codec name -> ENCODINGS entry.  Built in reverse order
# so the first entry in ENCODINGS wins when two entries share a canonical name
# (e.g. "latin-1" and "iso-8859-1" both resolve to "iso8859-1").
_CODEC_MAP: dict[str, str] = {}
for _enc in reversed(ENCODINGS):
    try:
        _CODEC_MAP[codecs.lookup(_enc).name] = _enc
    except LookupError:
        pass


def normalize_encoding(enc: str) -> str:
    """
    Return the ENCODINGS entry that best matches *enc*, or ``ENCODINGS[0]``.

    Strips whitespace then resolves Python codec aliases so that e.g.
    ``"utf-8"``, ``"latin1"``, or ``" cp437 "`` each map to the canonical
    ENCODINGS label.
    """
    enc = enc.strip()
    if enc in ENCODINGS:
        return enc
    try:
        canonical = codecs.lookup(enc).name
        if canonical in _CODEC_MAP:
            return _CODEC_MAP[canonical]
    except LookupError:
        pass
    return ENCODINGS[0]


DEFAULTS_KEY = "__defaults__"
BATCH_SIZE = 100


# Map CLI flag names (without leading --) to TUI widget IDs.
FLAG_TO_WIDGET: dict[str, str] = {
    "term": "term",
    "encoding": "encoding",
    "encoding-errors": "encoding-errors",
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


def navigate_from_button(
    key: str,
    focused: textual.widgets.Button,
    buttons: list[typing.Any],
    target: typing.Any,
    screen: "textual.screen.Screen[object]",
    event: textual.events.Key,
) -> None:
    """
    Handle arrow navigation when a button in the column is focused.

    :param key: The key name (e.g. ``"up"``, ``"down"``, ``"right"``).
    :param focused: The currently focused widget.
    :param buttons: Ordered list of buttons in the column.
    :param target: Widget to focus on ``"right"`` press.
    :param screen: The screen owning the widgets.
    :param event: The key event to call :meth:`prevent_default` on.
    """
    idx = buttons.index(focused)
    if key == "up" and idx > 0:
        buttons[idx - 1].focus()
        event.prevent_default()
    elif key == "down" and idx < len(buttons) - 1:
        buttons[idx + 1].focus()
        event.prevent_default()
    elif key == "right":
        screen.call_later(target.focus)
        event.prevent_default()


def navigate_from_table(
    key: str, buttons: list[typing.Any], screen: "textual.screen.Screen[object]", event: textual.events.Key
) -> None:
    """
    Handle arrow navigation when the data table is focused.

    :param key: The key name.
    :param buttons: Ordered list of buttons in the column.
    :param screen: The screen owning the widgets.
    :param event: The key event to call :meth:`prevent_default` on.
    """
    if key == "left":
        if buttons:
            screen.call_later(buttons[0].focus)
        event.prevent_default()


def handle_arrow_navigation(
    screen: "textual.screen.Screen[object]",
    event: textual.events.Key,
    button_col_selector: str,
    table_selector: str,
    form_selector: str = "",
) -> None:
    """
    Arrow key navigation between a button column, data table, and form.

    :param screen: The screen handling the key event.
    :param event: The key event.
    :param button_col_selector: CSS selector for the button column container.
    :param table_selector: CSS selector for the textual.widgets.DataTable.
    :param form_selector: CSS selector for the inline form (optional).
    """
    focused = screen.focused
    buttons = list(screen.query(f"{button_col_selector} Button"))
    table = screen.query_one(table_selector, textual.widgets.DataTable)

    if form_selector:
        try:
            form = screen.query_one(form_selector)
        except textual.css.query.NoMatches:
            form = None
        if form is not None and form.display:
            form_fields: list[textual.widgets.Input | textual.widgets.Switch | textual.widgets.Button] = [
                w
                for w in form.query("Input, Switch, Button")
                if isinstance(w, (textual.widgets.Input, textual.widgets.Switch, textual.widgets.Button))
            ]
            if focused in form_fields:
                idx = form_fields.index(focused)  # type: ignore[arg-type]
                if event.key == "up" and idx > 0:
                    form_fields[idx - 1].focus()
                    event.prevent_default()
                elif event.key == "down" and idx < len(form_fields) - 1:
                    form_fields[idx + 1].focus()
                    event.prevent_default()
                elif event.key == "left" and isinstance(focused, (textual.widgets.Switch, textual.widgets.Button)):
                    if buttons:
                        screen.call_later(buttons[0].focus)
                    event.prevent_default()
                return
            if isinstance(focused, textual.widgets.Button) and focused in buttons:
                if event.key == "right" and form_fields:
                    screen.call_later(form_fields[0].focus)
                    event.prevent_default()
                    return

    if isinstance(focused, textual.widgets.Input):
        return

    if isinstance(focused, textual.widgets.Button) and focused in buttons:
        navigate_from_button(event.key, focused, buttons, table, screen, event)
    elif focused is table:
        navigate_from_table(event.key, buttons, screen, event)


TOOLTIP_CACHE: dict[str, str] | None = None


def build_tooltips() -> dict[str, str]:
    """Extract help text from argparse and return ``{widget_id: help}``."""
    global TOOLTIP_CACHE
    if TOOLTIP_CACHE is not None:
        return TOOLTIP_CACHE
    from telnetlib3.client import _get_argument_parser  # noqa: ICN003

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


@dataclasses.dataclass
class SessionConfig:
    """
    Persistent configuration for a single session.

    Field defaults mirror the CLI defaults in
    :func:`telnetlib3.client.get_argument_parser`.
    """

    # Metadata
    name: str = ""
    last_connected: str = ""

    # Connection
    host: str = ""
    port: int = 23
    protocol: str = "telnet"  # "telnet", "websocket", or "ssh"
    ws_path: str = ""  # path appended to WebSocket URL (e.g. "/ws")
    ssh_username: str = ""  # empty = os.getlogin() at runtime
    ssh_key_file: str = ""  # path to private key; empty = password auth
    ssl: bool = False
    ssl_cafile: str = ""
    ssl_no_verify: bool = False

    # Terminal
    term: str = ""  # empty = use $TERM at runtime
    speed: int = 38400
    encoding: str = "utf8"
    encoding_errors: str = "replace"

    # Mode: "auto", "raw", or "line"
    mode: str = "auto"

    # Display
    colormatch: str = "vga"
    color_brightness: float = 1.0
    color_contrast: float = 1.0
    background_color: str = "#000000"
    ice_colors: bool = True
    force_black_bg: bool = False

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
    logfile_mode: str = "append"
    typescript: str = ""
    typescript_mode: str = "append"
    no_repl: bool = False

    # Developer
    coverage: bool = False

    # Server type: "bbs", "mud", or "" (unset)
    server_type: str = ""

    # Bookmarked sessions sort to top of the list
    bookmarked: bool = False


def ensure_dirs() -> None:
    os.makedirs(paths.CONFIG_DIR, exist_ok=True)
    os.makedirs(paths.DATA_DIR, exist_ok=True)


def load_sessions() -> dict[str, SessionConfig]:
    """Load session configs from ``~/.config/telix/sessions.json``."""
    ensure_dirs()
    if not os.path.exists(paths.SESSIONS_FILE):
        return {}
    with open(paths.SESSIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    known = {f.name for f in dataclasses.fields(SessionConfig)}
    result: dict[str, SessionConfig] = {}
    for key, val in data.items():
        filtered = {k: v for k, v in val.items() if k in known}
        result[key] = SessionConfig(**filtered)
    return result


def save_sessions(sessions: dict[str, SessionConfig]) -> None:
    """Save session configs to ``~/.config/telix/sessions.json``."""
    ensure_dirs()
    data = {key: dataclasses.asdict(cfg) for key, cfg in sessions.items()}
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    paths.atomic_write(str(paths.SESSIONS_FILE), content)


CMD_STR_FLAGS: list[tuple[str, str, object]] = [
    ("term", "--term", ""),
    ("encoding", "--encoding", "utf8"),
    ("speed", "--speed", 38400),
    ("encoding_errors", "--encoding-errors", "replace"),
    ("connect_minwait", "--connect-minwait", 0.0),
    ("connect_maxwait", "--connect-maxwait", 4.0),
    ("send_environ", "--send-environ", "TERM,LANG,COLUMNS,LINES,COLORTERM"),
    ("loglevel", "--loglevel", "warn"),
    ("logfile", "--logfile", ""),
    ("logfile_mode", "--logfile-mode", "append"),
    ("typescript", "--typescript", ""),
    ("typescript_mode", "--typescript-mode", "append"),
    ("ssl_cafile", "--ssl-cafile", ""),
]

CMD_BOOL_FLAGS: list[tuple[str, str, bool]] = [
    ("ssl", "--ssl", False),
    ("ssl_no_verify", "--ssl-no-verify", False),
    ("ansi_keys", "--ansi-keys", False),
    ("ascii_eol", "--ascii-eol", False),
]

CMD_NEG_BOOL_FLAGS: list[tuple[str, str, bool]] = []

# Telix-specific flags parsed by telix.main, not telnetlib3.
TELIX_STR_FLAGS: list[tuple[str, str, object]] = [
    ("colormatch", "--colormatch", "vga"),
    ("color_brightness", "--color-brightness", 1.0),
    ("color_contrast", "--color-contrast", 1.0),
    ("background_color", "--background-color", "#000000"),
]

TELIX_NEG_BOOL_FLAGS: list[tuple[str, str, bool]] = [("ice_colors", "--no-ice-colors", True)]


def build_command(config: SessionConfig) -> list[str]:
    """
    Build CLI arguments from *config*.

    Dispatches to the appropriate builder based on ``config.protocol``.
    """
    if config.protocol == "websocket":
        return build_ws_command(config)
    if config.protocol == "ssh":
        return build_ssh_command(config)
    return build_telnet_command(config)


def build_telnet_command(config: SessionConfig) -> list[str]:
    """
    Build ``telix`` CLI arguments from *config*.

    Uses telix's own entry point so telix-specific flags (color, REPL) are parsed before telnetlib3 sees the remaining
    arguments. Only emits flags that differ from the CLI defaults.
    """
    cmd = [sys.executable, "-c", "from telix.main import main; main()", config.host, str(config.port)]

    if config.no_repl:
        cmd.append("--no-repl")

    for attr, flag, default in TELIX_STR_FLAGS:
        val = getattr(config, attr)
        if val != default:
            cmd.extend([flag, str(val)])

    for attr, flag, default in TELIX_NEG_BOOL_FLAGS:
        if getattr(config, attr) != default:
            cmd.append(flag)

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


def build_ws_command(config: SessionConfig) -> list[str]:
    """
    Build WebSocket client CLI arguments from *config*.

    Constructs a ``ws://`` or ``wss://`` URL from the session config fields.
    """
    scheme = "wss" if config.ssl else "ws"
    standard_port = 443 if config.ssl else 80
    if config.port == standard_port:
        host_part = config.host
    else:
        host_part = f"{config.host}:{config.port}"
    ws_path = config.ws_path
    if ws_path and not ws_path.startswith("/"):
        ws_path = "/" + ws_path
    url = f"{scheme}://{host_part}{ws_path}"
    if config.coverage:
        cmd = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--source=telix",
            "--branch",
            "--parallel-mode",
            "-m",
            "telix.main",
            url,
        ]
    else:
        cmd = [sys.executable, "-c", "from telix.main import main; main()", url]
    if config.no_repl:
        cmd.append("--no-repl")
    if config.mode == "raw":
        cmd.append("--raw-mode")
    elif config.mode == "line":
        cmd.append("--line-mode")
    if config.ansi_keys:
        cmd.append("--ansi-keys")
    if config.ascii_eol:
        cmd.append("--ascii-eol")
    if config.compression is True:
        cmd.append("--compression")
    elif config.compression is False:
        cmd.append("--no-compression")
    for field, flag, default in [
        ("encoding", "--encoding", "utf8"),
        ("encoding_errors", "--encoding-errors", "replace"),
        ("term", "--term", ""),
        ("speed", "--speed", 38400),
        ("send_environ", "--send-environ", "TERM,LANG,COLUMNS,LINES,COLORTERM"),
        ("connect_minwait", "--connect-minwait", 0.0),
        ("connect_maxwait", "--connect-maxwait", 4.0),
        ("loglevel", "--loglevel", "warn"),
        ("logfile", "--logfile", ""),
        ("typescript", "--typescript", ""),
        ("logfile_mode", "--logfile-mode", "append"),
        ("typescript_mode", "--typescript-mode", "append"),
        ("colormatch", "--colormatch", "vga"),
        ("color_brightness", "--color-brightness", 1.0),
        ("color_contrast", "--color-contrast", 1.0),
        ("background_color", "--background-color", "#000000"),
    ]:
        value = getattr(config, field)
        if value != default:
            cmd += [flag, str(value)]
    if config.connect_timeout != 10.0:
        cmd += ["--connect-timeout", str(config.connect_timeout)]
    for attr, flag in (("always_will", "--always-will"), ("always_do", "--always-do")):
        for opt in getattr(config, attr).split(","):
            if opt := opt.strip():
                cmd.extend([flag, opt])
    if not config.ice_colors:
        cmd.append("--no-ice-colors")
    return cmd


def build_ssh_command(config: SessionConfig) -> list[str]:
    """
    Build ``telix-ssh`` CLI arguments from *config*.

    SSH connections always use BBS mode (raw, VGA palette, iCE colors);
    those flags are applied automatically by :func:`~telix.ssh_client.main`.
    """
    cmd = [sys.executable, "-c", "from telix.ssh_client import main; main()", config.host]
    if config.port != 22:
        cmd += ["--port", str(config.port)]
    if config.ssh_username:
        cmd += ["--username", config.ssh_username]
    if config.ssh_key_file:
        cmd += ["--key-file", config.ssh_key_file]
    if config.colormatch != "vga":
        cmd += ["--colormatch", config.colormatch]
    if not config.ice_colors:
        cmd.append("--no-ice-colors")
    if config.background_color != "#000000":
        cmd += ["--background-color", config.background_color]
    if config.color_brightness != 1.0:
        cmd += ["--color-brightness", str(config.color_brightness)]
    if config.color_contrast != 1.0:
        cmd += ["--color-contrast", str(config.color_contrast)]
    if config.loglevel != "warn":
        cmd += ["--loglevel", config.loglevel]
    if config.logfile:
        cmd += ["--logfile", config.logfile]
    if config.typescript:
        cmd += ["--typescript", config.typescript]
    return cmd


def relative_time(iso_str: str) -> str:
    """Return a short relative-time string like ``'5m ago'`` or ``'3d ago'``."""
    return util.relative_time(iso_str)


# Reset SGR, cursor, scroll region, alt-screen, mouse, and bracketed paste,
# then move cursor home and clear the screen so tracebacks start clean.
# \x1b[r (DECSTBM) must come before \x1b[?1049l so it resets the alt-screen
# scroll region before switching to the normal screen.
TERMINAL_CLEANUP = (
    "\x1b[m\x1b[?25h\x1b[r\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[H\x1b[2J"
)


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


class SessionListScreen(textual.screen.Screen[None]):
    """Main screen: table of saved sessions with action buttons."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("q", "quit_app", "Quit"),
        textual.binding.Binding("n", "new_session", "New"),
        textual.binding.Binding("e", "edit_session", "Edit"),
        textual.binding.Binding("c", "copy_session", "Copy"),
        textual.binding.Binding("b", "toggle_bookmark", "Bookmark"),
        textual.binding.Binding("d", "delete_session", "Delete"),
        textual.binding.Binding("t", "theme", "Theme"),
        textual.binding.Binding("enter", "connect", "Connect"),
        textual.binding.Binding("f1", "show_help", "Help"),
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
    #search-row { height: auto; margin-bottom: 0; }
    #session-search { width: 1fr; }
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
    #copy-btn {
        background: dodgerblue 30%;
        color: $text;
    }
    #copy-btn:hover {
        background: dodgerblue 50%;
    }
    #copy-btn:focus {
        background: dodgerblue 50%;
    }
    #edit-btn {
        background: mediumorchid 30%;
        color: $text;
    }
    #edit-btn:hover {
        background: mediumorchid 50%;
    }
    #edit-btn:focus {
        background: mediumorchid 50%;
    }
    #theme-btn {
        width: auto;
        min-width: 0;
        margin-left: 1;
        background: teal 30%;
        color: $text;
    }
    #theme-btn:hover {
        background: teal 50%;
    }
    #theme-btn:focus {
        background: teal 50%;
    }
    """

    def __init__(self) -> None:
        """Initialize session list with empty session dict."""
        super().__init__()
        self.sessions: dict[str, SessionConfig] = {}
        self.pending_rows: list[tuple[str, SessionConfig]] = []
        self.refresh_gen: int = 0
        self.cursor_just_moved: bool = False

    def compose(self) -> textual.app.ComposeResult:
        """Build the session list layout."""
        with textual.containers.Vertical(id="session-panel"):
            yield textual.widgets.Static(" ")
            with textual.containers.Horizontal(id="search-row"):
                yield textual.widgets.Input(placeholder="Search sessions\u2026", id="session-search")
                yield textual.widgets.Button("Theme", variant="default", id="theme-btn")
            yield textual.widgets.Static(" ")
            with textual.containers.Horizontal(id="session-body"):
                with textual.containers.Vertical(id="button-col"):
                    yield textual.widgets.Button("Connect", variant="primary", id="connect-btn")
                    yield textual.widgets.Button("New", variant="success", id="add-btn")
                    yield textual.widgets.Button("Bookmark", variant="warning", id="bookmark-btn")
                    yield textual.widgets.Button("Delete", variant="error", id="delete-btn")
                    yield textual.widgets.Button("Copy", variant="default", id="copy-btn")
                    yield textual.widgets.Button("Edit", variant="default", id="edit-btn")
                yield textual.widgets.DataTable(id="session-table")
        yield textual.widgets.Footer()

    def on_mount(self) -> None:
        """Load sessions and populate the data table."""
        self.sessions = load_sessions()
        if not self.sessions:
            from .directory import directory_to_sessions

            self.sessions = directory_to_sessions()
            save_sessions(self.sessions)
        table = self.query_one("#session-table", textual.widgets.DataTable)
        table.cursor_type = "row"
        table.add_column(" ", width=4, key="icon")
        table.add_column("Host/Name", width=20, key="name")
        table.add_column("Type", width=5, key="type")
        table.add_column("Port", width=6, key="port")
        table.add_column("Enc", width=5, key="enc")
        table.add_column("Last", width=8, key="last")
        table.add_column("Flags", width=12, key="flags")
        self.resize_name_column()
        self.refresh_table()
        if table.row_count > 0:
            table.focus()

    def resize_name_column(self) -> None:
        """Set Host/Name column width to fill available space."""
        table = self.query_one("#session-table", textual.widgets.DataTable)
        # Fixed columns: icon(4) + Type(5) + Port(6) + Enc(5) + Last(8) + Flags(12) = 40
        # Button col(12) + padding/borders(6) + column gutters(11)
        fixed = 40 + 12 + 6 + 11
        name_w = max(16, self.app.size.width - fixed)
        col = table.columns.get("name")  # type: ignore[call-overload]
        if col is not None:
            col.width = name_w

    def on_resize(self, event: textual.events.Resize) -> None:
        """Recalculate name column width on terminal resize."""
        self.resize_name_column()

    @staticmethod
    def flags(cfg: SessionConfig) -> str:
        """Return short flag codes summarizing non-default session options."""
        parts: list[str] = []
        if cfg.protocol == "websocket":
            parts.append("ws")
        if cfg.ssl:
            parts.append("ssl")
        if cfg.mode == "raw":
            parts.append("raw")
        elif cfg.mode == "line":
            parts.append("line")
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

    def add_rows(self, table: "textual.widgets.DataTable[object]", items: list[tuple[str, SessionConfig]]) -> None:
        """Add a list of ``(key, cfg)`` pairs as rows to *table*."""
        for key, cfg in items:
            table.add_row(
                "\u2021" if cfg.bookmarked else "",
                cfg.name or cfg.host,
                cfg.server_type.upper() if cfg.server_type else "",
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
        table = self.query_one("#session-table", textual.widgets.DataTable)
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
        table = self.query_one("#session-table", textual.widgets.DataTable)
        batch, self.pending_rows = (self.pending_rows[:BATCH_SIZE], self.pending_rows[BATCH_SIZE:])
        self.add_rows(table, batch)
        if self.pending_rows:
            self.call_later(self.load_next_batch, gen)

    def on_input_changed(self, event: textual.widgets.Input.Changed) -> None:
        """Filter session table when search input changes."""
        if event.input.id == "session-search":
            self.refresh_table(event.value)

    def save(self) -> None:
        save_sessions(self.sessions)

    def session_keys(self) -> list[str]:
        return [k for k in self.sessions if k != DEFAULTS_KEY]

    def selected_key(self) -> str | None:
        table = self.query_one("#session-table", textual.widgets.DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return str(row_key.value)

    def on_key(self, event: textual.events.Key) -> None:
        """Arrow/Home/End keys navigate between search, theme button, buttons, and the table."""
        search_input = self.query_one("#session-search", textual.widgets.Input)
        table = self.query_one("#session-table", textual.widgets.DataTable)
        theme_btn = self.query_one("#theme-btn", textual.widgets.Button)

        if self.focused is table and event.key == "enter":
            self.action_connect()
            event.prevent_default()
            return

        # Right from search -> theme button; left from theme -> search
        if self.focused is search_input and event.key == "right":
            theme_btn.focus()
            event.prevent_default()
            return
        if self.focused is theme_btn and event.key == "left":
            search_input.focus()
            event.prevent_default()
            return
        # Down from theme button -> table
        if self.focused is theme_btn and event.key == "down":
            table.focus()
            event.prevent_default()
            return
        # Up from table row 0 or first button-col button -> search row
        if self.focused is theme_btn and event.key == "up":
            event.prevent_default()
            return

        if event.key in ("up", "down"):
            if self.focused is search_input and event.key == "down":
                table.focus()
                event.prevent_default()
                return
            if self.focused is table and event.key == "up" and table.cursor_row == 0:
                search_input.focus()
                event.prevent_default()
                return
            buttons = list(self.query("#button-col Button"))
            if event.key == "up" and buttons and self.focused is buttons[0]:
                search_input.focus()
                event.prevent_default()
                return

        if event.key in ("home", "end"):
            if self.focused is table and table.row_count > 0:
                row = 0 if event.key == "home" else table.row_count - 1
                table.move_cursor(row=row)
                event.prevent_default()
        elif event.key in ("up", "down", "left", "right"):
            handle_arrow_navigation(self, event, "#button-col", "#session-table")  # type: ignore[arg-type]

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
        """Dispatch button press to the appropriate action."""
        handlers = {
            "connect-btn": self.action_connect,
            "add-btn": self.action_new_session,
            "copy-btn": self.action_copy_session,
            "bookmark-btn": self.action_toggle_bookmark,
            "edit-btn": self.action_edit_session,
            "delete-btn": self.action_delete_session,
            "theme-btn": self.action_theme,
            "quit-btn": self.action_quit_app,
        }
        handler = handlers.get(event.button.id or "")
        if handler:
            handler()

    def on_data_table_row_highlighted(self, event: textual.widgets.DataTable.RowHighlighted) -> None:
        """Track that the cursor just moved to a new row."""
        self.cursor_just_moved = True

    def on_data_table_row_selected(self, event: textual.widgets.DataTable.RowSelected) -> None:
        """Connect on mouse: skip the first click (which moves the cursor), connect on the second."""
        if self.cursor_just_moved:
            self.cursor_just_moved = False
            return
        self.action_connect()

    def action_quit_app(self) -> None:
        """Exit the application."""
        self.app.exit()

    def action_new_session(self) -> None:
        """Open editor for a new session pre-filled with defaults."""
        defaults = self.sessions.get(DEFAULTS_KEY, SessionConfig())
        new_cfg = SessionConfig(**dataclasses.asdict(defaults))
        new_cfg.name = ""
        new_cfg.host = ""
        new_cfg.last_connected = ""
        self.app.push_screen(SessionEditScreen(config=new_cfg, is_new=True), callback=self.do_edit_result)

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
            table = self.query_one("#session-table", textual.widgets.DataTable)
            if config is None:
                table.focus()
                return
            new_key = config.name or config.host
            if not new_key:
                table.focus()
                return
            if new_key != old_key and old_key in self.sessions:
                del self.sessions[old_key]
            self.sessions[new_key] = config
            self.save()
            self.refresh_table()
            self.select_row(new_key)
            table.focus()

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
            callback=do_confirm,  # type: ignore[arg-type]
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

    def action_copy_session(self) -> None:
        """Duplicate the selected session with a unique name."""
        key = self.require_selected()
        if key is None:
            return
        cfg = self.sessions[key]
        new_cfg = SessionConfig(**dataclasses.asdict(cfg))
        new_cfg.last_connected = ""
        base = cfg.name or cfg.host
        n = 1
        while True:
            candidate = f"{base} ({n})"
            if candidate not in self.sessions:
                break
            n += 1
        new_cfg.name = candidate
        self.sessions[candidate] = new_cfg
        self.save()
        self.refresh_table()
        self.select_row(candidate)
        self.notify(f"Copied as '{candidate}'")

    def action_show_help(self) -> None:
        """Open the session manager help screen."""
        from . import client_tui_base

        self.app.push_screen(client_tui_base.CommandHelpScreen(topic="session"))

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
            _elapsed = 0.0
            try:
                # stderr must NOT be piped -- the child may launch
                # Textual subprocesses (F8/F9 editors) that write all
                # output to sys.__stderr__.  A piped stderr would send
                # that output into the pipe instead of the terminal,
                # hanging the editor.
                env = None
                if cfg.coverage:
                    cov_rc = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tox.ini")
                    env = {**os.environ, "COVERAGE_PROCESS_START": cov_rc}
                    log.debug("coverage enabled, COVERAGE_PROCESS_START=%s", cov_rc)
                log.debug("launch: %s", shlex.join(cmd))
                _t0 = time.monotonic()
                proc = subprocess.Popen(cmd, env=env)
                proc.wait()
                _elapsed = time.monotonic() - _t0
                if proc.returncode:
                    os._exit(proc.returncode)
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
                terminal.restore_io_blocking()
                # Reset terminal to known-good state -- the child may
                # have left raw mode, SGR attributes, mouse tracking,
                # or alternate screen active.
                sys.stdout.write(TERMINAL_CLEANUP)
                sys.stdout.flush()
                # If the session exited very quickly, the child likely crashed
                # before fully starting.  Pause so the error output is visible
                # rather than being wiped by the TUI redraw.
                if _elapsed < 5.0:
                    sys.stdout.write("\r\n[press Enter to return to session manager]\r\n")
                    sys.stdout.flush()
                    sys.stdin.readline()
        self.refresh_table()
        self.select_row(key)
        # Textual's app.suspend() re-enters the alternate screen buffer
        # (which is blank) but does not mark the screen dirty for a full
        # repaint.  Without this, widgets only redraw on hover.
        self.screen.refresh()

    def action_theme(self) -> None:
        """Open the theme picker modal."""
        self.app.push_screen(ThemePickerScreen())

    def do_edit_result(self, config: SessionConfig | None) -> None:
        table = self.query_one("#session-table", textual.widgets.DataTable)
        if config is None:
            table.focus()
            return
        key = config.name or config.host
        if not key:
            table.focus()
            return
        self.sessions[key] = config
        self.save()
        self.refresh_table()
        self.select_row(key)
        table.focus()

    def select_row(self, key: str) -> None:
        """Move the table cursor to the row with the given key."""
        table = self.query_one("#session-table", textual.widgets.DataTable)
        for row_idx, row_key in enumerate(table.rows):
            if str(row_key.value) == key:
                table.move_cursor(row=row_idx)
                break


class ThemePickerScreen(textual.screen.Screen[str | None]):
    """Modal screen wrapping ThemeEditPane for standalone theme selection."""

    BINDINGS: typing.ClassVar[list[textual.binding.Binding]] = [  # type: ignore[assignment]
        textual.binding.Binding("escape", "cancel", "Cancel", priority=True)
    ]

    CSS = """
    ThemePickerScreen {
        height: 100%;
        width: 100%;
    }
    """

    def compose(self) -> textual.app.ComposeResult:
        """Compose the theme picker with modal ThemeEditPane."""
        from . import client_tui_bars

        yield client_tui_bars.ThemeEditPane(modal=True)

    def on_theme_edit_pane_saved(self, event: "object") -> None:
        """Dismiss with the current theme on save."""
        self.dismiss(self.app.theme)

    def on_theme_edit_pane_cancelled(self, event: "object") -> None:
        """Dismiss with None on cancel."""
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Restore original theme and dismiss."""
        from . import client_tui_bars

        try:
            pane = self.query_one(client_tui_bars.ThemeEditPane)
            self.app.theme = pane.original_theme
        except textual.css.query.NoMatches:
            pass
        self.dismiss(None)


class SessionEditScreen(textual.screen.Screen[SessionConfig | None]):
    """Full-screen form for adding or editing a session."""

    CSS = """
    SessionEditScreen {
        align: center middle;
    }
    #edit-panel {
        width: 100%;
        max-width: 65;
        height: 100%;
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
        height: 1fr;
    }
    .field-row {
        height: 3;
        margin-bottom: 0;
    }
    .field-label {
        width: 14;
        padding-top: 1;
    }
    .field-label-wide {
        width: 28;
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
    #logfile-mode, #typescript-mode {
        width: auto;
        height: auto;
    }
    #loglevel-spacer {
        width: 22;
    }
    #tab-terminal > *, #tab-display > *, #tab-advanced > * {
        margin-bottom: 1;
    }
    .switch-row {
        height: 3;
    }
    .conn-label {
        width: 8;
        text-align: right;
        padding-top: 1;
    }
    #name-server-row {
        height: auto;
        margin-bottom: 1;
    }
    #name {
        width: 31;
    }
    #protocol-radio {
        height: auto;
    }
    #proto-details-row {
        height: auto;
        margin-bottom: 1;
    }
    #protocol-col {
        width: auto;
        height: auto;
        padding-right: 2;
    }
    #conn-details-col {
        width: 1fr;
        height: auto;
    }
    #port {
        width: 12;
    }
    #ws-path-row {
        height: auto;
        display: none;
    }
    #ws-path-row.visible {
        display: block;
    }
    #ws-path {
        width: 1fr;
    }
    #ssh-details-row {
        height: auto;
        display: none;
    }
    #ssh-details-row.visible {
        display: block;
    }
    #compression-row {
        height: auto;
    }
    #server-type-col {
        width: 2fr;
        height: auto;
    }
    #server-type-label {
        width: 12;
        text-align: right;
        padding-top: 1;
    }
    #compression-col {
        width: 22;
        height: auto;
    }
    #connect-timeout {
        max-width: 13;
    }
    #conn-timeout-label {
        width: 20;
        padding-top: 1;
    }
    #coverage-label {
        padding-top: 1;
        padding-left: 2;
        width: auto;
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
    .field-input-short {
        max-width: 10;
    }
    #palette-preview {
        width: 1fr;
        padding-top: 1;
    }
    #detected-bg-color {
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

    def __init__(self, config: SessionConfig, is_defaults: bool = False, is_new: bool = False) -> None:
        """Initialize edit screen with session config and mode flags."""
        super().__init__()
        self.config = config
        self.is_defaults = is_defaults
        self.is_new = is_new
        bg_env = os.environ.get("TELIX_DETECTED_BG")
        self.detected_bg = tuple(int(x) for x in bg_env.split(",")) if bg_env else None

    TAB_IDS: typing.ClassVar[list[tuple[str, str]]] = [
        ("Connection", "tab-connection"),
        ("Terminal", "tab-terminal"),
        ("Display", "tab-display"),
        ("Advanced", "tab-advanced"),
    ]

    @staticmethod
    def field_row(label: str, *widgets: "Widget", row_class: str = "field-row") -> textual.containers.Horizontal:
        """Return a ``Horizontal`` row with a label and widgets."""
        return textual.containers.Horizontal(
            textual.widgets.Label(label, classes="field-label"), *widgets, classes=row_class
        )

    def compose_connection_tab(self, cfg: SessionConfig) -> textual.app.ComposeResult:
        """Yield widgets for the Connection tab pane."""
        is_ssh = cfg.protocol == "ssh"
        if not self.is_defaults:
            if cfg.protocol == "ssh":
                proto_id_init = "proto-ssh"
            elif cfg.protocol == "websocket":
                proto_id_init = "proto-wss" if cfg.ssl else "proto-ws"
            else:
                proto_id_init = "proto-telnets" if cfg.ssl else "proto-telnet"
            with textual.containers.Horizontal(id="name-server-row"):
                yield textual.widgets.Label("Name", classes="conn-label")
                yield textual.widgets.Input(value=cfg.name, placeholder="optional display name", id="name")
                with textual.containers.Horizontal(id="server-type-col"):
                    yield textual.widgets.Label("Server Type", id="server-type-label")
                    with textual.widgets.RadioSet(id="server-type-radio"):
                        yield textual.widgets.RadioButton(
                            "BBS", value=is_ssh or cfg.server_type == "bbs", id="type-bbs"
                        )
                        yield textual.widgets.RadioButton(
                            "Mud", value=not is_ssh and cfg.server_type == "mud", id="type-mud"
                        )
            with textual.containers.Horizontal(id="proto-details-row"):
                with textual.containers.Horizontal(id="protocol-col"):
                    yield textual.widgets.Label("Protocol", classes="conn-label")
                    with textual.widgets.RadioSet(id="protocol-radio"):
                        yield textual.widgets.RadioButton(
                            "telnet", value=proto_id_init == "proto-telnet", id="proto-telnet"
                        )
                        yield textual.widgets.RadioButton(
                            "telnets", value=proto_id_init == "proto-telnets", id="proto-telnets"
                        )
                        yield textual.widgets.RadioButton("ws", value=proto_id_init == "proto-ws", id="proto-ws")
                        yield textual.widgets.RadioButton("wss", value=proto_id_init == "proto-wss", id="proto-wss")
                        yield textual.widgets.RadioButton("ssh", value=proto_id_init == "proto-ssh", id="proto-ssh")
                with textual.containers.Vertical(id="conn-details-col"):
                    yield textual.containers.Horizontal(
                        textual.widgets.Label("Host", classes="conn-label"),
                        textual.widgets.Input(value=cfg.host, placeholder="hostname", id="host", classes="field-input"),
                        classes="field-row",
                    )
                    yield textual.containers.Horizontal(
                        textual.widgets.Label("Port", classes="conn-label"),
                        textual.widgets.Input(
                            value=str(cfg.port),
                            placeholder="22"
                            if is_ssh
                            else ("443" if cfg.ssl else ("80" if cfg.protocol == "websocket" else "23")),
                            id="port",
                        ),
                        classes="field-row",
                    )
                    yield textual.containers.Horizontal(
                        textual.widgets.Label("Path", classes="conn-label"),
                        textual.widgets.Input(
                            value=cfg.ws_path,
                            placeholder="/ws",
                            id="ws-path",
                            tooltip="WebSocket path appended to URL",
                            classes="field-input",
                        ),
                        classes="field-row",
                        id="ws-path-row",
                    )
                    with textual.containers.Vertical(id="ssh-details-row"):
                        yield textual.containers.Horizontal(
                            textual.widgets.Label("User", classes="conn-label"),
                            textual.widgets.Input(
                                value=cfg.ssh_username,
                                placeholder="username",
                                id="ssh-username",
                                tooltip="SSH login username (empty = system login)",
                                classes="field-input",
                            ),
                            classes="field-row",
                        )
                        yield textual.containers.Horizontal(
                            textual.widgets.Label("Key", classes="conn-label"),
                            textual.widgets.Input(
                                value=cfg.ssh_key_file,
                                placeholder="path to private key",
                                id="ssh-key-file",
                                tooltip="Path to SSH private key file (empty = password auth)",
                                classes="field-input",
                            ),
                            classes="field-row",
                        )
        else:
            with textual.containers.Horizontal(id="server-type-col"):
                yield textual.widgets.Label("Server Type", id="server-type-label")
                with textual.widgets.RadioSet(id="server-type-radio"):
                    yield textual.widgets.RadioButton("BBS", value=cfg.server_type == "bbs", id="type-bbs")
                    yield textual.widgets.RadioButton("Mud", value=cfg.server_type == "mud", id="type-mud")
        with textual.containers.Horizontal(id="compression-row"):
            with textual.containers.Vertical(id="compression-col"):
                yield textual.widgets.Label("MCCP Compression")
                with textual.widgets.RadioSet(id="compression-radio"):
                    yield textual.widgets.RadioButton("Passive", value=cfg.compression is None, id="compress-passive")
                    yield textual.widgets.RadioButton("Yes", value=cfg.compression is True, id="compress-yes")
                    yield textual.widgets.RadioButton("No", value=cfg.compression is False, id="compress-no")

    def compose_terminal_tab(self, cfg: SessionConfig) -> textual.app.ComposeResult:
        """Yield widgets for the Terminal tab pane."""
        yield self.field_row(
            "TERM",
            textual.widgets.Input(
                value=cfg.term, placeholder=os.environ.get("TERM", "unknown"), id="term", classes="field-input"
            ),
        )
        with textual.containers.Horizontal(id="mode-repl-row"):
            with textual.containers.Vertical(id="mode-col"):
                yield textual.widgets.Label("Terminal Mode")
                with textual.widgets.RadioSet(id="mode-radio"):
                    is_ssh = cfg.protocol == "ssh"
                    yield textual.widgets.RadioButton(
                        "Auto-detect", value=not is_ssh and cfg.mode == "auto", id="mode-auto"
                    )
                    yield textual.widgets.RadioButton("Raw mode", value=is_ssh or cfg.mode == "raw", id="mode-raw")
                    yield textual.widgets.RadioButton(
                        "Line mode", value=not is_ssh and cfg.mode == "line", id="mode-line"
                    )
            with textual.containers.Vertical(id="repl-col"), textual.containers.Horizontal(classes="switch-row"):
                repl_dim = "" if cfg.mode != "raw" else " dimmed"
                yield textual.widgets.Label("Advanced REPL", id="repl-label", classes=f"field-label{repl_dim}")
                yield textual.widgets.Switch(value=not cfg.no_repl, id="use-repl", disabled=cfg.mode == "raw")
        enc = normalize_encoding(cfg.encoding or "utf8")
        is_retro = enc.lower() in ("atascii", "petscii")
        with textual.containers.Horizontal(classes="field-row"):
            yield textual.widgets.Label("Encoding", id="enc-label")
            yield textual.widgets.Select([(e, e) for e in ENCODINGS], value=enc, id="encoding", allow_blank=False)
            yield textual.widgets.Label("Errors", id="enc-errors-label")
            yield textual.widgets.Select(
                [(v, v) for v in ("replace", "ignore", "strict")], value=cfg.encoding_errors, id="encoding-errors"
            )
        dim = "" if is_retro else " dimmed"
        with textual.containers.Horizontal(id="keys-eol-row"):
            with textual.containers.Horizontal(classes="switch-row"):
                yield textual.widgets.Label("ANSI Keys", id="ansi-keys-label", classes=f"field-label{dim}")
                sw = textual.widgets.Switch(value=cfg.ansi_keys, id="ansi-keys", disabled=not is_retro)
                sw.tooltip = "Required for Windows"
                yield sw
            with textual.containers.Horizontal(classes="switch-row"):
                yield textual.widgets.Label("ASCII EOL", id="ascii-eol-label", classes=f"field-label{dim}")
                yield textual.widgets.Switch(value=cfg.ascii_eol, id="ascii-eol", disabled=not is_retro)

    def compose_display_tab(self, cfg: SessionConfig) -> textual.app.ComposeResult:
        """Yield widgets for the Display tab pane."""
        yield textual.containers.Horizontal(
            textual.widgets.Label("Color Palette", classes="field-label"),
            textual.widgets.Select([(v, v) for v in ("vga", "xterm", "none")], value=cfg.colormatch, id="colormatch"),
            textual.widgets.Static("", id="palette-preview"),
            classes="field-row",
        )
        has_detected = self.detected_bg is not None
        if self.detected_bg is not None:
            r, g, b = self.detected_bg
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            yield textual.containers.Horizontal(
                textual.widgets.Label("Detected Background", classes="field-label-wide"),
                textual.widgets.Static(f"[on rgb({r},{g},{b})]  [/] {hex_color}", id="detected-bg-color"),
                classes="field-row",
            )
        yield textual.containers.Horizontal(
            textual.widgets.Label("Brightness %", classes="field-label"),
            textual.widgets.Input(
                value=str(int(cfg.color_brightness * 100)), id="color-brightness", classes="field-input-short"
            ),
            textual.widgets.Label("Contrast %", classes="field-label-short"),
            textual.widgets.Input(
                value=str(int(cfg.color_contrast * 100)), id="color-contrast", classes="field-input-short"
            ),
            classes="field-row",
        )
        force_val = True if not has_detected else cfg.force_black_bg
        with textual.containers.Horizontal():
            with textual.containers.Horizontal(classes="switch-row"):
                yield textual.widgets.Label("iCE Colors", classes="field-label")
                yield textual.widgets.Switch(value=cfg.ice_colors, id="ice-colors")
            with textual.containers.Horizontal(classes="switch-row"):
                yield textual.widgets.Label("Force Black BG", classes=f"field-label{'' if has_detected else ' dimmed'}")
                switch = textual.widgets.Switch(value=force_val, id="force-black-bg", disabled=not has_detected)
                if not has_detected:
                    switch.tooltip = "Could not detect background color of your terminal"
                yield switch

    def compose_advanced_tab(self, cfg: SessionConfig) -> textual.app.ComposeResult:
        """Yield widgets for the Advanced tab pane."""
        yield self.field_row(
            "Send Environ", textual.widgets.Input(value=cfg.send_environ, id="send-environ", classes="field-input")
        )
        yield textual.containers.Horizontal(
            textual.widgets.Label("LogFile", classes="field-label"),
            textual.widgets.Input(value=cfg.logfile, placeholder="path", id="logfile", classes="field-input"),
            textual.widgets.RadioSet(
                textual.widgets.RadioButton("Append", value=cfg.logfile_mode == "append"),
                textual.widgets.RadioButton("Rewrite", value=cfg.logfile_mode == "rewrite"),
                id="logfile-mode",
            ),
            classes="field-row",
        )
        yield textual.containers.Horizontal(
            textual.widgets.Label("LogLevel", classes="field-label"),
            textual.widgets.Select(
                [(v, v) for v in ("trace", "debug", "info", "warn", "error", "critical")],
                value=cfg.loglevel,
                id="loglevel",
                classes="field-input",
            ),
            textual.widgets.Static(id="loglevel-spacer"),
            classes="field-row",
        )
        yield textual.containers.Horizontal(
            textual.widgets.Label("Typescript", classes="field-label"),
            textual.widgets.Input(value=cfg.typescript, placeholder="path", id="typescript", classes="field-input"),
            textual.widgets.RadioSet(
                textual.widgets.RadioButton("Append", value=cfg.typescript_mode == "append"),
                textual.widgets.RadioButton("Rewrite", value=cfg.typescript_mode == "rewrite"),
                id="typescript-mode",
            ),
            classes="field-row",
        )
        yield textual.containers.Horizontal(
            textual.widgets.Label("Connection Timeout", id="conn-timeout-label", classes="field-label"),
            textual.widgets.Input(value=str(cfg.connect_timeout), id="connect-timeout", classes="field-input"),
            textual.widgets.Label("Coverage", id="coverage-label"),
            textual.widgets.Switch(value=cfg.coverage, id="coverage", tooltip="Track code coverage (for developers)"),
            classes="field-row",
        )

    def compose(self) -> textual.app.ComposeResult:
        """Build the tabbed session editor layout."""
        cfg = self.config
        with textual.containers.Vertical(id="edit-panel"):
            with textual.containers.Horizontal(id="tab-bar"):
                for i, (label, tab_id) in enumerate(self.TAB_IDS):
                    btn = textual.widgets.Button(label, id=f"tabbtn-{tab_id}")
                    if i == 0:
                        btn.add_class("active-tab")
                    yield btn

            with textual.widgets.ContentSwitcher(id="tab-content", initial="tab-connection"):
                with textual.containers.VerticalScroll(id="tab-connection", classes="tab-pane"):
                    yield from self.compose_connection_tab(cfg)
                with textual.containers.VerticalScroll(id="tab-terminal", classes="tab-pane"):
                    yield from self.compose_terminal_tab(cfg)
                with textual.containers.VerticalScroll(id="tab-display", classes="tab-pane"):
                    yield from self.compose_display_tab(cfg)
                with textual.containers.VerticalScroll(id="tab-advanced", classes="tab-pane"):
                    yield from self.compose_advanced_tab(cfg)

            with textual.containers.Horizontal(id="bottom-bar"):
                yield textual.widgets.Button("Cancel", variant="error", id="cancel-btn")
                yield textual.widgets.Button("Save", variant="success", id="save-btn")

    def on_mount(self) -> None:
        """Apply argparse-derived tooltips to form widgets."""
        tips = build_tooltips()
        for widget_id, help_text in tips.items():
            try:
                widget = self.query_one(f"#{widget_id}")
                widget.tooltip = help_text
            except textual.css.query.NoMatches:
                pass
        self.update_palette_preview()
        for radio_set in self.query(textual.widgets.RadioSet):
            idx = radio_set.pressed_index
            if idx >= 0:
                radio_set._selected = idx
        if not self.is_defaults:
            if self.config.protocol == "ssh":
                proto_id = "proto-ssh"
            elif self.config.protocol == "websocket":
                proto_id = "proto-wss" if self.config.ssl else "proto-ws"
            else:
                proto_id = "proto-telnets" if self.config.ssl else "proto-telnet"
            self.apply_protocol_visibility(proto_id)
            if self.config.protocol == "ssh":
                server_type_radio = self.query_one("#server-type-radio", textual.widgets.RadioSet)
                self.call_after_refresh(lambda rs=server_type_radio: setattr(rs, "disabled", True))

    def on_radio_set_changed(self, event: textual.widgets.RadioSet.Changed) -> None:
        """Handle radio-set changes for server type, protocol, and terminal mode."""
        if event.radio_set.id == "server-type-radio":
            self.apply_server_type(event.pressed.id)  # type: ignore[arg-type]
        elif event.radio_set.id == "protocol-radio":
            proto_id = event.pressed.id
            self.apply_protocol_visibility(proto_id or "")
            server_type_radio = self.query_one("#server-type-radio", textual.widgets.RadioSet)
            if proto_id == "proto-ssh":
                self.apply_server_type("type-bbs")
                self.select_radio("server-type-radio", "type-bbs")
                self.call_after_refresh(lambda rs=server_type_radio: setattr(rs, "disabled", True))
            else:
                server_type_radio.disabled = False
        elif event.radio_set.id == "mode-radio":
            is_raw = event.pressed.id == "mode-raw"
            repl_switch = self.query_one("#use-repl", textual.widgets.Switch)
            repl_switch.disabled = is_raw
            if is_raw:
                repl_switch.value = False
            else:
                repl_switch.value = True
            self.query_one("#repl-label", textual.widgets.Label).set_class(is_raw, "dimmed")

    def select_radio(self, radio_set_id: str, button_id: str) -> None:
        """
        Select a radio button by setting only the target button's value to ``True``.

        RadioSet's ``_on_radio_button_changed`` uses ``prevent(RadioButton.Changed)`` when it deselects the
        previously-pressed button, so only the target must be set to ``True`` -- setting other buttons to ``False``
        directly causes RadioSet to fight the change and leaves multiple buttons appearing selected.

        If the RadioSet was disabled, it is re-enabled for the value change and re-disabled after the next refresh so
        that the ``RadioButton.Changed`` event can be processed before the widget is locked again.
        """
        radio_set = self.query_one(f"#{radio_set_id}", textual.widgets.RadioSet)
        was_disabled = radio_set.disabled
        radio_set.disabled = False
        self.query_one(f"#{button_id}", textual.widgets.RadioButton).value = True
        if was_disabled:
            self.call_after_refresh(lambda rs=radio_set: setattr(rs, "disabled", True))

    def apply_server_type(self, button_id: str) -> None:
        """Apply preset field values for BBS or MUD server type."""
        if button_id == "type-none":
            return
        ssl_on = self.is_ssl_active()
        if button_id == "type-bbs":
            self.query_one("#colormatch", textual.widgets.Select).value = "vga"
            self.query_one("#ice-colors", textual.widgets.Switch).value = True
            self.select_radio("mode-radio", "mode-raw")
            self.query_one("#use-repl", textual.widgets.Switch).value = False
            self.query_one("#use-repl", textual.widgets.Switch).disabled = True
            self.query_one("#repl-label", textual.widgets.Label).set_class(True, "dimmed")
            if ssl_on:
                self.select_radio("compression-radio", "compress-no")
            else:
                self.select_radio("compression-radio", "compress-passive")
            force_bg = self.query_one("#force-black-bg", textual.widgets.Switch)
            if not force_bg.disabled:
                force_bg.value = True
            self.update_palette_preview()
            compress_label = "no (SSL)" if ssl_on else "passive"
            self.notify(f"BBS: Color Palette vga, iCE Colors on, Raw mode, REPL off, MCCP Compression {compress_label}")
        elif button_id == "type-mud":
            if ssl_on:
                self.select_radio("compression-radio", "compress-no")
            else:
                self.select_radio("compression-radio", "compress-yes")
            self.select_radio("mode-radio", "mode-line")
            self.query_one("#use-repl", textual.widgets.Switch).value = True
            self.query_one("#use-repl", textual.widgets.Switch).disabled = False
            self.query_one("#repl-label", textual.widgets.Label).set_class(False, "dimmed")
            self.query_one("#colormatch", textual.widgets.Select).value = "none"
            self.query_one("#ice-colors", textual.widgets.Switch).value = False
            force_bg = self.query_one("#force-black-bg", textual.widgets.Switch)
            if not force_bg.disabled:
                force_bg.value = False
            self.update_palette_preview()
            compress_label = "no (SSL)" if ssl_on else "yes"
            self.notify(
                f"MUD: MCCP Compression {compress_label}, Line mode, REPL on, Color Palette none, iCE Colors off"
            )

    def is_ssl_active(self) -> bool:
        """Return True if the currently-selected protocol implies SSL."""
        for proto_id in ("proto-telnets", "proto-wss"):
            try:
                if self.query_one(f"#{proto_id}", textual.widgets.RadioButton).value:
                    return True
            except textual.css.query.NoMatches:
                pass
        return False

    def apply_protocol_visibility(self, proto_id: str) -> None:
        """Toggle visibility of protocol-specific widgets based on selected protocol radio button id."""
        is_ws = proto_id in ("proto-ws", "proto-wss")
        is_ssh = proto_id == "proto-ssh"
        is_ssl = proto_id in ("proto-telnets", "proto-wss")
        self.query_one("#ws-path-row").set_class(is_ws, "visible")
        self.query_one("#ssh-details-row").set_class(is_ssh, "visible")
        self.query_one("#compression-row").display = not is_ssh
        self.query_one("#mode-radio", textual.widgets.RadioSet).disabled = is_ssh
        compression_radio = self.query_one("#compression-radio", textual.widgets.RadioSet)
        if is_ssl:
            self.select_radio("compression-radio", "compress-no")
            self.call_after_refresh(lambda rs=compression_radio: setattr(rs, "disabled", True))
        else:
            compression_radio.disabled = False
            if self.query_one("#compress-no", textual.widgets.RadioButton).value:
                self.select_radio("compression-radio", "compress-passive")
        port_input = self.query_one("#port", textual.widgets.Input)
        port_val = int_val(port_input.value, 0)
        if is_ssh:
            port_input.placeholder = "22"
            if port_val in (23, 80, 443):
                port_input.value = "22"
        elif is_ws:
            port_input.placeholder = "443" if is_ssl else "80"
            if is_ssl and port_val in (23, 22, 80):
                port_input.value = "443"
            elif not is_ssl and port_val in (23, 22, 443):
                port_input.value = "80"
        else:
            port_input.placeholder = "23"
            if port_val in (22, 80, 443):
                port_input.value = "23"

    def on_select_changed(self, event: textual.widgets.Select.Changed) -> None:
        """React to Select widget changes."""
        if event.select.id == "colormatch":
            self.update_palette_preview()
        elif event.select.id == "encoding":
            is_retro = str(event.value).lower() in ("atascii", "petscii")
            self.query_one("#ansi-keys", textual.widgets.Switch).disabled = not is_retro
            self.query_one("#ascii-eol", textual.widgets.Switch).disabled = not is_retro
            for label_id in ("#ansi-keys-label", "#ascii-eol-label"):
                label = self.query_one(label_id, textual.widgets.Label)
                label.set_class(not is_retro, "dimmed")

    def on_input_changed(self, event: textual.widgets.Input.Changed) -> None:
        """Update palette preview when brightness/contrast changes."""
        if event.input.id in ("color-brightness", "color-contrast"):
            self.update_palette_preview()

    def on_switch_changed(self, event: textual.widgets.Switch.Changed) -> None:
        """Handle switch changes for palette and other toggles."""
        if event.switch.id in ("ice-colors", "force-black-bg"):
            self.update_palette_preview()

    def update_palette_preview(self) -> None:
        """Render CP437 full-block color preview for the selected palette."""
        from telix.color_filter import PALETTES, _adjust_color

        palette_name = self.query_one("#colormatch", textual.widgets.Select).value
        preview = self.query_one("#palette-preview", textual.widgets.Static)
        if palette_name == "none" or palette_name not in PALETTES:
            preview.update("")
            return
        brightness = self._parse_pct("#color-brightness", 100) / 100.0
        contrast = self._parse_pct("#color-contrast", 100) / 100.0
        palette = [
            _adjust_color(r, g, b, brightness, contrast)
            for r, g, b in PALETTES[palette_name]  # type: ignore[index]
        ]
        force_black = self.query_one("#force-black-bg", textual.widgets.Switch).value
        if not force_black and self.detected_bg is not None:
            palette[0] = self.detected_bg  # type: ignore[assignment]
        ice = self.query_one("#ice-colors", textual.widgets.Switch).value
        block = "\u2588"
        fg_blocks = "".join(f"[rgb({r},{g},{b})]{block}[/]" for r, g, b in palette)
        bg_count = 16 if ice else 8
        bg_blocks = "".join(f"[on rgb({r},{g},{b})] [/]" for r, g, b in palette[:bg_count])
        preview.update(f"FG: {fg_blocks}\nBG: {bg_blocks}")

    def _parse_pct(self, widget_id: str, default: int) -> int:
        """Parse a percentage integer from an Input widget, clamped 0-100."""
        try:
            val = int(self.query_one(widget_id, textual.widgets.Input).value)
        except (ValueError, textual.css.query.NoMatches):
            return default
        return max(0, min(100, val))

    def switch_to_tab(self, tab_id: str) -> None:
        """Activate the given tab and update button styling."""
        self.query_one("#tab-content", textual.widgets.ContentSwitcher).current = tab_id
        for btn in self.query("#tab-bar Button"):
            btn.remove_class("active-tab")
            if btn.id == f"tabbtn-{tab_id}":
                btn.add_class("active-tab")

    def active_tab_focusables(self) -> list[typing.Any]:
        """Return focusable widgets in the currently visible tab pane."""
        current = self.query_one("#tab-content", textual.widgets.ContentSwitcher).current
        if not current:
            return []
        pane = self.query_one(f"#{current}")
        return [w for w in pane.query("Input, Select, Switch, RadioButton") if not w.disabled]

    def on_key(self, event: textual.events.Key) -> None:
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
                    switcher = self.query_one("#tab-content", textual.widgets.ContentSwitcher)
                    current = switcher.current
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

    def on_button_pressed(self, event: textual.widgets.Button.Pressed) -> None:
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
            cfg.name = self.query_one("#name", textual.widgets.Input).value.strip()
            cfg.host = self.query_one("#host", textual.widgets.Input).value.strip()
            cfg.port = int_val(self.query_one("#port", textual.widgets.Input).value, 23)
            cfg.ws_path = self.query_one("#ws-path", textual.widgets.Input).value.strip()
            cfg.ssh_username = self.query_one("#ssh-username", textual.widgets.Input).value.strip()
            cfg.ssh_key_file = self.query_one("#ssh-key-file", textual.widgets.Input).value.strip()
            proto_map = {
                "proto-telnet": ("telnet", False),
                "proto-telnets": ("telnet", True),
                "proto-ws": ("websocket", False),
                "proto-wss": ("websocket", True),
                "proto-ssh": ("ssh", False),
            }
            for btn_id, (protocol, ssl) in proto_map.items():
                if self.query_one(f"#{btn_id}", textual.widgets.RadioButton).value:
                    cfg.protocol = protocol
                    cfg.ssl = ssl
                    break
            else:
                cfg.protocol = "telnet"
                cfg.ssl = False
        else:
            cfg.name = DEFAULTS_KEY
            cfg.ssl = False

        cfg.ssl_no_verify = False

        cfg.last_connected = self.config.last_connected
        cfg.bookmarked = self.config.bookmarked

        cfg.term = self.query_one("#term", textual.widgets.Input).value.strip()
        cfg.encoding = self.query_one("#encoding", textual.widgets.Select).value  # type: ignore[assignment]
        encoding_errors = self.query_one("#encoding-errors", textual.widgets.Select).value
        cfg.encoding_errors = encoding_errors  # type: ignore[assignment]

        if self.query_one("#mode-raw", textual.widgets.RadioButton).value:
            cfg.mode = "raw"
        elif self.query_one("#mode-line", textual.widgets.RadioButton).value:
            cfg.mode = "line"
        else:
            cfg.mode = "auto"

        cfg.ansi_keys = self.query_one("#ansi-keys", textual.widgets.Switch).value
        cfg.ascii_eol = self.query_one("#ascii-eol", textual.widgets.Switch).value

        cfg.colormatch = self.query_one("#colormatch", textual.widgets.Select).value  # type: ignore[assignment]
        cfg.color_brightness = self._parse_pct("#color-brightness", 100) / 100.0
        cfg.color_contrast = self._parse_pct("#color-contrast", 100) / 100.0
        cfg.ice_colors = self.query_one("#ice-colors", textual.widgets.Switch).value
        cfg.force_black_bg = self.query_one("#force-black-bg", textual.widgets.Switch).value
        if not cfg.force_black_bg and self.detected_bg is not None:
            r, g, b = self.detected_bg
            cfg.background_color = f"#{r:02x}{g:02x}{b:02x}"
        else:
            cfg.background_color = "#000000"

        timeout_input = self.query_one("#connect-timeout", textual.widgets.Input)
        cfg.connect_timeout = float_val(timeout_input.value, 10.0)
        cfg.coverage = self.query_one("#coverage", textual.widgets.Switch).value

        if self.query_one("#compress-yes", textual.widgets.RadioButton).value:
            cfg.compression = True
        elif self.query_one("#compress-no", textual.widgets.RadioButton).value:
            cfg.compression = False
        else:
            cfg.compression = None

        cfg.send_environ = (
            self.query_one("#send-environ", textual.widgets.Input).value.strip() or "TERM,LANG,COLUMNS,LINES,COLORTERM"
        )
        cfg.always_will = self.config.always_will
        cfg.always_do = self.config.always_do
        cfg.loglevel = self.query_one("#loglevel", textual.widgets.Select).value  # type: ignore[assignment]
        cfg.logfile = self.query_one("#logfile", textual.widgets.Input).value.strip()
        cfg.logfile_mode = (
            "rewrite" if self.query_one("#logfile-mode", textual.widgets.RadioSet).pressed_index == 1 else "append"
        )
        cfg.typescript = self.query_one("#typescript", textual.widgets.Input).value.strip()
        cfg.typescript_mode = (
            "rewrite" if self.query_one("#typescript-mode", textual.widgets.RadioSet).pressed_index == 1 else "append"
        )
        cfg.no_repl = not self.query_one("#use-repl", textual.widgets.Switch).value
        if cfg.mode == "raw":
            cfg.no_repl = True

        radio = self.query_one("#server-type-radio", textual.widgets.RadioSet)
        if radio.pressed_index == 0:
            cfg.server_type = "bbs"
        elif radio.pressed_index == 1:
            cfg.server_type = "mud"
        else:
            cfg.server_type = ""

        return cfg
