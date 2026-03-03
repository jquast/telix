"""
WebSocket client for telix.

Provides :func:`run_ws_client` and :func:`build_parser`, called by
:func:`telix.main.main` when a ``ws://`` or ``wss://`` URL is given on the
command line.  Connects via ``websockets.connect()`` using the
``gmcp.mudstandards.org`` subprotocol, creates reader/writer adapters, runs
the receive loop, and invokes the telix shell.
"""

import asyncio
import logging
import argparse
import urllib.parse

import websockets
import websockets.typing
import websockets.exceptions
import telnetlib3.telopt
import telnetlib3.accessories
import telnetlib3._session_context

from . import ws_transport

log = logging.getLogger(__name__)

GMCP_SUBPROTOCOL = "gmcp.mudstandards.org"
TELNET_SUBPROTOCOL = "telnet.mudstandards.org"
WS_SUBPROTOCOLS = [
    websockets.typing.Subprotocol(GMCP_SUBPROTOCOL),
    websockets.typing.Subprotocol(TELNET_SUBPROTOCOL),
]


async def run_ws_client(
    url: str,
    shell: str = "telix.client_shell.ws_client_shell",
    no_repl: bool = False,
    loglevel: str = "warn",
    logfile: str = "",
    typescript: str = "",
    logfile_mode: str = "append",
    typescript_mode: str = "append",
    encoding: str = "utf-8",
    encoding_errors: str = "replace",
) -> None:
    """
    Connect to a WebSocket MUD server and run the telix shell.

    :param url: WebSocket URL (e.g. ``wss://gel.monster:8443``).
    :param shell: Dotted path to the shell coroutine.
    :param no_repl: If ``True``, skip the interactive REPL (raw I/O only).
    :param loglevel: Logging level name (default ``"warn"``).
    :param logfile: Optional path to write log output.
    :param typescript: Optional path to record session transcript.
    :param logfile_mode: ``"append"`` (default) or ``"rewrite"`` the log file.
    :param typescript_mode: ``"append"`` (default) or ``"rewrite"`` the typescript file.
    :param encoding: Character encoding name (default ``"utf-8"``).
    :param encoding_errors: Error handler for encoding (default ``"replace"``).
    """
    if logfile:
        telnetlib3.accessories.make_logger(
            name="telix",
            loglevel=loglevel,
            logfile=logfile,
            filemode="w" if logfile_mode == "rewrite" else "a",
        )
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)

    reader = ws_transport.WebSocketReader(
        encoding=encoding, encoding_errors=encoding_errors
    )
    writer: ws_transport.WebSocketWriter | None = None

    shell_fn = telnetlib3.accessories.function_lookup(shell)

    async with websockets.connect(
        url, subprotocols=WS_SUBPROTOCOLS, max_size=2**20, open_timeout=10
    ) as ws:
        is_telnet_subprotocol = ws.subprotocol == TELNET_SUBPROTOCOL
        if ws.subprotocol not in (GMCP_SUBPROTOCOL, TELNET_SUBPROTOCOL):
            log.warning(
                "server did not negotiate a known subprotocol (got %r), "
                "GMCP may not work",
                ws.subprotocol,
            )
        writer = ws_transport.WebSocketWriter(
            ws, peername=(host, port), encoding=encoding
        )

        writer.ctx = telnetlib3._session_context.TelnetSessionContext()
        writer.ctx.no_repl = no_repl
        writer.ctx.typescript_path = typescript
        writer.ctx.typescript_mode = typescript_mode
        writer.ctx.encoding = encoding

        if is_telnet_subprotocol:
            log.info("telnet subprotocol negotiated, IAC processing enabled")
        else:
            # Mode-based echo default for GMCP / no subprotocol:
            # BBS (no_repl) = server echoes; MUD (repl) = local echo.
            writer.will_echo = no_repl

        iac_remainder = b""

        async def receive_loop() -> None:
            """Read WebSocket frames and dispatch to reader/writer."""
            nonlocal iac_remainder
            assert writer is not None
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        if is_telnet_subprotocol:
                            clean, events, iac_remainder = (
                                ws_transport.extract_iac(message, iac_remainder)
                            )
                            if clean:
                                reader.feed_data(clean)
                            for event in events:
                                kind = event[0]
                                if kind == "will":
                                    option = event[1]
                                    if option == telnetlib3.telopt.ECHO:
                                        writer.will_echo = True
                                        writer.write_iac(
                                            telnetlib3.telopt.DO, option
                                        )
                                    else:
                                        writer.write_iac(
                                            telnetlib3.telopt.DONT, option
                                        )
                                elif kind == "wont":
                                    option = event[1]
                                    if option == telnetlib3.telopt.ECHO:
                                        writer.will_echo = False
                                    writer.write_iac(
                                        telnetlib3.telopt.DONT, option
                                    )
                                elif kind == "do":
                                    option = event[1]
                                    writer.write_iac(
                                        telnetlib3.telopt.WONT, option
                                    )
                                elif kind == "cmd":
                                    writer.fire_prompt_signal()
                            if clean or not events:
                                writer.fire_prompt_signal()
                        else:
                            reader.feed_data(message)
                            writer.fire_prompt_signal()
                    elif isinstance(message, str):
                        if is_telnet_subprotocol:
                            continue
                        try:
                            pkg, data = ws_transport.parse_gmcp_frame(message)
                            writer.dispatch_gmcp(pkg, data)
                        except ValueError:
                            log.warning("invalid GMCP frame: %r", message[:80])
            except websockets.exceptions.ConnectionClosed:
                log.debug("WebSocket connection closed")
            finally:
                reader.feed_eof()

        recv_task = asyncio.ensure_future(receive_loop())
        drain_task = asyncio.ensure_future(writer.drain())

        try:
            await shell_fn(reader, writer)
        finally:
            writer.close()
            recv_task.cancel()
            results = await asyncio.gather(recv_task, drain_task, return_exceptions=True)
            for result in results:
                if isinstance(result, (asyncio.CancelledError, websockets.exceptions.ConnectionClosed)):
                    continue
                if isinstance(result, Exception):
                    log.exception("unexpected exception during shutdown", exc_info=result)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for WebSocket connections via ``telix``."""
    parser = argparse.ArgumentParser(prog="telix", description="Connect to a WebSocket MUD server.")
    parser.add_argument("url", help="WebSocket URL (e.g. wss://gel.monster:8443)")

    conn = parser.add_argument_group("Connection options")
    conn.add_argument(
        "--encoding",
        default="utf-8",
        help="encoding name (default: utf-8)",
    )
    conn.add_argument(
        "--encoding-errors",
        default="replace",
        dest="encoding_errors",
        choices=["replace", "ignore", "strict"],
        help="handler for encoding errors (default: replace)",
    )
    conn.add_argument(
        "--logfile", default="", metavar="FILE", help="write log to FILE"
    )
    conn.add_argument(
        "--logfile-mode",
        default="append",
        choices=["append", "rewrite"],
        dest="logfile_mode",
        help="log file write mode: append (default) or rewrite",
    )
    conn.add_argument(
        "--loglevel",
        default="warn",
        choices=["trace", "debug", "info", "warn", "error", "critical"],
        help="logging level (default: warn)",
    )
    conn.add_argument(
        "--no-repl",
        action="store_true",
        default=False,
        dest="no_repl",
        help="disable the interactive REPL (raw I/O only)",
    )
    conn.add_argument(
        "--shell",
        default="telix.client_shell.ws_client_shell",
        help="dotted path to shell coroutine (default: telix WS shell)",
    )
    conn.add_argument(
        "--typescript", default="", metavar="FILE", help="record session to FILE"
    )
    conn.add_argument(
        "--typescript-mode",
        default="append",
        choices=["append", "rewrite"],
        dest="typescript_mode",
        help="typescript write mode: append (default) or rewrite",
    )

    telix = parser.add_argument_group("Telix options")
    telix.add_argument(
        "--background-color",
        default="#000000",
        dest="background_color",
        metavar="COLOR",
        help="terminal background color as #RRGGBB (default: #000000)",
    )
    telix.add_argument(
        "--color-brightness",
        type=float,
        default=1.0,
        dest="color_brightness",
        metavar="N",
        help="color brightness multiplier (default: 1.0)",
    )
    telix.add_argument(
        "--color-contrast",
        type=float,
        default=1.0,
        dest="color_contrast",
        metavar="N",
        help="color contrast multiplier (default: 1.0)",
    )
    telix.add_argument(
        "--colormatch",
        default="vga",
        metavar="PALETTE",
        help="color palette for remapping (default: vga, 'none' to disable)",
    )
    telix.add_argument(
        "--no-ice-colors",
        action="store_true",
        default=False,
        dest="no_ice_colors",
        help="disable iCE color (blink as bright background) support",
    )

    return parser
