"""
WebSocket client for telix.

Provides :func:`run_ws_client` and :func:`build_parser`, called by
:func:`telix.main.main` when a ``ws://`` or ``wss://`` URL is given on the
command line.  Connects via ``websockets.connect()`` using the
``gmcp.mudstandards.org`` or ``telnet.mudstandards.org`` subprotocol.

For the ``telnet.mudstandards.org`` subprotocol, a real
:class:`telnetlib3.client_base.TelnetClient` is run on top of a
:class:`~telix.ws_transport.WSTelnetTransport`, giving full IAC option
negotiation and all telnet-specific CLI flags.

For the ``gmcp.mudstandards.org`` subprotocol, lightweight
:class:`~telix.ws_transport.WebSocketReader` / :class:`~telix.ws_transport.WebSocketWriter`
adapters are used instead.
"""

import sys
import asyncio
import logging
import argparse
import urllib.parse

import websockets
import telnetlib3.client
import telnetlib3.telopt
import websockets.typing
import websockets.exceptions
import telnetlib3.accessories
import telnetlib3.client_base
import websockets.asyncio.client
import telnetlib3._session_context

from . import ws_transport

log = logging.getLogger(__name__)

GMCP_SUBPROTOCOL = "gmcp.mudstandards.org"
TELNET_SUBPROTOCOL = "telnet.mudstandards.org"
WS_SUBPROTOCOLS = [websockets.typing.Subprotocol(GMCP_SUBPROTOCOL), websockets.typing.Subprotocol(TELNET_SUBPROTOCOL)]  # type: ignore[attr-defined]


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
    raw_mode: "bool | None" = None,
    ansi_keys: bool = False,
    ascii_eol: bool = False,
    always_do: "set[bytes] | None" = None,
    always_will: "set[bytes] | None" = None,
    always_dont: "set[bytes] | None" = None,
    always_wont: "set[bytes] | None" = None,
    term: str = "",
    speed: int = 38400,
    send_environ: "tuple[str, ...] | None" = None,
    gmcp_modules: "list[str] | None" = None,
    connect_minwait: float = 0.0,
    connect_maxwait: float = 4.0,
    connect_timeout: "float | None" = None,
    compression: "bool | None" = None,
) -> None:
    """
    Connect to a WebSocket MUD server and run the telix shell.

    When the server negotiates the ``telnet.mudstandards.org`` subprotocol,
    a real :class:`~telnetlib3.client_base.TelnetClient` is run on top of a
    :class:`~telix.ws_transport.WSTelnetTransport`, enabling full IAC option
    negotiation and all telnet-specific flags.

    When the ``gmcp.mudstandards.org`` subprotocol is used instead,
    lightweight :class:`~telix.ws_transport.WebSocketReader` /
    :class:`~telix.ws_transport.WebSocketWriter` adapters are used.

    :param url: WebSocket URL (e.g. ``wss://gel.monster:8443``).
    :param shell: Dotted path to the GMCP-subprotocol shell coroutine.
    :param no_repl: If ``True``, skip the interactive REPL (raw I/O only).
    :param loglevel: Logging level name (default ``"warn"``).
    :param logfile: Optional path to write log output.
    :param typescript: Optional path to record session transcript.
    :param logfile_mode: ``"append"`` (default) or ``"rewrite"`` the log file.
    :param typescript_mode: ``"append"`` (default) or ``"rewrite"`` the typescript file.
    :param encoding: Character encoding name (default ``"utf-8"``).
    :param encoding_errors: Error handler for encoding (default ``"replace"``).
    :param raw_mode: ``True`` = force raw (BBS) mode, ``False`` = force line mode,
        ``None`` = auto-detect from server negotiation.
    :param ansi_keys: If ``True``, transmit raw ANSI escape sequences for arrow/function
        keys instead of translating them.
    :param ascii_eol: If ``True``, use ASCII CR/LF instead of encoding-native EOL.
    :param always_do: Telnet options to always send DO for.
    :param always_will: Telnet options to always send WILL for.
    :param always_dont: Telnet options to always send DONT for.
    :param always_wont: Telnet options to always send WONT for.
    :param term: Terminal type string (default: ``$TERM``).
    :param speed: Terminal speed (default: 38400).
    :param send_environ: Environment variable names to send via NEW-ENVIRON.
    :param gmcp_modules: GMCP module names to request via ``Core.Supports.Set``.
    :param connect_minwait: Minimum wait (seconds) before declaring negotiation done.
    :param connect_maxwait: Maximum wait (seconds) for negotiation to complete.
    :param connect_timeout: WebSocket open timeout in seconds (default: 10).
    :param compression: ``True`` to request MCCP, ``False`` to refuse, ``None`` for auto.
    """
    if logfile:
        telnetlib3.accessories.make_logger(
            name="telix", loglevel=loglevel, logfile=logfile, filemode="w" if logfile_mode == "rewrite" else "a"
        )
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)

    open_timeout = connect_timeout if connect_timeout is not None else 10

    async with websockets.connect(url, subprotocols=WS_SUBPROTOCOLS, max_size=2**20, open_timeout=open_timeout) as ws:
        if ws.subprotocol not in (GMCP_SUBPROTOCOL, TELNET_SUBPROTOCOL):
            log.warning("server did not negotiate a known subprotocol (got %r), GMCP may not work", ws.subprotocol)

        # Sniff the first frame to detect servers that send raw IAC negotiation over the
        # GMCP subprotocol (spec violation but common in practice, e.g. BBS servers).
        first_msg = None
        try:
            first_msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            log.debug("no initial frame received within 1 second, defaulting to GMCP path")

        iac_detected = isinstance(first_msg, bytes) and b"\xff" in first_msg
        use_telnet_engine = ws.subprotocol == TELNET_SUBPROTOCOL or iac_detected

        if use_telnet_engine:
            log.info("telnet engine selected (subprotocol=%r, iac_detected=%r)", ws.subprotocol, iac_detected)
            await _run_telnet_over_ws(
                ws=ws,
                first_msg=first_msg,
                host=host,
                port=port,
                encoding=encoding,
                encoding_errors=encoding_errors,
                term=term or "",
                speed=speed,
                send_environ=send_environ or (),
                gmcp_modules=gmcp_modules,
                shell="telix.client_shell.telix_client_shell",
                connect_minwait=connect_minwait,
                connect_maxwait=connect_maxwait,
                compression=compression,
                raw_mode=raw_mode,
                no_repl=no_repl,
                ansi_keys=ansi_keys,
                ascii_eol=ascii_eol,
                always_do=always_do or set(),
                always_will=always_will or set(),
                always_dont=always_dont or set(),
                always_wont=always_wont or set(),
                typescript=typescript,
                typescript_mode=typescript_mode,
            )
        else:
            await _run_gmcp_ws(
                ws=ws,
                first_msg=first_msg,
                host=host,
                port=port,
                encoding=encoding,
                encoding_errors=encoding_errors,
                no_repl=no_repl,
                raw_mode=raw_mode,
                ansi_keys=ansi_keys,
                ascii_eol=ascii_eol,
                typescript=typescript,
                typescript_mode=typescript_mode,
                shell=shell,
            )


async def _run_telnet_over_ws(
    ws: "websockets.asyncio.client.ClientConnection",
    host: str,
    port: int,
    encoding: str,
    encoding_errors: str,
    term: str,
    speed: int,
    send_environ: "tuple[str, ...]",
    gmcp_modules: "list[str] | None",
    shell: str,
    connect_minwait: float,
    connect_maxwait: float,
    compression: "bool | None",
    raw_mode: "bool | None",
    no_repl: bool,
    ansi_keys: bool,
    ascii_eol: bool,
    always_do: "set[bytes]",
    always_will: "set[bytes]",
    always_dont: "set[bytes]",
    always_wont: "set[bytes]",
    typescript: str,
    typescript_mode: str,
    first_msg: "bytes | str | None" = None,
) -> None:
    """
    Run a telnet session over the ``telnet.mudstandards.org`` WebSocket subprotocol.

    Creates a real :class:`~telnetlib3.client_base.TelnetClient` on top of a
    :class:`~telix.ws_transport.WSTelnetTransport`, giving full IAC negotiation.
    Uses :func:`~telix.client_shell.telix_client_shell` as the shell.
    """
    shell_fn = telnetlib3.accessories.function_lookup(shell)
    raw_mode_val = True if no_repl else raw_mode

    async def wrapped_shell(reader: "object", writer: "object") -> None:
        ctx = writer.ctx  # type: ignore[attr-defined]
        ctx.raw_mode = raw_mode_val
        ctx.ansi_keys = ansi_keys
        if typescript:
            ctx.typescript_file = open(typescript, "w" if typescript_mode == "rewrite" else "a", encoding="utf-8")
        try:
            await shell_fn(reader, writer)
        finally:
            if ctx.typescript_file is not None:
                ctx.typescript_file.close()
                ctx.typescript_file = None

    # Build telnetlib3 client with all connection options.
    client_cls = (
        telnetlib3.client.TelnetClient
        if sys.platform == "win32" or not sys.stdin.isatty()
        else telnetlib3.client.TelnetTerminalClient
    )
    client = client_cls(
        encoding=encoding,
        encoding_errors=encoding_errors,
        force_binary=True,
        term=term or "unknown",
        tspeed=(speed, speed),
        shell=wrapped_shell,
        connect_minwait=connect_minwait,
        connect_maxwait=connect_maxwait,
        compression=compression,
        send_environ=send_environ,
    )

    # Patch connection_made to set always_do / always_will / gmcp_modules.
    orig_connection_made = client.connection_made

    def patched_connection_made(transport: "object") -> None:
        orig_connection_made(transport)  # type: ignore[arg-type]
        writer = client.writer
        if writer is None:
            return
        writer.always_will = always_will
        writer.always_do = always_do
        writer.always_wont = always_wont  # type: ignore[attr-defined]
        writer.always_dont = always_dont  # type: ignore[attr-defined]
        writer.passive_do = {telnetlib3.telopt.GMCP}
        writer._encoding_explicit = encoding not in ("utf8", "utf-8", False)
        if gmcp_modules:
            writer.ctx.gmcp_modules = gmcp_modules  # type: ignore[attr-defined]

    client.connection_made = patched_connection_made  # type: ignore[method-assign]

    # Create transport backed by a send queue; drain loop sends to WebSocket.
    send_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    extra: dict[str, object] = {"peername": (host, port), "ssl_object": None}
    transport = ws_transport.WSTelnetTransport(send_queue, extra)

    async def drain_loop() -> None:
        while True:
            item = await send_queue.get()
            if item is None:
                break
            try:
                await ws.send(item)
            except websockets.exceptions.ConnectionClosed:
                break

    def _dispatch_ws_message(message: "bytes | str") -> None:
        if isinstance(message, bytes):
            client.data_received(message)
        elif isinstance(message, str):
            try:
                pkg, data = ws_transport.parse_gmcp_frame(message)
                if client.writer is not None:
                    cb = client.writer._ext_callback.get(telnetlib3.telopt.GMCP)
                    if cb:
                        cb(telnetlib3.telopt.GMCP, pkg, data)
            except ValueError:
                log.warning("invalid GMCP frame in WS telnet session: %r", message[:80])

    async def receive_loop() -> None:
        try:
            if first_msg is not None:
                _dispatch_ws_message(first_msg)
            async for message in ws:
                _dispatch_ws_message(message)
        except websockets.exceptions.ConnectionClosed:
            log.debug("WS telnet connection closed")
        finally:
            client.eof_received()

    client.connection_made(transport)
    drain_task = asyncio.ensure_future(drain_loop())
    recv_task = asyncio.ensure_future(receive_loop())

    try:
        await client.waiter_closed
    finally:
        transport.close()
        recv_task.cancel()
        results = await asyncio.gather(recv_task, drain_task, return_exceptions=True)
        for result in results:
            if isinstance(result, (asyncio.CancelledError, websockets.exceptions.ConnectionClosed)):
                continue
            if isinstance(result, Exception):
                log.exception("unexpected exception during WS telnet shutdown", exc_info=result)


async def _run_gmcp_ws(
    ws: "websockets.asyncio.client.ClientConnection",
    host: str,
    port: int,
    encoding: str,
    encoding_errors: str,
    no_repl: bool,
    raw_mode: "bool | None",
    ansi_keys: bool,
    ascii_eol: bool,
    typescript: str,
    typescript_mode: str,
    shell: str,
    first_msg: "bytes | str | None" = None,
) -> None:
    """
    Run a session over the ``gmcp.mudstandards.org`` WebSocket subprotocol.

    Uses lightweight :class:`~telix.ws_transport.WebSocketReader` /
    :class:`~telix.ws_transport.WebSocketWriter` adapters and
    :func:`~telix.client_shell.ws_client_shell`.
    """
    reader = ws_transport.WebSocketReader(encoding=encoding, encoding_errors=encoding_errors)
    writer = ws_transport.WebSocketWriter(ws, peername=(host, port), encoding=encoding)

    # Create a faux telnet context; no IAC negotiation occurs over this WebSocket subprotocol.
    # ws_client_shell will replace this with a TelixSessionContext and call load_configs().
    writer.ctx = telnetlib3._session_context.TelnetSessionContext()  # type: ignore[assignment]
    writer.ctx.raw_mode = raw_mode
    writer.ctx.no_repl = no_repl  # type: ignore[attr-defined]
    writer.ctx.ascii_eol = ascii_eol
    writer.ctx.ansi_keys = ansi_keys

    # Mode-based echo default: BBS (no_repl or raw mode) = server echoes; MUD = local echo.
    writer.will_echo = no_repl or raw_mode is True

    iac_remainder = b""

    def _process_message(message: "bytes | str") -> None:
        nonlocal iac_remainder
        if isinstance(message, bytes):
            clean, events, iac_remainder = ws_transport.extract_iac(message, iac_remainder)
            for event in events:
                if event[0] == "will" and event[1] == telnetlib3.telopt.ECHO:
                    writer.will_echo = True
                elif event[0] in ("dont", "wont") and event[1] == telnetlib3.telopt.ECHO:
                    writer.will_echo = False
            if clean:
                reader.feed_data(clean)
                writer.fire_prompt_signal()
        elif isinstance(message, str):
            try:
                pkg, data = ws_transport.parse_gmcp_frame(message)
                writer.dispatch_gmcp(pkg, data)
            except ValueError:
                log.warning("invalid GMCP frame: %r", message[:80])

    async def receive_loop() -> None:
        if typescript:
            writer.ctx.typescript_file = open(
                typescript, "w" if typescript_mode == "rewrite" else "a", encoding="utf-8"
            )
        try:
            if first_msg is not None:
                _process_message(first_msg)
            async for message in ws:
                _process_message(message)
        except websockets.exceptions.ConnectionClosed:
            log.debug("WebSocket connection closed")
        finally:
            reader.feed_eof()
            if writer.ctx.typescript_file is not None:
                writer.ctx.typescript_file.close()
                writer.ctx.typescript_file = None

    shell_fn = telnetlib3.accessories.function_lookup(shell)
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
        "--always-do",
        action="append",
        default=[],
        metavar="OPT",
        dest="always_do",
        help="always send DO for this option (comma-separated, named like GMCP)",
    )
    conn.add_argument(
        "--always-dont",
        action="append",
        default=[],
        metavar="OPT",
        dest="always_dont",
        help="always send DONT for this option, refusing even natively supported",
    )
    conn.add_argument(
        "--always-will",
        action="append",
        default=[],
        metavar="OPT",
        dest="always_will",
        help="always send WILL for this option (comma-separated, named like MXP)",
    )
    conn.add_argument(
        "--always-wont",
        action="append",
        default=[],
        metavar="OPT",
        dest="always_wont",
        help="always send WONT for this option, refusing even natively supported",
    )
    conn.add_argument("--compression", action="store_true", default=False, help="request MCCP compression")
    conn.add_argument(
        "--no-compression", action="store_true", default=False, dest="no_compression", help="refuse MCCP compression"
    )
    conn.add_argument(
        "--connect-maxwait",
        type=float,
        default=4.0,
        dest="connect_maxwait",
        metavar="N",
        help="timeout for pending negotiation (default: 4.0)",
    )
    conn.add_argument(
        "--connect-minwait",
        type=float,
        default=0.0,
        dest="connect_minwait",
        metavar="N",
        help="shell delay before declaring negotiation done (default: 0)",
    )
    conn.add_argument(
        "--connect-timeout",
        type=float,
        default=None,
        dest="connect_timeout",
        metavar="N",
        help="timeout for WebSocket connection in seconds (default: 10)",
    )
    conn.add_argument("--encoding", default="utf-8", help="encoding name (default: utf-8)")
    conn.add_argument(
        "--encoding-errors",
        default="replace",
        dest="encoding_errors",
        choices=["replace", "ignore", "strict"],
        help="handler for encoding errors (default: replace)",
    )
    conn.add_argument("--logfile", default="", metavar="FILE", help="write log to FILE")
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
        "--raw-mode", action="store_true", default=False, dest="raw_mode", help="force raw (BBS) mode input"
    )
    conn.add_argument("--line-mode", action="store_true", default=False, dest="line_mode", help="force line mode input")
    conn.add_argument(
        "--ansi-keys",
        action="store_true",
        default=False,
        dest="ansi_keys",
        help="transmit raw ANSI escape sequences for arrow/function keys",
    )
    conn.add_argument(
        "--ascii-eol",
        action="store_true",
        default=False,
        dest="ascii_eol",
        help="use ASCII CR/LF instead of encoding-native EOL",
    )
    conn.add_argument(
        "--gmcp-modules",
        default=None,
        metavar="MODULES",
        dest="gmcp_modules",
        help="comma-separated list of GMCP modules to request",
    )
    conn.add_argument(
        "--send-environ",
        default="",
        metavar="VARS",
        dest="send_environ",
        help="comma-separated environment variables to send via NEW-ENVIRON",
    )
    conn.add_argument(
        "--shell",
        default="telix.client_shell.ws_client_shell",
        help="dotted path to shell coroutine (default: telix WS shell)",
    )
    conn.add_argument("--speed", type=int, default=38400, metavar="N", help="terminal speed to report (default: 38400)")
    conn.add_argument("--term", default="", metavar="TERM", help="terminal type to negotiate (default: $TERM)")
    conn.add_argument("--typescript", default="", metavar="FILE", help="record session to FILE")
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
