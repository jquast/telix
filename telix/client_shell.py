"""
Telix client shell -- wraps telnetlib3's terminal handling with REPL support.

Provides :func:`telix_client_shell`, a drop-in replacement for
:func:`telnetlib3.client_shell.telnet_client_shell` that creates a
:class:`~telix.session_context.SessionContext`, loads per-session configs
(macros, autoreplies, highlights, chat, rooms), and alternates between
REPL and raw event loops based on telnet negotiation state.

Also provides :func:`ws_client_shell` for WebSocket connections using
the ``gmcp.mudstandards.org`` wire format.
"""

# std imports
import io
import os
import sys
import shlex
import typing
import asyncio
import logging
import contextlib

# 3rd party
import telnetlib3
import telnetlib3.client
import telnetlib3.telopt
import telnetlib3.client_shell
import telnetlib3.stream_reader
import telnetlib3.stream_writer

# local
from . import (
    chat,
    paths,
    rooms,
    macros,
    autoreply,
    client_repl,
    highlighter,
    progressbars,
    ws_transport,
    ssh_transport,
    session_context,
)

log = logging.getLogger(__name__)

__all__ = ("ssh_client_shell", "telix_client_shell", "ws_client_shell")


def load_configs(ctx: "session_context.TelixSessionContext") -> None:
    """
    Create config/data directories and load all per-session config files into *ctx*.

    Handles macros, autoreplies, highlights, progress bars, GMCP snapshot, chat, rooms, and history.  Missing files are
    silently skipped so first-time connections start with empty defaults.

    :param ctx: Session context to populate.
    """
    config_dir = str(paths.xdg_config_dir())
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(str(paths.xdg_data_dir()), exist_ok=True)

    macros_path = os.path.join(config_dir, "macros.json")
    ctx.macros_file = macros_path
    if os.path.isfile(macros_path):
        ctx.macro_defs = macros.load_macros(macros_path, ctx.session_key)
    ctx.macro_defs = macros.ensure_builtin_macros(ctx.macro_defs)

    disconnect = next((m for m in ctx.macro_defs if m.builtin_name == "disconnect" and m.enabled), None)
    if disconnect is not None:
        seq = macros.key_name_to_seq(disconnect.key)
        if seq is not None:
            ctx.keyboard_escape = seq

    autoreplies_path = os.path.join(config_dir, "autoreplies.json")
    ctx.autoreplies_file = autoreplies_path
    if os.path.isfile(autoreplies_path):
        ctx.autoreply_rules = autoreply.load_autoreplies(autoreplies_path, ctx.session_key)

    highlights_path = os.path.join(config_dir, "highlights.json")
    ctx.highlights_file = highlights_path
    if os.path.isfile(highlights_path):
        ctx.highlight_rules = highlighter.load_highlights(highlights_path, ctx.session_key)

    progressbars_path = os.path.join(config_dir, "progressbars.json")
    ctx.progressbars_file = progressbars_path
    if os.path.isfile(progressbars_path):
        ctx.progressbar_configs = progressbars.load_progressbars(progressbars_path, ctx.session_key)

    ctx.gmcp_snapshot_file = paths.gmcp_snapshot_path(ctx.session_key)

    chat_file = paths.chat_path(ctx.session_key)
    ctx.chat_file = chat_file
    if os.path.isfile(chat_file):
        ctx.chat_messages = chat.load_chat(chat_file)
    ctx.on_chat_text = lambda data: chat.append_chat_msg(ctx, data)
    ctx.on_chat_channels = lambda data: setattr(ctx, "chat_channels", data)

    rooms_file = rooms.rooms_path(ctx.session_key)
    ctx.rooms_file = rooms_file
    ctx.current_room_file = rooms.current_room_path(ctx.session_key)
    ctx.room_graph = rooms.RoomStore(rooms_file)

    def on_room_info(data: typing.Any) -> None:
        num = str(data["num"])
        ctx.previous_room_num = ctx.current_room_num
        ctx.current_room_num = num
        ctx.room_changed.set()
        ctx.room_changed.clear()
        ctx.room_graph.update_room(data)
        rooms.write_current_room(ctx.current_room_file, num)

    ctx.on_room_info = on_room_info
    ctx.history_file = paths.history_path(ctx.session_key)


class ColorFilteredWriter:
    """
    Wraps an ``asyncio.StreamWriter`` to apply the session color filter to all writes.

    Used in raw-mode paths where :func:`telnetlib3.client_shell._raw_event_loop` writes
    decoded server text as bytes directly to stdout, bypassing the REPL's filter step.

    :param inner: The underlying ``asyncio.StreamWriter``.
    :param ctx: Session context carrying ``color_filter`` and ``erase_eol``.
    :param encoding: Character encoding for decoding bytes before filtering.
    """

    def __init__(
        self, inner: asyncio.StreamWriter, ctx: session_context.TelixSessionContext, encoding: "str | None" = None
    ) -> None:
        self.inner = inner
        self.ctx = ctx
        self.encoding = encoding or ctx.encoding or "utf-8"

    def write(self, data: bytes) -> None:
        """Filter *data* through the color filter if one is active, then write."""
        # XXX TODO: implement dynamically changing IncrementalDecoder like done in telnetlib3
        cf = self.ctx.color_filter
        if cf is not None:
            text = data.decode(self.encoding, errors="replace")
            text = cf.filter(text)
            if self.ctx.erase_eol:
                text = text.replace("\r\n", "\x1b[K\r\n\x1b[K")
            data = text.encode(self.ctx.encoding, errors="replace")
        self.inner.write(data)

    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self.inner, name)


def build_session_key(
    writer: (
        telnetlib3.stream_writer.TelnetWriter
        | telnetlib3.stream_writer.TelnetWriterUnicode
        | ws_transport.WebSocketWriter
        | ssh_transport.SSHWriter
    ),
) -> str:
    """
    Derive ``host:port`` session key from CLI arguments or peername.

    For telnet writers, prefers the original hostname from ``sys.argv``
    over the resolved IP from :func:`socket.getpeername`, so that
    session-specific files (history, rooms, macros, etc.) are keyed by
    the human-readable hostname used to connect.

    For WebSocket and SSH writers, falls through directly to peername since the
    hostname is already set by the respective client.
    """
    if isinstance(writer, (ws_transport.WebSocketWriter, ssh_transport.SSHWriter)):
        peername = writer.get_extra_info("peername")
        if peername:
            return f"{peername[0]}:{peername[1]}"
        return ""
    _stderr_buf = io.StringIO()
    try:
        from . import main as _main_mod

        # Strip telix-specific args (e.g. --colormatch none) before passing to telnetlib3's parser
        # so they don't get misread as the positional port argument.
        stripped = _main_mod._build_telix_parser().parse_known_args(sys.argv[1:])[1]
        with contextlib.redirect_stderr(_stderr_buf):
            args = telnetlib3.client._get_argument_parser().parse_known_args(stripped)[0]
        if args.host and not args.host.startswith(("ws://", "wss://")):
            return f"{args.host}:{args.port}"
    except (SystemExit, Exception):
        pass
    finally:
        lines = [line for line in _stderr_buf.getvalue().splitlines() if line.strip()]
        if lines:
            log.error("argv parse error (command: %s):", shlex.join(sys.argv))
            for line in lines:
                log.error("%s", line)
            # "required" errors mean argv lacks connection info; use peername fallback.
            # "invalid" errors (wrong type, bad value) indicate a real misconfiguration.
            if any("invalid" in line.lower() for line in lines):
                sys.exit(1)
    peername = writer.get_extra_info("peername")
    if peername:
        return f"{peername[0]}:{peername[1]}"
    return ""


def want_repl(
    ctx: session_context.TelixSessionContext,
    writer: (telnetlib3.stream_writer.TelnetWriter | telnetlib3.stream_writer.TelnetWriterUnicode),
) -> bool:
    """Return True when the REPL should be active."""
    if ctx.raw_mode is True:
        return False
    return ctx.repl_enabled and getattr(writer, "mode", "local") == "local"


def _setup_color_filter(
    ctx: session_context.TelixSessionContext,
    writer: (
        telnetlib3.stream_writer.TelnetWriter
        | telnetlib3.stream_writer.TelnetWriterUnicode
        | ws_transport.WebSocketWriter
    ),
) -> None:
    """
    Create and attach a color filter from telix CLI args and terminal detection.

    Reads color options from :data:`telix.main._color_args` (set by
    :func:`~telix.main.main` before the shell starts) and encoding from
    the telnetlib3 writer context.  For retro encodings (PETSCII, ATASCII),
    uses the encoding-specific filter instead of ColorFilter.
    """
    from . import main as _main_mod
    from .color_filter import PALETTES, ColorConfig, ColorFilter, PetsciiColorFilter, AtasciiControlFilter

    args = _main_mod._color_args
    if args is None:
        return

    colormatch: str = args.colormatch or "vga"
    if colormatch.lower() == "none":
        return

    encoding_name: str = getattr(writer.ctx, "encoding", "") or ""
    if not encoding_name:
        encoding_name = getattr(writer, "default_encoding", "") or ""
    is_petscii = encoding_name.lower() in ("petscii", "cbm", "commodore", "c64", "c128")
    is_atascii = encoding_name.lower() in ("atascii", "atari8bit", "atari_8bit")
    if colormatch == "petscii":
        colormatch = "c64"
    if is_petscii and colormatch != "c64":
        colormatch = "c64"

    if colormatch not in PALETTES:
        log.warning("Unknown palette %r, disabling color filter", colormatch)
        return

    if is_petscii or colormatch == "c64":
        ctx.color_filter = PetsciiColorFilter(brightness=args.color_brightness, contrast=args.color_contrast)
        return

    if is_atascii:
        ctx.color_filter = AtasciiControlFilter()
        return

    bg_color: tuple[int, int, int] = (0, 0, 0)
    bg_str = args.background_color
    if isinstance(bg_str, str) and bg_str.startswith("#") and len(bg_str) == 7:
        bg_color = (int(bg_str[1:3], 16), int(bg_str[3:5], 16), int(bg_str[5:7], 16))
    elif isinstance(bg_str, tuple):
        bg_color = bg_str
    fg_color: tuple[int, int, int] | None = None

    # Terminal colors detected at startup (before any framework took stdin) are
    # stored in env vars so subprocess connections inherit them automatically.
    bg_env = os.environ.get("TELIX_DETECTED_BG")
    if bg_env:
        r, g, b = (int(x) for x in bg_env.split(","))
        bg_color = (r, g, b)
    fg_env = os.environ.get("TELIX_DETECTED_FG")
    if fg_env:
        r, g, b = (int(x) for x in fg_env.split(","))
        fg_color = (r, g, b)

    force_black_bg = getattr(args, "force_black_bg", False)
    if force_black_bg:
        bg_color = (0, 0, 0)
        fg_color = None

    color_config = ColorConfig(
        palette_name=colormatch,
        brightness=args.color_brightness,
        contrast=args.color_contrast,
        background_color=bg_color,
        ice_colors=not args.no_ice_colors,
        foreground_color=fg_color,
        force_black_bg=force_black_bg,
    )
    ctx.color_filter = ColorFilter(color_config)
    ctx.erase_eol = True


def _setup_ansi_keys(ctx: "session_context.TelixSessionContext") -> None:
    """
    Set ``ctx.ansi_keys`` from the telix CLI ``--ansi-keys`` flag.

    Reads :data:`telix.main._color_args` set by :func:`~telix.main.main`
    before the shell starts.  No-op when called outside a main() context.

    :param ctx: Session context to update.
    """
    from . import main as _main_mod

    args = _main_mod._color_args
    if args is None:
        return
    ctx.ansi_keys = bool(getattr(args, "ansi_keys", False))


async def telix_client_shell(
    telnet_reader: (telnetlib3.stream_reader.TelnetReader | telnetlib3.stream_reader.TelnetReaderUnicode),
    telnet_writer: (telnetlib3.stream_writer.TelnetWriter | telnetlib3.stream_writer.TelnetWriterUnicode),
) -> None:
    """
    Telix client shell with REPL/raw mode switching.

    Drop-in replacement for
    :func:`telnetlib3.client_shell.telnet_client_shell`.
    Creates a :class:`SessionContext`, loads configs, and runs an outer
    loop that alternates between the REPL (line-mode) and raw event loop
    based on telnet negotiation state.

    :param telnet_reader: Server-side telnet reader stream.
    :param telnet_writer: Client-side telnet writer stream.
    """
    # 1. Build SessionContext and attach to writer, preserving attributes
    #    that run_client() wrappers already set on the original ctx.
    ctx = telnet_writer.ctx = session_context.TelixSessionContext.create_using_telnet_ctx(
        writer=telnet_writer,
        session_key=build_session_key(telnet_writer),
        encoding=telnet_writer.fn_encoding(incoming=True),
    )
    ctx.repl_enabled = True

    # 2. Load per-session configs, set up color filter from CLI arguments
    load_configs(ctx)

    _setup_color_filter(ctx, telnet_writer)
    _setup_ansi_keys(ctx)

    # 3. Setup GMCP callbacks
    base_on_gmcp = telnet_writer._ext_callback.get(telnetlib3.telopt.GMCP)

    def on_gmcp(package: str, data: typing.Any) -> None:
        if base_on_gmcp is not None:
            base_on_gmcp(package, data)
        if package == "Comm.Channel.Text":
            if ctx.on_chat_text is not None:
                ctx.on_chat_text(data)
        elif package == "Comm.Channel.List":
            if ctx.on_chat_channels is not None:
                ctx.on_chat_channels(data)
        elif package == "Room.Info":
            if ctx.on_room_info is not None:
                ctx.on_room_info(data)

    telnet_writer.set_ext_callback(telnetlib3.telopt.GMCP, on_gmcp)

    keyboard_escape = ctx.keyboard_escape

    with telnetlib3.client_shell.Terminal(telnet_writer=telnet_writer) as tty_shell:
        linesep = "\n"
        switched_to_raw = False
        last_will_echo = False
        local_echo = tty_shell.software_echo
        if tty_shell._istty:
            raw_mode = telnetlib3.client_shell._get_raw_mode(telnet_writer)
            if telnet_writer.will_echo or raw_mode is True:
                linesep = "\r\n"
        stdout = await tty_shell.make_stdout()  # pylint: disable=no-member
        tty_shell.setup_winch()

        # EOR/GA-based command pacing for raw-mode autoreplies.
        prompt_ready_raw = asyncio.Event()
        prompt_ready_raw.set()
        ga_detected_raw = False

        def on_prompt_signal_raw(cmd: bytes) -> None:
            nonlocal ga_detected_raw
            ga_detected_raw = True
            prompt_ready_raw.set()
            ar = ctx.autoreply_engine
            if ar is not None:
                ar.on_prompt()

        telnet_writer.set_iac_callback(telnetlib3.telopt.GA, on_prompt_signal_raw)
        telnet_writer.set_iac_callback(telnetlib3.telopt.CMD_EOR, on_prompt_signal_raw)

        async def wait_for_prompt_raw() -> None:
            if not ga_detected_raw:
                return
            try:
                await asyncio.wait_for(prompt_ready_raw.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            prompt_ready_raw.clear()

        ctx.autoreply_wait_fn = wait_for_prompt_raw

        escape_name = telnetlib3.accessories.name_unicode(keyboard_escape)
        banner_sep = "\r\n" if tty_shell._istty else linesep
        stdout.write(f"Escape character is '{escape_name}'.{banner_sep}".encode())

        def handle_close(msg: str) -> None:
            cf = ctx.color_filter
            if cf is not None:
                flush = cf.flush()
                if flush:
                    stdout.write(flush.encode())
            stdout.write(f"\033[m{linesep}{msg}{linesep}".encode())
            tty_shell.cleanup_winch()

        def check_want_repl() -> bool:
            return want_repl(ctx, telnet_writer)

        # Wait briefly for negotiation to settle before deciding to
        # enter the REPL or evaluating will_echo for local_echo.
        # Servers that negotiate ECHO+SGA (kludge mode) often send
        # those options shortly after connection, and entering the
        # REPL only to immediately exit causes scroll region
        # corruption.  In forced raw mode, skipping this wait causes
        # local_echo to be set before the server negotiates WILL ECHO,
        # resulting in double echo (local + server).
        if ctx.raw_mode is not False and tty_shell._istty:
            try:
                await asyncio.wait_for(telnet_writer.wait_for_condition(lambda w: w.mode != "local"), timeout=0.05)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # Outer loop: alternate between REPL and raw modes.
        while True:
            if check_want_repl() and tty_shell._istty:
                mode_switched = await client_repl.repl_event_loop(
                    telnet_reader, telnet_writer, tty_shell, stdout, history_file=ctx.history_file
                )
                if not mode_switched:
                    # Connection closed normally.
                    break
                # Server switched to kludge/raw mode -- fall through.

            # Raw event loop.
            if not switched_to_raw and tty_shell._istty and tty_shell._save_mode is not None:
                tty_shell.set_mode(tty_shell._make_raw(tty_shell._save_mode, suppress_echo=True))
                switched_to_raw = True
                local_echo = not telnet_writer.will_echo
                linesep = "\r\n"
            stdin = await tty_shell.connect_stdin()  # pylint: disable=no-member
            state = telnetlib3.client_shell._RawLoopState(
                switched_to_raw=switched_to_raw, last_will_echo=last_will_echo, local_echo=local_echo, linesep=linesep
            )
            raw_stdout = ColorFilteredWriter(stdout, ctx) if ctx.color_filter is not None else stdout
            await telnetlib3.client_shell._raw_event_loop(
                telnet_reader,
                telnet_writer,
                tty_shell,
                stdin,
                raw_stdout,  # type: ignore[arg-type]
                keyboard_escape,
                state,
                handle_close,
                check_want_repl,
            )
            tty_shell.disconnect_stdin(stdin)  # pylint: disable=no-member
            # Carry forward state from the raw loop.
            switched_to_raw = state.switched_to_raw
            last_will_echo = state.last_will_echo
            local_echo = state.local_echo
            linesep = state.linesep
            if state.reactivate_repl and check_want_repl():
                # Server returned to line mode -- loop back to REPL.
                continue
            # Connection closed.
            break

        ctx.close()


async def ssh_client_shell(ssh_reader: ssh_transport.SSHReader, ssh_writer: ssh_transport.SSHWriter) -> None:
    """
    Telix client shell for SSH connections.

    SSH connections are always BBS/raw mode -- no telnet negotiation occurs, so
    there is no REPL line-mode switching.  Creates a
    :class:`~telix.session_context.TelixSessionContext`, loads configs, and
    runs a single raw event-loop pass.

    :param ssh_reader: :class:`~telix.ssh_transport.SSHReader` fed by the receive loop.
    :param ssh_writer: :class:`~telix.ssh_transport.SSHWriter` wrapping the SSH process.
    """
    import telnetlib3._session_context

    ssh_writer.ctx = telnetlib3._session_context.TelnetSessionContext(
        raw_mode=True, autoreply_engine=None, autoreply_wait_fn=None, typescript_file=None
    )

    ctx = ssh_writer.ctx = session_context.TelixSessionContext.create_using_telnet_ctx(
        session_key=build_session_key(ssh_writer), writer=ssh_writer, encoding=ssh_writer.encoding
    )
    ctx.repl_enabled = False
    ctx.raw_mode = True

    load_configs(ctx)
    _setup_color_filter(ctx, ssh_writer)

    keyboard_escape = ctx.keyboard_escape

    with telnetlib3.client_shell.Terminal(telnet_writer=ssh_writer) as tty_shell:  # type: ignore[arg-type]
        linesep = "\r\n"
        stdout = await tty_shell.make_stdout()
        tty_shell.setup_winch()

        escape_name = telnetlib3.accessories.name_unicode(keyboard_escape)
        stdout.write(f"Escape character is '{escape_name}'.{linesep}".encode())

        def handle_close(msg: str) -> None:
            cf = ctx.color_filter
            if cf is not None:
                flush = cf.flush()
                if flush:
                    stdout.write(flush.encode())
            stdout.write(f"\033[m{linesep}{msg}{linesep}".encode())
            tty_shell.cleanup_winch()

        if tty_shell._istty:
            if tty_shell._save_mode is not None:
                tty_shell.set_mode(tty_shell._make_raw(tty_shell._save_mode, suppress_echo=True))
            stdin = await tty_shell.connect_stdin()  # pylint: disable=no-member
            state = telnetlib3.client_shell._RawLoopState(
                switched_to_raw=True, last_will_echo=False, local_echo=not ssh_writer.will_echo, linesep=linesep
            )
            raw_stdout = ColorFilteredWriter(stdout, ctx) if ctx.color_filter is not None else stdout
            await telnetlib3.client_shell._raw_event_loop(
                ssh_reader,  # type: ignore[arg-type]
                ssh_writer,  # type: ignore[arg-type]
                tty_shell,
                stdin,
                raw_stdout,  # type: ignore[arg-type]
                keyboard_escape,
                state,
                handle_close,
                lambda: False,
            )
            tty_shell.disconnect_stdin(stdin)  # pylint: disable=no-member
        else:
            handle_close("Connection closed.")
    ctx.close()


async def ws_client_shell(ws_reader: ws_transport.WebSocketReader, ws_writer: ws_transport.WebSocketWriter) -> None:
    """
    Telix client shell for WebSocket connections.

    Simpler counterpart to :func:`telix_client_shell` -- WebSocket
    connections are always line-mode (no raw/kludge switching), so this
    function creates a :class:`SessionContext`, loads configs, wires GMCP
    dispatch, and runs a single pass of the REPL event loop.

    The pseudo-prompt signal (GA/EOR) is fired by the receive loop in
    :mod:`~telix.ws_client` after each BINARY frame delivery, giving the
    REPL the same prompt boundary as telnet.

    :param reader: :class:`WebSocketReader` fed by the receive loop.
    :param writer: :class:`WebSocketWriter` wrapping the WebSocket connection.
    """
    # 1. Build SessionContext and attach to writer, preserving attributes from initial ctx.
    no_repl = getattr(ws_writer.ctx, "no_repl", False)
    ctx = ws_writer.ctx = session_context.TelixSessionContext.create_using_telnet_ctx(
        session_key=build_session_key(ws_writer), writer=ws_writer, encoding=ws_writer.encoding
    )
    ctx.repl_enabled = not no_repl

    # 2. Load per-session configs.
    load_configs(ctx)

    # 2b. Set up color filter from CLI args.
    _setup_color_filter(ctx, ws_writer)

    # 3. Wire GMCP dispatch (no base callback -- WebSocket has none).
    def on_gmcp(package: str, data: typing.Any) -> None:
        if package == "Comm.Channel.Text":
            if ctx.on_chat_text is not None:
                ctx.on_chat_text(data)
        elif package == "Comm.Channel.List":
            if ctx.on_chat_channels is not None:
                ctx.on_chat_channels(data)
        elif package == "Room.Info":
            if ctx.on_room_info is not None:
                ctx.on_room_info(data)

    ws_writer.set_ext_callback(ws_transport.GMCP, on_gmcp)

    keyboard_escape = ctx.keyboard_escape

    # Terminal / repl_event_loop / _flush_color_filter are typed for
    # TelnetWriter but accept any duck-compatible writer at runtime.
    with telnetlib3.client_shell.Terminal(telnet_writer=ws_writer) as tty_shell:  # type: ignore[arg-type]
        linesep = "\n"
        stdout = await tty_shell.make_stdout()
        tty_shell.setup_winch()

        escape_name = telnetlib3.accessories.name_unicode(keyboard_escape)
        banner_sep = "\r\n" if tty_shell._istty else linesep
        stdout.write(f"Escape character is '{escape_name}'.{banner_sep}".encode())

        def handle_close(msg: str) -> None:
            cf = ctx.color_filter
            if cf is not None:
                flush = cf.flush()
                if flush:
                    stdout.write(flush.encode())
            stdout.write(f"\033[m{linesep}{msg}{linesep}".encode())
            tty_shell.cleanup_winch()

        if tty_shell._istty and ctx.repl_enabled:
            await client_repl.repl_event_loop(
                ws_reader,  # type: ignore[arg-type]
                ws_writer,  # type: ignore[arg-type]
                tty_shell,
                stdout,
                history_file=ctx.history_file,
            )
            handle_close("Connection closed.")
        elif tty_shell._istty:
            # Raw mode: byte-at-a-time I/O for BBS connections.
            if tty_shell._save_mode is not None:
                tty_shell.set_mode(tty_shell._make_raw(tty_shell._save_mode, suppress_echo=True))
            linesep = "\r\n"
            stdin = await tty_shell.connect_stdin()  # pylint: disable=no-member
            state = telnetlib3.client_shell._RawLoopState(
                switched_to_raw=True, last_will_echo=False, local_echo=not ws_writer.will_echo, linesep=linesep
            )
            raw_stdout = ColorFilteredWriter(stdout, ctx) if ctx.color_filter is not None else stdout
            await telnetlib3.client_shell._raw_event_loop(
                ws_reader,  # type: ignore[arg-type]
                ws_writer,  # type: ignore[arg-type]
                tty_shell,
                stdin,
                raw_stdout,  # type: ignore[arg-type]
                keyboard_escape,
                state,
                handle_close,
                lambda: False,  # never switch back to REPL
            )
            tty_shell.disconnect_stdin(stdin)  # pylint: disable=no-member
        else:
            handle_close("Connection closed.")
    ctx.close()
