"""
Telix client shell -- wraps telnetlib3's terminal handling with REPL support.

Provides :func:`telix_client_shell`, a drop-in replacement for
:func:`telnetlib3.client_shell.telnet_client_shell` that creates a
:class:`~telix.session_context.SessionContext`, loads per-session configs
(macros, autoreplies, highlights, chat, rooms), and alternates between
REPL and raw event loops based on telnet negotiation state.
"""

# std imports
import os
import sys
import typing
import asyncio
import logging

# 3rd party
import telnetlib3
import telnetlib3.client
import telnetlib3.telopt
import telnetlib3.client_shell
import telnetlib3.stream_reader
import telnetlib3.stream_writer

# local
from . import chat, paths, rooms, macros, autoreply, client_repl, highlighter, progressbars, session_context

log = logging.getLogger(__name__)

__all__ = ("telix_client_shell",)


def build_session_key(
    writer: (telnetlib3.stream_writer.TelnetWriter | telnetlib3.stream_writer.TelnetWriterUnicode),
) -> str:
    """
    Derive ``host:port`` session key from CLI arguments or peername.

    Prefers the original hostname from ``sys.argv`` over the resolved IP
    from :func:`socket.getpeername`, so that session-specific files
    (history, rooms, macros, etc.) are keyed by the human-readable
    hostname used to connect.
    """
    try:
        args = telnetlib3.client._get_argument_parser().parse_known_args(sys.argv[1:])[0]
        if args.host:
            return f"{args.host}:{args.port}"
    except (SystemExit, Exception):
        pass
    peername = writer.get_extra_info("peername")
    if peername:
        return f"{peername[0]}:{peername[1]}"
    return ""


def load_configs(ctx: session_context.SessionContext) -> None:
    """
    Load per-session config files into *ctx*.

    Missing config files are silently skipped so first-time connections
    start with empty defaults.
    """
    session_key = ctx.session_key
    config_dir = str(paths.xdg_config_dir())
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(str(paths.xdg_data_dir()), exist_ok=True)

    # macros
    macros_path = os.path.join(config_dir, "macros.json")
    ctx.macros_file = macros_path
    if os.path.isfile(macros_path):
        ctx.macro_defs = macros.load_macros(macros_path, session_key)

    # autoreplies
    autoreplies_path = os.path.join(config_dir, "autoreplies.json")
    ctx.autoreplies_file = autoreplies_path
    if os.path.isfile(autoreplies_path):
        ctx.autoreply_rules = autoreply.load_autoreplies(autoreplies_path, session_key)

    # highlights
    highlights_path = os.path.join(config_dir, "highlights.json")
    ctx.highlights_file = highlights_path
    if os.path.isfile(highlights_path):
        ctx.highlight_rules = highlighter.load_highlights(highlights_path, session_key)

    # progress bars
    progressbars_path = os.path.join(config_dir, "progressbars.json")
    ctx.progressbars_file = progressbars_path
    if os.path.isfile(progressbars_path):
        ctx.progressbar_configs = progressbars.load_progressbars(progressbars_path, session_key)

    # GMCP snapshot
    ctx.gmcp_snapshot_file = paths.gmcp_snapshot_path(session_key)

    # chat
    chat_file = paths.chat_path(session_key)
    ctx.chat_file = chat_file
    if os.path.isfile(chat_file):
        ctx.chat_messages = chat.load_chat(chat_file)

    ctx.on_chat_text = lambda data: chat.append_chat_msg(ctx, data)
    ctx.on_chat_channels = lambda data: setattr(ctx, "chat_channels", data)

    # rooms
    rooms_file = rooms.rooms_path(session_key)
    ctx.rooms_file = rooms_file
    ctx.current_room_file = rooms.current_room_path(session_key)
    ctx.room_graph = rooms.RoomStore(rooms_file)

    def on_room_info(data: dict[str, typing.Any]) -> None:
        num = str(data["num"])
        ctx.previous_room_num = ctx.current_room_num
        ctx.current_room_num = num
        ctx.room_changed.set()
        ctx.room_changed.clear()
        ctx.room_graph.update_room(data)
        rooms.write_current_room(ctx.current_room_file, num)

    ctx.on_room_info = on_room_info

    # history
    ctx.history_file = paths.history_path(session_key)


def want_repl(
    ctx: session_context.SessionContext,
    writer: (telnetlib3.stream_writer.TelnetWriter | telnetlib3.stream_writer.TelnetWriterUnicode),
) -> bool:
    """Return True when the REPL should be active."""
    return ctx.repl_enabled and getattr(writer, "mode", "local") == "local"


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
    session_key = build_session_key(telnet_writer)
    old_ctx = telnet_writer.ctx
    ctx = session_context.SessionContext(session_key=session_key)
    ctx.typescript_file = old_ctx.typescript_file
    ctx.raw_mode = old_ctx.raw_mode
    ctx.ascii_eol = old_ctx.ascii_eol
    ctx.color_filter = old_ctx.color_filter
    ctx.input_filter = old_ctx.input_filter
    ctx.writer = telnet_writer
    ctx.repl_enabled = True
    telnet_writer.ctx = ctx

    # 2. Load per-session configs.
    load_configs(ctx)

    # 3. Override GMCP callback to dispatch ctx hooks after base storage.
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

    keyboard_escape = "\x1d"

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
            telnetlib3.client_shell._flush_color_filter(telnet_writer, stdout)
            stdout.write(f"\033[m{linesep}{msg}{linesep}".encode())
            tty_shell.cleanup_winch()

        def check_want_repl() -> bool:
            return want_repl(ctx, telnet_writer)

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
            await telnetlib3.client_shell._raw_event_loop(
                telnet_reader,
                telnet_writer,
                tty_shell,
                stdin,
                stdout,
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
