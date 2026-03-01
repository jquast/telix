"""
Telix client shell — wraps telnetlib3's terminal handling with REPL support.

Provides :func:`telix_client_shell`, a drop-in replacement for
:func:`telnetlib3.client_shell.telnet_client_shell` that creates a
:class:`~telix.session_context.SessionContext`, loads per-session configs
(macros, autoreplies, highlights, chat, rooms), and alternates between
REPL and raw event loops based on telnet negotiation state.
"""

from __future__ import annotations

# std imports
import os
import asyncio
import logging
from typing import Union

# 3rd party
from telnetlib3.client_shell import (
    Terminal,
    _get_raw_mode,
    _RawLoopState,
    _raw_event_loop,
    _flush_color_filter,
)
from telnetlib3.stream_reader import TelnetReader, TelnetReaderUnicode
from telnetlib3.stream_writer import TelnetWriter, TelnetWriterUnicode

# local
from . import _paths
from .client_repl import repl_event_loop
from .session_context import SessionContext

log = logging.getLogger(__name__)

__all__ = ("telix_client_shell",)


def _build_session_key(writer: Union[TelnetWriter, TelnetWriterUnicode]) -> str:
    """
    Derive ``host:port`` session key from CLI arguments or peername.

    Prefers the original hostname from ``sys.argv`` over the resolved IP
    from :func:`socket.getpeername`, so that session-specific files
    (history, rooms, macros, etc.) are keyed by the human-readable
    hostname used to connect.
    """
    import sys

    try:
        from telnetlib3.client import _get_argument_parser

        args = _get_argument_parser().parse_known_args(sys.argv[1:])[0]
        if args.host:
            return f"{args.host}:{args.port}"
    except (SystemExit, Exception):
        pass
    peername = writer.get_extra_info("peername")
    if peername:
        return f"{peername[0]}:{peername[1]}"
    return ""


def _load_configs(ctx: SessionContext) -> None:
    """
    Load per-session config files into *ctx*.

    Missing config files are silently skipped so first-time connections start with empty defaults.
    """
    session_key = ctx.session_key
    config_dir = str(_paths.xdg_config_dir())
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(str(_paths.xdg_data_dir()), exist_ok=True)

    # macros
    macros_path = os.path.join(config_dir, "macros.json")
    ctx.macros_file = macros_path
    if os.path.isfile(macros_path):
        from .macros import load_macros

        ctx.macro_defs = load_macros(macros_path, session_key)

    # autoreplies
    autoreplies_path = os.path.join(config_dir, "autoreplies.json")
    ctx.autoreplies_file = autoreplies_path
    if os.path.isfile(autoreplies_path):
        from .autoreply import load_autoreplies

        ctx.autoreply_rules = load_autoreplies(autoreplies_path, session_key)

    # highlights
    highlights_path = os.path.join(config_dir, "highlights.json")
    ctx.highlights_file = highlights_path
    if os.path.isfile(highlights_path):
        from .highlighter import load_highlights

        ctx.highlight_rules = load_highlights(highlights_path, session_key)

    # progress bars
    progressbars_path = os.path.join(config_dir, "progressbars.json")
    ctx.progressbars_file = progressbars_path
    if os.path.isfile(progressbars_path):
        from .progressbars import load_progressbars

        ctx.progressbar_configs = load_progressbars(progressbars_path, session_key)

    # GMCP snapshot
    ctx.gmcp_snapshot_file = _paths.gmcp_snapshot_path(session_key)

    # chat
    chat_file = _paths.chat_path(session_key)
    ctx.chat_file = chat_file
    if os.path.isfile(chat_file):
        from .chat import load_chat

        ctx.chat_messages = load_chat(chat_file)

    from .chat import append_chat_msg

    ctx.on_chat_text = lambda data: append_chat_msg(ctx, data)
    ctx.on_chat_channels = lambda data: setattr(ctx, "chat_channels", data)

    # rooms
    from .rooms import rooms_path

    rooms_file = rooms_path(session_key)
    ctx.rooms_file = rooms_file

    # history
    ctx.history_file = _paths.history_path(session_key)


def _want_repl(ctx: SessionContext, writer: Union[TelnetWriter, TelnetWriterUnicode]) -> bool:
    """Return True when the REPL should be active."""
    return ctx.repl_enabled and getattr(writer, "mode", "local") == "local"


async def telix_client_shell(
    telnet_reader: Union[TelnetReader, TelnetReaderUnicode],
    telnet_writer: Union[TelnetWriter, TelnetWriterUnicode],
) -> None:
    """
    Telix client shell with REPL/raw mode switching.

    Drop-in replacement for :func:`telnetlib3.client_shell.telnet_client_shell`.
    Creates a :class:`SessionContext`, loads configs, and runs an outer loop
    that alternates between the REPL (line-mode) and raw event loop based on
    telnet negotiation state.

    :param telnet_reader: Server-side telnet reader stream.
    :param telnet_writer: Client-side telnet writer stream.
    """
    # 1. Build SessionContext and attach to writer, preserving attributes
    #    that run_client() wrappers already set on the original ctx.
    session_key = _build_session_key(telnet_writer)
    _old_ctx = telnet_writer.ctx
    ctx = SessionContext(session_key=session_key)
    ctx.typescript_file = _old_ctx.typescript_file
    ctx.raw_mode = _old_ctx.raw_mode
    ctx.ascii_eol = _old_ctx.ascii_eol
    ctx.color_filter = _old_ctx.color_filter
    ctx.input_filter = _old_ctx.input_filter
    ctx.writer = telnet_writer
    ctx.repl_enabled = True
    telnet_writer.ctx = ctx

    # 2. Load per-session configs.
    _load_configs(ctx)

    keyboard_escape = "\x1d"

    with Terminal(telnet_writer=telnet_writer) as tty_shell:
        linesep = "\n"
        switched_to_raw = False
        last_will_echo = False
        local_echo = tty_shell.software_echo
        if tty_shell._istty:
            raw_mode = _get_raw_mode(telnet_writer)
            if telnet_writer.will_echo or raw_mode is True:
                linesep = "\r\n"
        stdout = await tty_shell.make_stdout()  # pylint: disable=no-member
        tty_shell.setup_winch()

        # EOR/GA-based command pacing for raw-mode autoreplies.
        prompt_ready_raw = asyncio.Event()
        prompt_ready_raw.set()
        ga_detected_raw = False

        def _on_prompt_signal_raw(_cmd: bytes) -> None:
            nonlocal ga_detected_raw
            ga_detected_raw = True
            prompt_ready_raw.set()
            ar = ctx.autoreply_engine
            if ar is not None:
                ar.on_prompt()

        from telnetlib3.telopt import GA, CMD_EOR

        telnet_writer.set_iac_callback(GA, _on_prompt_signal_raw)
        telnet_writer.set_iac_callback(CMD_EOR, _on_prompt_signal_raw)

        async def _wait_for_prompt_raw() -> None:
            if not ga_detected_raw:
                return
            try:
                await asyncio.wait_for(prompt_ready_raw.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            prompt_ready_raw.clear()

        ctx.autoreply_wait_fn = _wait_for_prompt_raw

        from telnetlib3 import accessories

        escape_name = accessories.name_unicode(keyboard_escape)
        banner_sep = "\r\n" if tty_shell._istty else linesep
        stdout.write(f"Escape character is '{escape_name}'.{banner_sep}".encode())

        def _handle_close(msg: str) -> None:
            _flush_color_filter(telnet_writer, stdout)
            stdout.write(f"\033[m{linesep}{msg}{linesep}".encode())
            tty_shell.cleanup_winch()

        def _check_want_repl() -> bool:
            return _want_repl(ctx, telnet_writer)

        # Outer loop: alternate between REPL and raw modes.
        while True:
            if _check_want_repl() and tty_shell._istty:
                mode_switched = await repl_event_loop(
                    telnet_reader, telnet_writer, tty_shell, stdout, history_file=ctx.history_file
                )
                if not mode_switched:
                    # Connection closed normally.
                    break
                # Server switched to kludge/raw mode — fall through to raw loop.

            # Raw event loop.
            if not switched_to_raw and tty_shell._istty and tty_shell._save_mode is not None:
                tty_shell.set_mode(tty_shell._make_raw(tty_shell._save_mode, suppress_echo=True))
                switched_to_raw = True
                local_echo = not telnet_writer.will_echo
                linesep = "\r\n"
            stdin = await tty_shell.connect_stdin()  # pylint: disable=no-member
            state = _RawLoopState(
                switched_to_raw=switched_to_raw,
                last_will_echo=last_will_echo,
                local_echo=local_echo,
                linesep=linesep,
            )
            await _raw_event_loop(
                telnet_reader,
                telnet_writer,
                tty_shell,
                stdin,
                stdout,
                keyboard_escape,
                state,
                _handle_close,
                _check_want_repl,
            )
            tty_shell.disconnect_stdin(stdin)  # pylint: disable=no-member
            # Carry forward state from the raw loop.
            switched_to_raw = state.switched_to_raw
            last_will_echo = state.last_will_echo
            local_echo = state.local_echo
            linesep = state.linesep
            if state.reactivate_repl and _check_want_repl():
                # Server returned to line mode — loop back to REPL.
                continue
            # Connection closed.
            break

        ctx.close()
