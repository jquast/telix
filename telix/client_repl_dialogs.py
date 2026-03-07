"""TUI subprocess management: confirmation dialogs, help screen, editor launchers."""

# std imports
import os
import re
import sys
import json
import shlex
import asyncio
import logging
import tempfile
import subprocess
from typing import TYPE_CHECKING, Any

from .help import get_help
from .paths import CONFIG_DIR as config_dir

# local
from .paths import safe_terminal_size
from .rooms import load_prefs, save_prefs, read_fasttravel
from .rooms import rooms_path as rooms_path_fn
from .rooms import fasttravel_path as fasttravel_path_fn
from .rooms import current_room_path as current_room_path_fn
from .macros import load_macros
from .autoreply import load_autoreplies
from .repl_theme import invalidate_cache as invalidate_theme_cache
from .highlighter import load_highlights
from .progressbars import load_progressbars
from .gmcp_snapshot import save_gmcp_snapshot
from .client_repl_render import make_styles
from .client_repl_travel import fast_travel

if TYPE_CHECKING:
    from .session_context import TelixSessionContext

log = logging.getLogger(__name__)


def _coverage_wrap(cmd: list[str]) -> list[str]:
    """
    Inject ``coverage.process_startup()`` into ``-c`` subprocess commands.

    When ``COVERAGE_PROCESS_START`` is set (by the session manager Coverage
    switch), prepends coverage startup to the ``-c`` code string so that each
    subprocess records its own ``.coverage.*`` data file.
    """
    if not os.environ.get("COVERAGE_PROCESS_START"):
        return cmd
    if len(cmd) >= 3 and cmd[1] == "-c":
        wrapped = cmd.copy()
        wrapped[2] = "import coverage; coverage.process_startup(); " + cmd[2]
        log.debug("coverage_wrap: %s", wrapped)
        return wrapped
    log.debug("coverage_wrap: cmd not wrappable: %s", cmd)
    return cmd


def _prepare_terminal() -> None:
    """Reset the terminal for a subprocess: clear screen and flush buffers."""
    from .client_repl import get_term, terminal_cleanup

    blessed_term = get_term()
    sys.stdout.write(terminal_cleanup())
    sys.stdout.write(blessed_term.change_scroll_region(0, blessed_term.height - 1))  # type: ignore[arg-type]
    sys.stdout.flush()
    sys.stderr.flush()
    sys.__stderr__.flush()


def _make_crash_env() -> tuple[str, dict[str, str]]:
    """Create a crash file and return ``(crash_path, env)``."""
    fd, crash_path = tempfile.mkstemp(suffix=".json", prefix="crash-")
    os.close(fd)
    env = dict(os.environ, TELIX_CRASH_FILE=crash_path)
    return crash_path, env


def _run_subprocess(
    cmd: list[str],
    replay_buf: Any | None = None,
    env: dict[str, str] | None = None,
    crash_path: str = "",
    cleanup_files: list[str] | None = None,
) -> "subprocess.CompletedProcess[bytes] | None":
    """
    Run a TUI subprocess with terminal and editor-active flag management.

    :param cmd: Command list for :func:`subprocess.run`.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    :param env: Optional environment dict; ``None`` inherits the parent env.
    :param crash_path: If non-empty, passed to :func:`handle_crash_file` on return.
    :param cleanup_files: Paths to remove after the subprocess exits.
    :returns: Completed process, or ``None`` if the executable was not found.
    """
    from .client_repl import blocking_fds, restore_after_subprocess

    global subprocess_is_active
    wrapped = _coverage_wrap(cmd)
    log.debug("_run_subprocess: %s", wrapped)
    run_kwargs: dict[str, Any] = {}
    if env is not None:
        run_kwargs["env"] = env
    subprocess_is_active = True
    result: subprocess.CompletedProcess[bytes] | None = None
    try:
        with blocking_fds():
            result = subprocess.run(wrapped, check=False, **run_kwargs)
    except FileNotFoundError:
        log.warning("subprocess not found: %s", cmd[0:3])
    finally:
        subprocess_is_active = False
        if crash_path:
            handle_crash_file(crash_path, cmd, replay_buf, result)
        restore_after_subprocess(replay_buf)
        for path in cleanup_files or ():
            try:
                os.unlink(path)
            except OSError:
                pass
    return result


# Causes MUD output to buffer while in dialog.  The asyncio read_server
# loop continues receiving MUD data during subprocess sessions; writing
# that data to the terminal fills the PTY buffer and deadlocks the
# subprocess's Textual WriterThread.  Data is queued here and replayed
# when the subprocess exits.
subprocess_is_active = False
subprocess_buffer: list[bytes] = []


def get_logfile_path() -> str:
    """Return the path of the first FileHandler on the root logger, or ``""``."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename:
            return handler.baseFilename
    return ""


def confirm_dialog(title: str, body: str, warning: str = "", replay_buf: Any | None = None) -> bool:
    """
    Show a Textual confirmation dialog in a subprocess.

    Launches :func:`telix.client_tui.confirm_dialog_main` as a
    subprocess, reads the result from a temporary file, and restores
    terminal state on return.

    :param title: Dialog title.
    :param body: Body text.
    :param warning: Optional warning text displayed in red.
    :param replay_buf: Optional replay buffer for screen repaint.
    :returns: Whether the user confirmed.
    """
    fd, result_path = tempfile.mkstemp(suffix=".json", prefix="confirm-")
    os.close(fd)

    logfile = get_logfile_path()
    cmd = [
        sys.executable,
        "-c",
        "import sys; from telix.client_tui_dialogs import confirm_dialog_main; "
        "confirm_dialog_main(sys.argv[1], sys.argv[2],"
        " warning=sys.argv[3], result_file=sys.argv[4],"
        " logfile=sys.argv[5])",
        title,
        body,
        warning or "",
        result_path,
        logfile,
    ]

    log.debug(
        "confirm_dialog: pre-subprocess fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s stderr_isatty=%s "
        "TERM=%s COLORTERM=%s terminal_size=%s",
        os.get_blocking(0),
        os.get_blocking(1),
        os.get_blocking(2),
        sys.stdin.isatty(),
        sys.__stderr__.isatty(),
        os.environ.get("TERM", ""),
        os.environ.get("COLORTERM", ""),
        safe_terminal_size(),
    )
    _prepare_terminal()
    _run_subprocess(cmd, replay_buf=replay_buf)

    confirmed = False
    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
        confirmed = bool(data.get("confirmed", False))
    except (OSError, ValueError):
        pass
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass

    return confirmed


def randomwalk_dialog(replay_buf: Any | None = None, session_key: str = "") -> str | None:
    """
    Show the random walk dialog with visit-level parameter.

    Loads saved preferences from *session_key* (if provided) as defaults,
    and saves the user's choices back on confirmation.

    :param replay_buf: Optional replay buffer for screen repaint.
    :param session_key: Session key for loading/saving preferences.
    :returns: Command string (e.g. ``"`randomwalk 2 autosearch`"``) on
        confirm, or ``None`` on cancel.
    """
    default_visit_level = 2
    default_auto_search = False
    default_auto_evaluate = False
    default_auto_survey = False
    default_autoreplies = True
    if session_key:
        prefs = load_prefs(session_key)
        default_visit_level = int(prefs.get("randomwalk_visit_level", 2))
        default_auto_search = bool(prefs.get("randomwalk_auto_search", False))
        default_auto_evaluate = bool(prefs.get("randomwalk_auto_evaluate", False))
        default_auto_survey = bool(prefs.get("randomwalk_auto_survey", False))
        default_autoreplies = bool(prefs.get("randomwalk_autoreplies", True))

    fd, result_path = tempfile.mkstemp(suffix=".json", prefix="randomwalk-")
    os.close(fd)

    logfile = get_logfile_path()
    cmd = [
        sys.executable,
        "-c",
        "import sys; from telix.client_tui_dialogs import randomwalk_dialog_main; "
        "randomwalk_dialog_main(result_file=sys.argv[1],"
        " default_visit_level=sys.argv[2],"
        " default_auto_search=sys.argv[3],"
        " default_auto_evaluate=sys.argv[4],"
        " default_auto_survey=sys.argv[5],"
        " default_autoreplies=sys.argv[6],"
        " logfile=sys.argv[7])",
        result_path,
        str(default_visit_level),
        "1" if default_auto_search else "0",
        "1" if default_auto_evaluate else "0",
        "1" if default_auto_survey else "0",
        "1" if default_autoreplies else "0",
        logfile,
    ]

    log.debug("randomwalk_dialog: launching subprocess")
    _prepare_terminal()
    _run_subprocess(cmd, replay_buf=replay_buf)

    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("confirmed", False):
            return None
        if session_key:
            save_data = load_prefs(session_key)
            save_data["randomwalk_visit_level"] = int(data.get("visit_level", default_visit_level))  # type: ignore[assignment]
            save_data["randomwalk_auto_search"] = bool(data.get("auto_search", default_auto_search))
            save_data["randomwalk_auto_evaluate"] = bool(data.get("auto_evaluate", default_auto_evaluate))
            save_data["randomwalk_auto_survey"] = bool(data.get("auto_survey", default_auto_survey))
            save_data["randomwalk_autoreplies"] = bool(data.get("autoreplies", default_autoreplies))
            save_prefs(session_key, save_data)
        return str(data.get("command", f"`randomwalk 999 {default_visit_level}`"))
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass


def autodiscover_dialog(replay_buf: Any | None = None, session_key: str = "") -> str | None:
    """
    Show the autodiscover dialog with BFS/DFS strategy selection.

    Loads saved strategy preference from *session_key* (if provided) as
    default, and saves the user's choice back on confirmation.

    :param replay_buf: Optional replay buffer for screen repaint.
    :param session_key: Session key for loading/saving preferences.
    :returns: Command string (e.g. ``"`autodiscover bfs`"``) on
        confirm, or ``None`` on cancel.
    """
    default_strategy = "bfs"
    default_auto_search = False
    default_auto_evaluate = False
    default_auto_survey = False
    default_autoreplies = True
    if session_key:
        prefs = load_prefs(session_key)
        saved = prefs.get("autodiscover_strategy", "bfs")
        if saved in ("bfs", "dfs"):
            default_strategy = str(saved)
        default_auto_search = bool(prefs.get("autodiscover_auto_search", False))
        default_auto_evaluate = bool(prefs.get("autodiscover_auto_evaluate", False))
        default_auto_survey = bool(prefs.get("autodiscover_auto_survey", False))
        default_autoreplies = bool(prefs.get("autodiscover_autoreplies", True))

    fd, result_path = tempfile.mkstemp(suffix=".json", prefix="autodiscover-")
    os.close(fd)

    logfile = get_logfile_path()
    cmd = [
        sys.executable,
        "-c",
        "import sys; from telix.client_tui_dialogs import autodiscover_dialog_main; "
        "autodiscover_dialog_main(result_file=sys.argv[1],"
        " default_strategy=sys.argv[2],"
        " default_auto_search=sys.argv[3],"
        " default_auto_evaluate=sys.argv[4],"
        " default_auto_survey=sys.argv[5],"
        " default_autoreplies=sys.argv[6],"
        " logfile=sys.argv[7])",
        result_path,
        default_strategy,
        "1" if default_auto_search else "0",
        "1" if default_auto_evaluate else "0",
        "1" if default_auto_survey else "0",
        "1" if default_autoreplies else "0",
        logfile,
    ]

    log.debug("autodiscover_dialog: launching subprocess")
    _prepare_terminal()
    _run_subprocess(cmd, replay_buf=replay_buf)

    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("confirmed", False):
            return None
        if session_key:
            save_data = load_prefs(session_key)
            save_data["autodiscover_strategy"] = str(data.get("strategy", default_strategy))
            save_data["autodiscover_auto_search"] = bool(data.get("auto_search", default_auto_search))
            save_data["autodiscover_auto_evaluate"] = bool(data.get("auto_evaluate", default_auto_evaluate))
            save_data["autodiscover_auto_survey"] = bool(data.get("auto_survey", default_auto_survey))
            save_data["autodiscover_autoreplies"] = bool(data.get("autoreplies", default_autoreplies))
            save_prefs(session_key, save_data)
        return str(data.get("command", f"`autodiscover {default_strategy}`"))
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass


def strip_md(text: str) -> str:
    """Strip markdown bold/code markers from text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    return text.strip()


def render_help_md(has_gmcp: bool = False) -> list[str]:
    """
    Render keybindings help markdown into plain-text lines.

    :param has_gmcp: Whether GMCP room data is available.
    :rtype: list[str]
    """
    md = get_help("keybindings")
    lines: list[str] = []
    in_header_row = False
    skip_section = False
    for raw in md.splitlines():
        stripped = raw.strip()
        if stripped.startswith("##"):
            heading = stripped.lstrip("# ").strip()
            if not has_gmcp and "GMCP" in heading:
                skip_section = True
                continue
            skip_section = False
            lines.append("")
            lines.append("  " + heading)
            lines.append("")
            in_header_row = True
        elif skip_section or (stripped.startswith("|") and "---" in stripped):
            continue
        elif stripped.startswith("|"):
            cells = [strip_md(c) for c in stripped.split("|")[1:-1]]
            if in_header_row:
                in_header_row = False
                continue
            if len(cells) >= 2 and cells[0]:
                lines.append(f"  {cells[0]:<16}{cells[1]}")
        elif stripped and not stripped.startswith("|"):
            lines.append("  " + strip_md(stripped))
        elif lines and lines[-1] != "":
            lines.append("")
    return lines


def show_help(macro_defs: "Any" = None, replay_buf: Any | None = None, has_gmcp: bool = False) -> None:
    """
    Launch the keybindings help viewer as a Textual TUI subprocess.

    :param macro_defs: Unused (kept for API compatibility).
    :param replay_buf: Optional replay buffer for screen repaint on return.
    :param has_gmcp: Unused (kept for API compatibility).
    """
    logfile = get_logfile_path()
    cmd = [
        sys.executable,
        "-c",
        "import sys; from telix.client_tui_editors import show_help_main; "
        "show_help_main(topic=sys.argv[1], logfile=sys.argv[2])",
        "keybindings",
        logfile,
    ]

    crash_path, env = _make_crash_env()
    _prepare_terminal()
    _run_subprocess(cmd, replay_buf=replay_buf, env=env, crash_path=crash_path)


def read_crash_file(crash_path: str) -> dict[str, Any] | None:
    """
    Read crash data JSON from *crash_path*.

    The caller is responsible for deciding whether to delete the file.

    :param crash_path: Path to the crash JSON file.
    :returns: Parsed crash data dict, or ``None`` on missing/invalid file.
    """
    try:
        with open(crash_path, encoding="utf-8") as fh:
            return dict(json.load(fh))
    except (OSError, ValueError):
        return None


def format_crash_banner(crash_data: dict[str, Any], cmd: list[str], crash_path: str, exit_code: int) -> str:
    r"""
    Format crash data as a display banner with ``\r\n`` line endings.

    :param crash_data: Dict with ``traceback``, ``pid``, ``source`` keys.
    :param cmd: The subprocess command list.
    :param crash_path: Path to the crash file (shown in the closing line).
    :param exit_code: Subprocess exit code.
    :returns: Formatted banner string.
    """
    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 80
    tb_text = crash_data.get("traceback", "")
    quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
    cmd_footer = f" end of failed command (exit code={exit_code}) "
    tb_footer = f" End of captured traceback: {crash_path} "
    lines = ["", quoted_cmd, cmd_footer.center(width, "-"), ""]
    for line in tb_text.splitlines():
        lines.append(line)
    lines.append("")
    lines.append(tb_footer.center(width, "-"))
    lines.append("")
    return "\r\n".join(lines)


def handle_crash_file(crash_path: str, cmd: list[str], replay_buf: Any | None, result: Any | None) -> None:
    """
    Read crash file and inject a formatted banner into *replay_buf*.

    On success (``returncode == 0``) or missing result, clean up the
    file.  On failure the file is preserved for post-mortem inspection.

    :param crash_path: Path to the crash JSON file.
    :param cmd: The subprocess command list.
    :param replay_buf: Replay buffer list to append banner bytes to.
    :param result: ``subprocess.CompletedProcess`` or ``None``.
    """
    if result is None or result.returncode == 0:
        try:
            os.unlink(crash_path)
        except OSError:
            pass
        return
    data = read_crash_file(crash_path)
    if data:
        log.error("TUI subprocess (pid %s) crashed", data.get("pid", "?"))
        for line in data.get("traceback", "").splitlines():
            log.error("%s", line)
        if replay_buf is not None:
            banner = format_crash_banner(data, cmd, crash_path, result.returncode)
            replay_buf.append(banner.encode("utf-8"))


def most_recent_channel(chat_messages: list[Any], capture_log: dict[str, Any]) -> str:
    """Return the channel with the most recent activity across chat and captures."""
    best_ts = ""
    best_ch = ""
    if chat_messages:
        last = chat_messages[-1]
        best_ts = last.get("ts", "")
        best_ch = last.get("channel", "")
    for ch, entries in capture_log.items():
        if entries:
            ts = entries[-1].get("ts", "")
            if ts > best_ts:
                best_ts = ts
                best_ch = ch
    return best_ch


def launch_unified_editor(initial_tab: str, ctx: "TelixSessionContext", replay_buf: Any | None = None) -> None:
    """
    Launch the unified tabbed TUI editor as a subprocess.

    Gathers parameters for all panes, spawns ``unified_editor_main``, and
    reloads all potentially-modified configs on return.

    :param initial_tab: Tab to open initially (e.g. ``"help"``, ``"macros"``).
    :param ctx: Session context with file path and definition attributes.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    """
    session_key = ctx.session_key
    logfile = get_logfile_path()

    # -- Gather all pane parameters --
    highlights_file = ctx.highlights_file or os.path.join(config_dir, "highlights.json")
    macros_file = ctx.macros_file or os.path.join(config_dir, "macros.json")
    autoreplies_file = ctx.autoreplies_file or os.path.join(config_dir, "autoreplies.json")
    progressbars_file = ctx.progressbars_file or os.path.join(config_dir, "progressbars.json")
    rooms_file = ctx.rooms_file or rooms_path_fn(session_key)
    current_room_file = ctx.current_room_file or current_room_path_fn(session_key)
    fasttravel_file = fasttravel_path_fn(session_key)

    # Flush GMCP snapshot so the bars editor can read it.
    gmcp_snapshot_file = ctx.gmcp_snapshot_file or ""
    if gmcp_snapshot_file and ctx.gmcp_data:
        save_gmcp_snapshot(gmcp_snapshot_file, session_key, ctx.gmcp_data)
        ctx.gmcp_dirty = False

    # Autoreply select pattern.
    engine = ctx.autoreply_engine
    select_pattern = getattr(engine, "last_matched_pattern", "") if engine else ""

    # Chat / capture data.
    chat_file = ctx.chat_file or ""
    ctx.chat_unread = 0
    captures = getattr(ctx, "captures", {})
    capture_log = getattr(ctx, "capture_log", {})
    initial_channel = most_recent_channel(ctx.chat_messages, capture_log)

    capture_file = ""
    if captures or capture_log:
        fd, capture_file = tempfile.mkstemp(suffix=".json", prefix="captures-")
        os.close(fd)
        with open(capture_file, "w", encoding="utf-8") as fh:
            json.dump({"captures": captures, "capture_log": capture_log}, fh)

    params = {
        "initial_tab": initial_tab,
        "session_key": session_key,
        "logfile": logfile,
        "highlights_file": highlights_file,
        "macros_file": macros_file,
        "autoreplies_file": autoreplies_file,
        "progressbars_file": progressbars_file,
        "rooms_file": rooms_file,
        "current_room_file": current_room_file,
        "fasttravel_file": fasttravel_file,
        "gmcp_snapshot_file": gmcp_snapshot_file,
        "select_pattern": select_pattern,
        "chat_file": chat_file,
        "initial_channel": initial_channel,
        "capture_file": capture_file,
    }

    params_json = json.dumps(params)
    cmd = [
        sys.executable,
        "-c",
        "import sys; from telix.client_tui_dialogs import unified_editor_main; unified_editor_main()",
        params_json,
    ]

    crash_path, env = _make_crash_env()

    log.debug(
        "unified_editor: pre-subprocess initial_tab=%s TERM=%s COLORTERM=%s terminal_size=%s",
        initial_tab,
        os.environ.get("TERM", ""),
        os.environ.get("COLORTERM", ""),
        safe_terminal_size(),
    )
    _prepare_terminal()
    _run_subprocess(
        cmd,
        replay_buf=replay_buf,
        env=env,
        crash_path=crash_path,
        cleanup_files=[capture_file] if capture_file else None,
    )

    # Reload all configs that may have been modified.
    reload_macros(ctx, macros_file, session_key, log)
    reload_highlights(ctx, highlights_file, session_key, log)
    reload_autoreplies(ctx, autoreplies_file, session_key, log)
    reload_progressbars(ctx, progressbars_file, session_key, log)

    # Rebuild REPL color styles in case the theme was changed.
    invalidate_theme_cache()
    make_styles()

    # Reload room graph.
    room_graph = ctx.room_graph
    if room_graph is not None:
        room_graph.load_adjacency()

    # Handle fast travel.
    steps, noreply = read_fasttravel(fasttravel_file)
    if steps:
        log.debug("travel: scheduling %d steps (noreply=%s)", len(steps), noreply)
        task = asyncio.ensure_future(fast_travel(steps, ctx, log, noreply=noreply))
        ctx.travel_task = task

        def on_done(t: "asyncio.Task[None]") -> None:
            if ctx.travel_task is t:
                ctx.travel_task = None
            if not t.cancelled() and t.exception() is not None:
                log.warning("fast travel failed: %s", t.exception())

        task.add_done_callback(on_done)


def launch_tui_editor(editor_type: str, ctx: "TelixSessionContext", replay_buf: Any | None = None) -> None:
    """
    Launch a TUI editor for macros or autoreplies in a subprocess.

    :param editor_type: ``"macros"``, ``"autoreplies"``, or ``"highlights"``.
    :param ctx: Session context with file path and definition attributes.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    """
    session_key = ctx.session_key

    logfile = get_logfile_path()

    if editor_type == "macros":
        path = ctx.macros_file or os.path.join(config_dir, "macros.json")
        rp = ctx.rooms_file or rooms_path_fn(session_key)
        crp = ctx.current_room_file or current_room_path_fn(session_key)
        cmd = [
            sys.executable,
            "-c",
            "import sys; from telix.client_tui_editors import edit_macros_main; "
            "edit_macros_main(sys.argv[1], sys.argv[2],"
            " rooms_file=sys.argv[3], current_room_file=sys.argv[4],"
            " logfile=sys.argv[5])",
            path,
            session_key,
            rp,
            crp,
            logfile,
        ]
    elif editor_type == "highlights":
        path = ctx.highlights_file or os.path.join(config_dir, "highlights.json")
        cmd = [
            sys.executable,
            "-c",
            "import sys; from telix.client_tui_editors import edit_highlights_main; "
            "edit_highlights_main(sys.argv[1], sys.argv[2], logfile=sys.argv[3])",
            path,
            session_key,
            logfile,
        ]
    elif editor_type == "progressbars":
        path = ctx.progressbars_file or os.path.join(config_dir, "progressbars.json")
        snap = ctx.gmcp_snapshot_file or ""
        # Flush GMCP snapshot immediately so the editor subprocess can read it.
        if snap and ctx.gmcp_data:
            save_gmcp_snapshot(snap, session_key, ctx.gmcp_data)
            ctx.gmcp_dirty = False
        cmd = [
            sys.executable,
            "-c",
            "import sys; from telix.client_tui_editors import edit_progressbars_main; "
            "edit_progressbars_main(sys.argv[1], sys.argv[2],"
            " gmcp_snapshot_path=sys.argv[3], logfile=sys.argv[4])",
            path,
            session_key,
            snap,
            logfile,
        ]
    else:
        path = ctx.autoreplies_file or os.path.join(config_dir, "autoreplies.json")
        snap = ctx.gmcp_snapshot_file or ""
        if snap and ctx.gmcp_data:
            save_gmcp_snapshot(snap, session_key, ctx.gmcp_data)
            ctx.gmcp_dirty = False
        engine = ctx.autoreply_engine
        select = getattr(engine, "last_matched_pattern", "") if engine else ""
        cmd = [
            sys.executable,
            "-c",
            "import sys; from telix.client_tui_editors import edit_autoreplies_main; "
            "edit_autoreplies_main(sys.argv[1], sys.argv[2],"
            " select_pattern=sys.argv[3], gmcp_snapshot_path=sys.argv[4], logfile=sys.argv[5])",
            path,
            session_key,
            select,
            snap,
            logfile,
        ]

    crash_path, env = _make_crash_env()

    log.debug(
        "tui_editor: pre-subprocess fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s stderr_isatty=%s editor_type=%s "
        "TERM=%s COLORTERM=%s terminal_size=%s",
        os.get_blocking(0),
        os.get_blocking(1),
        os.get_blocking(2),
        sys.stdin.isatty(),
        sys.__stderr__.isatty(),
        editor_type,
        os.environ.get("TERM", ""),
        os.environ.get("COLORTERM", ""),
        safe_terminal_size(),
    )
    _prepare_terminal()
    _run_subprocess(cmd, replay_buf=replay_buf, env=env, crash_path=crash_path)

    if editor_type == "macros":
        reload_macros(ctx, path, session_key, log)
    elif editor_type == "highlights":
        reload_highlights(ctx, path, session_key, log)
    elif editor_type == "progressbars":
        reload_progressbars(ctx, path, session_key, log)
    else:
        reload_autoreplies(ctx, path, session_key, log)


def reload_macros(ctx: "TelixSessionContext", path: str, session_key: str, log: logging.Logger) -> None:
    """Reload macro definitions from disk and update dispatch."""
    if not os.path.exists(path):
        return
    try:
        new_defs = load_macros(path, session_key)
        ctx.macro_defs = new_defs
        ctx.macros_file = path
        dispatch = ctx.key_dispatch
        if dispatch is not None:
            dispatch.set_macros(new_defs, ctx, log)
        log.info("reloaded %d macros from %s", len(new_defs), path)
    except ValueError as exc:
        log.warning("failed to reload macros: %s", exc)


def reload_autoreplies(ctx: "TelixSessionContext", path: str, session_key: str, log: logging.Logger) -> None:
    """Reload autoreply rules from disk after editing."""
    if not os.path.exists(path):
        return
    try:
        ctx.autoreply_rules = load_autoreplies(path, session_key)
        ctx.autoreplies_file = path
        n_rules = len(ctx.autoreply_rules)
        log.info("reloaded %d autoreplies from %s", n_rules, path)
    except ValueError as exc:
        log.warning("failed to reload autoreplies: %s", exc)


def reload_progressbars(ctx: "TelixSessionContext", path: str, session_key: str, log: logging.Logger) -> None:
    """Reload progress bar configs from disk after editing."""
    if not os.path.exists(path):
        return
    try:
        ctx.progressbar_configs = load_progressbars(path, session_key)
        ctx.progressbars_file = path
        n_bars = len(ctx.progressbar_configs)
        log.info("reloaded %d progress bars from %s", n_bars, path)
    except (ValueError, FileNotFoundError):
        log.warning("failed to reload progress bars from %s", path)


def reload_highlights(ctx: "TelixSessionContext", path: str, session_key: str, log: logging.Logger) -> None:
    """Reload highlight rules from disk after editing."""
    if not os.path.exists(path):
        return
    try:
        ctx.highlight_rules = load_highlights(path, session_key)
        ctx.highlights_file = path
        n_rules = len(ctx.highlight_rules)
        log.info("reloaded %d highlights from %s", n_rules, path)
    except ValueError as exc:
        log.warning("failed to reload highlights: %s", exc)


def launch_chat_viewer(ctx: "TelixSessionContext", replay_buf: Any | None = None) -> None:
    """
    Launch the Capture Window TUI in a subprocess.

    Writes capture data (``ctx.captures`` and ``ctx.capture_log``) to a
    temporary JSON file and passes its path to the subprocess.

    :param ctx: Session context with chat and capture state.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    """
    session_key = ctx.session_key
    if not session_key:
        return

    chat_file = ctx.chat_file or ""

    ctx.chat_unread = 0

    # Write capture data to a temporary file for the subprocess.
    capture_file = ""
    captures = getattr(ctx, "captures", {})
    capture_log = getattr(ctx, "capture_log", {})
    initial_channel = most_recent_channel(ctx.chat_messages, capture_log)
    if captures or capture_log:
        fd, capture_file = tempfile.mkstemp(suffix=".json", prefix="captures-")
        os.close(fd)
        with open(capture_file, "w", encoding="utf-8") as fh:
            json.dump({"captures": captures, "capture_log": capture_log}, fh)

    logfile = get_logfile_path()
    cmd = [
        sys.executable,
        "-c",
        "import sys; from telix.client_tui_dialogs import chat_viewer_main; "
        "chat_viewer_main(sys.argv[1], sys.argv[2],"
        " initial_channel=sys.argv[3], logfile=sys.argv[4],"
        " capture_file=sys.argv[5])",
        chat_file,
        session_key,
        initial_channel,
        logfile,
        capture_file,
    ]

    crash_path, env = _make_crash_env()

    log.debug("chat_viewer: launching subprocess")
    _prepare_terminal()
    _run_subprocess(
        cmd,
        replay_buf=replay_buf,
        env=env,
        crash_path=crash_path,
        cleanup_files=[capture_file] if capture_file else None,
    )


def launch_room_browser(ctx: "TelixSessionContext", replay_buf: Any | None = None) -> None:
    """
    Launch the room browser TUI in a subprocess.

    On return, check for a fast travel file and queue movement commands.

    :param ctx: Session context with session attributes.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    """
    session_key = ctx.session_key
    if not session_key:
        return

    rp = ctx.rooms_file or rooms_path_fn(session_key)
    crp = ctx.current_room_file or current_room_path_fn(session_key)
    ftp = fasttravel_path_fn(session_key)

    logfile = get_logfile_path()
    cmd = [
        sys.executable,
        "-c",
        "import sys; from telix.client_tui_dialogs import edit_rooms_main; "
        "edit_rooms_main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4],"
        " logfile=sys.argv[5])",
        rp,
        session_key,
        crp,
        ftp,
        logfile,
    ]

    crash_path, env = _make_crash_env()

    log.debug(
        "room_browser: pre-subprocess fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s stderr_isatty=%s "
        "TERM=%s COLORTERM=%s terminal_size=%s",
        os.get_blocking(0),
        os.get_blocking(1),
        os.get_blocking(2),
        sys.stdin.isatty(),
        sys.__stderr__.isatty(),
        os.environ.get("TERM", ""),
        os.environ.get("COLORTERM", ""),
        safe_terminal_size(),
    )
    _prepare_terminal()
    _run_subprocess(cmd, replay_buf=replay_buf, env=env, crash_path=crash_path)

    room_graph = ctx.room_graph
    if room_graph is not None:
        room_graph.load_adjacency()

    steps, noreply = read_fasttravel(ftp)
    if steps:
        log.debug("travel: scheduling %d steps (noreply=%s)", len(steps), noreply)
        task = asyncio.ensure_future(fast_travel(steps, ctx, log, noreply=noreply))
        ctx.travel_task = task

        def on_done(t: "asyncio.Task[None]") -> None:
            if ctx.travel_task is t:
                ctx.travel_task = None
            if not t.cancelled() and t.exception() is not None:
                log.warning("fast travel failed: %s", t.exception())

        task.add_done_callback(on_done)
