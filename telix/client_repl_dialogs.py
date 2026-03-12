"""TUI thread-based management: confirmation dialogs, help screen, editor launchers."""

# std imports
import os
import re
import sys
import json
import signal
import typing
import asyncio
import logging
import tempfile
import threading
import contextlib
from typing import TYPE_CHECKING
from collections.abc import Callable, Iterator

# local
from . import help as help_mod
from . import (
    paths,
    rooms,
    macros,
    trigger,
    repl_theme,
    highlighter,
    progressbars,
    gmcp_snapshot,
    client_repl_render,
    client_repl_travel,
)

if TYPE_CHECKING:
    from .session_context import TelixSessionContext

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _patch_signal_for_thread() -> Iterator[None]:
    """
    Forward ``signal.signal()`` calls from the TUI worker thread to the main thread.

    Textual's ``LinuxDriver`` calls ``signal.signal(SIGWINCH/SIGTSTP/SIGCONT, ...)``
    during startup.  ``signal.signal()`` raises ``ValueError`` when called from any
    thread other than the main thread, so those registrations are intercepted here.

    For SIGWINCH specifically, the worker thread's handler is captured and installed in
    the main thread via a forwarding wrapper so that terminal resize events still work.
    The SIGWINCH handler Textual installs uses ``loop.call_soon_threadsafe()`` internally,
    so calling it from the main thread correctly posts the resize event to the worker
    thread's asyncio event loop.

    The main thread is blocked in ``t.join()`` for the duration of the TUI, so replacing
    the module-level ``signal.signal`` reference here is race-free.

    On Windows, SIGWINCH does not exist so only the ``safe_signal`` interceptor is
    installed; the SIGWINCH forwarding is skipped.
    """
    original = signal.signal
    tui_handlers: dict[int, typing.Any] = {}

    def safe_signal(signum: int, handler: typing.Any) -> typing.Any:
        if threading.current_thread() is threading.main_thread():
            return original(signum, handler)
        tui_handlers[signum] = handler
        return None

    has_sigwinch = hasattr(signal, "SIGWINCH")

    def forward_sigwinch(sig: int, frame: typing.Any) -> None:
        h = tui_handlers.get(signal.SIGWINCH)
        if callable(h):
            h(sig, frame)

    signal.signal = safe_signal  # type: ignore[assignment]
    old_winch = original(signal.SIGWINCH, forward_sigwinch) if has_sigwinch else None
    try:
        yield
    finally:
        signal.signal = original
        if has_sigwinch:
            original(signal.SIGWINCH, old_winch)


def _prepare_terminal() -> None:
    """Reset the terminal for a TUI: clear screen and flush buffers."""
    from .client_repl import get_term, terminal_cleanup

    blessed_term = get_term()
    sys.stdout.write(terminal_cleanup())
    sys.stdout.write(blessed_term.change_scroll_region(0, blessed_term.height - 1))  # type: ignore[arg-type]
    sys.stdout.flush()
    sys.stderr.flush()
    sys.__stderr__.flush()


def _run_in_thread(
    target: Callable[[], None], replay_buf: typing.Any | None = None, cleanup_files: list[str] | None = None
) -> None:
    """
    Run a TUI callable in a worker thread with terminal and editor-active flag management.

    The worker thread has no existing asyncio event loop so Textual's ``app.run()``
    (which calls :func:`asyncio.run` internally) is legal.  FD blocking is managed by
    the :func:`~telix.terminal_unix.blocking_fds` context manager in the calling thread;
    the worker inherits the blocking state.  Unhandled exceptions propagate via Python's
    default thread exception handler.

    :param target: Callable that creates and runs a Textual :class:`~textual.app.App`.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    :param cleanup_files: Paths to remove after the thread exits.
    """
    from .client_repl import blocking_fds, restore_after_subprocess

    global subprocess_is_active
    log.debug("_run_in_thread: starting worker thread")
    subprocess_is_active = True
    thread_exc: BaseException | None = None

    def wrapped_target() -> None:
        nonlocal thread_exc
        try:
            target()
        except BaseException as exc:
            thread_exc = exc
            raise

    try:
        with _patch_signal_for_thread(), blocking_fds():
            t = threading.Thread(target=wrapped_target, daemon=True)
            t.start()
            t.join()
    finally:
        subprocess_is_active = False
        if thread_exc is not None:
            log.error("TUI thread failed", exc_info=thread_exc)
        restore_after_subprocess(replay_buf)
        for path in cleanup_files or ():
            try:
                os.unlink(path)
            except OSError:
                pass


# Causes MUD output to buffer while in dialog.  The asyncio read_server
# loop continues receiving MUD data during subprocess sessions; writing
# that data to the terminal fills the PTY buffer and deadlocks the
# subprocess's Textual WriterThread.  Data is queued here and replayed
# when the subprocess exits.
subprocess_is_active = False
subprocess_buffer: list[bytes] = []


def confirm_dialog(title: str, body: str, warning: str = "", replay_buf: typing.Any | None = None) -> bool:
    """
    Show a Textual confirmation dialog in a worker thread.

    Runs :func:`~telix.client_tui_dialogs.run_confirm_dialog` in a worker thread,
    reads the result from a temporary file, and restores terminal state on return.

    :param title: Dialog title.
    :param body: Body text.
    :param warning: Optional warning text displayed in red.
    :param replay_buf: Optional replay buffer for screen repaint.
    :returns: Whether the user confirmed.
    """
    from . import client_tui_dialogs

    fd, result_path = tempfile.mkstemp(suffix=".json", prefix="confirm-")
    os.close(fd)

    log.debug(
        "confirm_dialog: pre-thread fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s stderr_isatty=%s "
        "TERM=%s COLORTERM=%s terminal_size=%s",
        getattr(os, "get_blocking", lambda fd: None)(0),
        getattr(os, "get_blocking", lambda fd: None)(1),
        getattr(os, "get_blocking", lambda fd: None)(2),
        sys.stdin.isatty(),
        sys.__stderr__.isatty(),
        os.environ.get("TERM", ""),
        os.environ.get("COLORTERM", ""),
        paths.safe_terminal_size(),
    )
    _prepare_terminal()
    _run_in_thread(
        lambda: client_tui_dialogs.run_confirm_dialog(title, body, warning or "", result_path), replay_buf=replay_buf
    )

    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass

    return bool(data.get("confirmed", False))


def randomwalk_dialog(replay_buf: typing.Any | None = None, session_key: str = "") -> str | None:
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
    default_room_change_cmd = ""
    default_triggers = True
    if session_key:
        prefs = rooms.load_prefs(session_key)
        default_visit_level = int(prefs.get("randomwalk_visit_level", 2))
        default_room_change_cmd = str(prefs.get("randomwalk_room_change_cmd", ""))
        default_triggers = bool(prefs.get("randomwalk_triggers", True))

    from . import client_tui_dialogs

    fd, result_path = tempfile.mkstemp(suffix=".json", prefix="randomwalk-")
    os.close(fd)

    log.debug("randomwalk_dialog: launching thread")
    _prepare_terminal()
    _run_in_thread(
        lambda: client_tui_dialogs.run_randomwalk_dialog(
            result_path, default_visit_level, default_room_change_cmd, default_triggers
        ),
        replay_buf=replay_buf,
    )

    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("confirmed", False):
            return None
        if session_key:
            save_data = rooms.load_prefs(session_key)
            save_data["randomwalk_visit_level"] = int(  # type: ignore[assignment]
                data.get("visit_level", default_visit_level)
            )
            save_data["randomwalk_room_change_cmd"] = str(data.get("room_change_cmd", default_room_change_cmd))
            save_data["randomwalk_triggers"] = bool(data.get("triggers", default_triggers))
            rooms.save_prefs(session_key, save_data)
        return str(data.get("command", f"`randomwalk 999 {default_visit_level}`"))
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass


def autodiscover_dialog(replay_buf: typing.Any | None = None, session_key: str = "") -> str | None:
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
    default_room_change_cmd = ""
    default_triggers = True
    if session_key:
        prefs = rooms.load_prefs(session_key)
        saved = prefs.get("autodiscover_strategy", "bfs")
        if saved in ("bfs", "dfs"):
            default_strategy = str(saved)
        default_room_change_cmd = str(prefs.get("autodiscover_room_change_cmd", ""))
        default_triggers = bool(prefs.get("autodiscover_triggers", True))

    from . import client_tui_dialogs

    fd, result_path = tempfile.mkstemp(suffix=".json", prefix="autodiscover-")
    os.close(fd)

    log.debug("autodiscover_dialog: launching thread")
    _prepare_terminal()
    _run_in_thread(
        lambda: client_tui_dialogs.run_autodiscover_dialog(
            result_path, default_strategy, default_room_change_cmd, default_triggers
        ),
        replay_buf=replay_buf,
    )

    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("confirmed", False):
            return None
        if session_key:
            save_data = rooms.load_prefs(session_key)
            save_data["autodiscover_strategy"] = str(data.get("strategy", default_strategy))
            save_data["autodiscover_room_change_cmd"] = str(data.get("room_change_cmd", default_room_change_cmd))
            save_data["autodiscover_triggers"] = bool(data.get("triggers", default_triggers))
            rooms.save_prefs(session_key, save_data)
        return str(data.get("command", f"`autodiscover {default_strategy}`"))
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
    md = help_mod.get_help("keybindings")
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


def show_help(macro_defs: "typing.Any" = None, replay_buf: typing.Any | None = None, has_gmcp: bool = False) -> None:
    """
    Launch the keybindings help viewer as a Textual TUI in a worker thread.

    :param macro_defs: Unused (kept for API compatibility).
    :param replay_buf: Optional replay buffer for screen repaint on return.
    :param has_gmcp: Unused (kept for API compatibility).
    """
    from . import client_tui_base

    _prepare_terminal()
    _run_in_thread(
        lambda: client_tui_base.launch_editor_in_thread(client_tui_base.CommandHelpScreen(topic="keybindings")),
        replay_buf=replay_buf,
    )


def most_recent_channel(chat_messages: list[typing.Any], capture_log: dict[str, typing.Any]) -> str:
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


def launch_unified_editor(initial_tab: str, ctx: "TelixSessionContext", replay_buf: typing.Any | None = None) -> None:
    """
    Launch the unified tabbed TUI editor in a worker thread.

    Gathers parameters for all panes, runs :func:`~telix.client_tui_dialogs.run_unified_editor`
    in a worker thread, and reloads all potentially-modified configs on return.

    :param initial_tab: Tab to open initially (e.g. ``"help"``, ``"macros"``).
    :param ctx: Session context with file path and definition attributes.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    """
    from . import client_tui_dialogs

    session_key = ctx.session_key

    # -- Gather all pane parameters --
    highlights_file = ctx.highlights.file or os.path.join(paths.CONFIG_DIR, "highlights.json")
    macros_file = ctx.macros.file or os.path.join(paths.CONFIG_DIR, "macros.json")
    triggers_file = ctx.triggers.file or os.path.join(paths.CONFIG_DIR, "triggers.json")
    progressbars_file = ctx.progress.file or os.path.join(paths.CONFIG_DIR, "progressbars.json")
    rooms_file = ctx.room.file or rooms.rooms_path(session_key)
    current_room_file = ctx.room.current_file or rooms.current_room_path(session_key)
    fasttravel_file = rooms.fasttravel_path(session_key)

    # Flush GMCP snapshot so the bars editor can read it.
    gmcp_snapshot_file = ctx.gmcp.snapshot_file or ""
    if gmcp_snapshot_file and ctx.gmcp_data:
        gmcp_snapshot.save_gmcp_snapshot(gmcp_snapshot_file, session_key, ctx.gmcp_data)
        ctx.gmcp.dirty = False

    # Trigger select pattern.
    engine = ctx.triggers.engine
    select_pattern = getattr(engine, "last_matched_pattern", "") if engine else ""

    # Chat / capture data.
    chat_file = ctx.chat.file or ""
    ctx.chat.unread = 0
    captures = ctx.highlights.captures
    capture_log = ctx.highlights.capture_log
    initial_channel = most_recent_channel(ctx.chat.messages, capture_log)

    capture_file = ""
    if captures or capture_log:
        fd, capture_file = tempfile.mkstemp(suffix=".json", prefix="captures-")
        os.close(fd)
        with open(capture_file, "w", encoding="utf-8") as fh:
            json.dump({"captures": captures, "capture_log": capture_log}, fh)

    params = {
        "initial_tab": initial_tab,
        "session_key": session_key,
        "highlights_file": highlights_file,
        "macros_file": macros_file,
        "triggers_file": triggers_file,
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

    log.debug(
        "unified_editor: pre-thread initial_tab=%s TERM=%s COLORTERM=%s terminal_size=%s",
        initial_tab,
        os.environ.get("TERM", ""),
        os.environ.get("COLORTERM", ""),
        paths.safe_terminal_size(),
    )
    _prepare_terminal()
    _run_in_thread(
        lambda: client_tui_dialogs.run_unified_editor(params),
        replay_buf=replay_buf,
        cleanup_files=[capture_file] if capture_file else None,
    )

    # Reload all configs that may have been modified.
    reload_macros(ctx, macros_file, session_key, log)
    reload_highlights(ctx, highlights_file, session_key, log)
    reload_triggers(ctx, triggers_file, session_key, log)
    reload_progressbars(ctx, progressbars_file, session_key, log)

    # Rebuild REPL color styles in case the theme was changed.
    repl_theme.invalidate_cache()
    client_repl_render.make_styles()

    # Reload room graph.
    room_graph = ctx.room.graph
    if room_graph is not None:
        room_graph.load_adjacency()

    # Handle fast travel.
    steps, noreply = rooms.read_fasttravel(fasttravel_file)
    if steps:
        log.debug("travel: scheduling %d steps (noreply=%s)", len(steps), noreply)
        task = asyncio.ensure_future(client_repl_travel.fast_travel(steps, ctx, log, noreply=noreply))
        ctx.walk.travel_task = task

        def on_done(t: "asyncio.Task[None]") -> None:
            if ctx.walk.travel_task is t:
                ctx.walk.travel_task = None
            if not t.cancelled() and t.exception() is not None:
                log.warning("fast travel failed: %s", t.exception())

        task.add_done_callback(on_done)


def launch_tui_editor(editor_type: str, ctx: "TelixSessionContext", replay_buf: typing.Any | None = None) -> None:
    """
    Launch a TUI editor for macros or triggers in a subprocess.

    :param editor_type: ``"macros"``, ``"triggers"``, or ``"highlights"``.
    :param ctx: Session context with file path and definition attributes.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    """
    from . import client_tui_base, client_tui_editors

    session_key = ctx.session_key

    if editor_type == "macros":
        path = ctx.macros.file or os.path.join(paths.CONFIG_DIR, "macros.json")
        rp = ctx.room.file or rooms.rooms_path(session_key)
        crp = ctx.room.current_file or rooms.current_room_path(session_key)
        target = lambda: client_tui_base.launch_editor_in_thread(  # noqa: E731
            client_tui_editors.MacroEditScreen(
                path=path, session_key=session_key, rooms_file=rp, current_room_file=crp
            ),
            session_key=session_key,
        )
    elif editor_type == "highlights":
        path = ctx.highlights.file or os.path.join(paths.CONFIG_DIR, "highlights.json")
        target = lambda: client_tui_base.launch_editor_in_thread(  # noqa: E731
            client_tui_editors.HighlightEditScreen(path=path, session_key=session_key), session_key=session_key
        )
    elif editor_type == "progressbars":
        path = ctx.progress.file or os.path.join(paths.CONFIG_DIR, "progressbars.json")
        snap = ctx.gmcp.snapshot_file or ""
        if snap and ctx.gmcp_data:
            gmcp_snapshot.save_gmcp_snapshot(snap, session_key, ctx.gmcp_data)
            ctx.gmcp.dirty = False
        target = lambda: client_tui_base.launch_editor_in_thread(  # noqa: E731
            client_tui_editors.ProgressBarEditScreen(path=path, session_key=session_key, gmcp_snapshot_path=snap),
            session_key=session_key,
        )
    else:
        path = ctx.triggers.file or os.path.join(paths.CONFIG_DIR, "triggers.json")
        snap = ctx.gmcp.snapshot_file or ""
        if snap and ctx.gmcp_data:
            gmcp_snapshot.save_gmcp_snapshot(snap, session_key, ctx.gmcp_data)
            ctx.gmcp.dirty = False
        engine = ctx.triggers.engine
        select = getattr(engine, "last_matched_pattern", "") if engine else ""
        target = lambda: client_tui_base.launch_editor_in_thread(  # noqa: E731
            client_tui_editors.TriggerEditScreen(
                path=path, session_key=session_key, select_pattern=select, gmcp_snapshot_path=snap
            ),
            session_key=session_key,
        )

    log.debug(
        "tui_editor: pre-thread fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s stderr_isatty=%s editor_type=%s "
        "TERM=%s COLORTERM=%s terminal_size=%s",
        getattr(os, "get_blocking", lambda fd: None)(0),
        getattr(os, "get_blocking", lambda fd: None)(1),
        getattr(os, "get_blocking", lambda fd: None)(2),
        sys.stdin.isatty(),
        sys.__stderr__.isatty(),
        editor_type,
        os.environ.get("TERM", ""),
        os.environ.get("COLORTERM", ""),
        paths.safe_terminal_size(),
    )
    _prepare_terminal()
    _run_in_thread(target, replay_buf=replay_buf)

    if editor_type == "macros":
        reload_macros(ctx, path, session_key, log)
    elif editor_type == "highlights":
        reload_highlights(ctx, path, session_key, log)
    elif editor_type == "progressbars":
        reload_progressbars(ctx, path, session_key, log)
    else:
        reload_triggers(ctx, path, session_key, log)


def reload_macros(ctx: "TelixSessionContext", path: str, session_key: str, log: logging.Logger) -> None:
    """Reload macro definitions from disk and update dispatch."""
    if not os.path.exists(path):
        return
    try:
        new_defs = macros.load_macros(path, session_key)
        ctx.macros.defs = new_defs
        ctx.macros.file = path
        dispatch = ctx.repl.key_dispatch
        if dispatch is not None:
            dispatch.set_macros(new_defs, ctx, log)
        log.info("reloaded %d macros from %s", len(new_defs), path)
    except ValueError as exc:
        log.warning("failed to reload macros: %s", exc)


def reload_triggers(ctx: "TelixSessionContext", path: str, session_key: str, log: logging.Logger) -> None:
    """Reload trigger rules from disk after editing."""
    if not os.path.exists(path):
        return
    try:
        ctx.triggers.rules = trigger.load_triggers(path, session_key)
        ctx.triggers.file = path
        n_rules = len(ctx.triggers.rules)
        log.info("reloaded %d triggers from %s", n_rules, path)
    except ValueError as exc:
        log.warning("failed to reload triggers: %s", exc)


def reload_progressbars(ctx: "TelixSessionContext", path: str, session_key: str, log: logging.Logger) -> None:
    """Reload progress bar configs from disk after editing."""
    if not os.path.exists(path):
        return
    try:
        ctx.progress.configs = progressbars.load_progressbars(path, session_key)
        ctx.progress.file = path
        n_bars = len(ctx.progress.configs)
        log.info("reloaded %d progress bars from %s", n_bars, path)
    except (ValueError, FileNotFoundError):
        log.warning("failed to reload progress bars from %s", path)


def reload_highlights(ctx: "TelixSessionContext", path: str, session_key: str, log: logging.Logger) -> None:
    """Reload highlight rules from disk after editing."""
    if not os.path.exists(path):
        return
    try:
        ctx.highlights.rules = highlighter.load_highlights(path, session_key)
        ctx.highlights.file = path
        n_rules = len(ctx.highlights.rules)
        log.info("reloaded %d highlights from %s", n_rules, path)
    except ValueError as exc:
        log.warning("failed to reload highlights: %s", exc)


def launch_chat_viewer(ctx: "TelixSessionContext", replay_buf: typing.Any | None = None) -> None:
    """
    Launch the Capture Window TUI in a worker thread.

    Writes capture data (``ctx.captures`` and ``ctx.capture_log``) to a
    temporary JSON file and passes its path to the worker thread.

    :param ctx: Session context with chat and capture state.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    """
    from . import client_tui_captures

    session_key = ctx.session_key
    if not session_key:
        return

    chat_file = ctx.chat.file or ""
    ctx.chat.unread = 0

    capture_file = ""
    captures = ctx.highlights.captures
    capture_log = ctx.highlights.capture_log
    initial_channel = most_recent_channel(ctx.chat.messages, capture_log)
    if captures or capture_log:
        fd, capture_file = tempfile.mkstemp(suffix=".json", prefix="captures-")
        os.close(fd)
        with open(capture_file, "w", encoding="utf-8") as fh:
            json.dump({"captures": captures, "capture_log": capture_log}, fh)

    log.debug("chat_viewer: launching thread")
    _prepare_terminal()
    _run_in_thread(
        lambda: client_tui_captures.run_chat_viewer(chat_file, session_key, initial_channel, capture_file),
        replay_buf=replay_buf,
        cleanup_files=[capture_file] if capture_file else None,
    )


def launch_room_browser(ctx: "TelixSessionContext", replay_buf: typing.Any | None = None) -> None:
    """
    Launch the room browser TUI in a subprocess.

    On return, check for a fast travel file and queue movement commands.

    :param ctx: Session context with session attributes.
    :param replay_buf: Optional replay buffer for screen repaint on return.
    """
    session_key = ctx.session_key
    if not session_key:
        return

    from . import client_tui_base, client_tui_rooms

    rp = ctx.room.file or rooms.rooms_path(session_key)
    crp = ctx.room.current_file or rooms.current_room_path(session_key)
    ftp = rooms.fasttravel_path(session_key)

    log.debug(
        "room_browser: pre-thread fd0_blocking=%s fd1=%s fd2=%s "
        "stdin_isatty=%s stderr_isatty=%s "
        "TERM=%s COLORTERM=%s terminal_size=%s",
        getattr(os, "get_blocking", lambda fd: None)(0),
        getattr(os, "get_blocking", lambda fd: None)(1),
        getattr(os, "get_blocking", lambda fd: None)(2),
        sys.stdin.isatty(),
        sys.__stderr__.isatty(),
        os.environ.get("TERM", ""),
        os.environ.get("COLORTERM", ""),
        paths.safe_terminal_size(),
    )
    _prepare_terminal()
    _run_in_thread(
        lambda: client_tui_base.launch_editor_in_thread(
            client_tui_rooms.RoomBrowserScreen(
                rooms_path=rp, session_key=session_key, current_room_file=crp, fasttravel_file=ftp
            ),
            session_key=session_key,
        ),
        replay_buf=replay_buf,
    )

    room_graph = ctx.room.graph
    if room_graph is not None:
        room_graph.load_adjacency()

    steps, noreply = rooms.read_fasttravel(ftp)
    if steps:
        log.debug("travel: scheduling %d steps (noreply=%s)", len(steps), noreply)
        task = asyncio.ensure_future(client_repl_travel.fast_travel(steps, ctx, log, noreply=noreply))
        ctx.walk.travel_task = task

        def on_done(t: "asyncio.Task[None]") -> None:
            if ctx.walk.travel_task is t:
                ctx.walk.travel_task = None
            if not t.cancelled() and t.exception() is not None:
                log.warning("fast travel failed: %s", t.exception())

        task.add_done_callback(on_done)
