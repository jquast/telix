"""
WebSocket client entry point for telix.

Provides :func:`main`, the ``telix-ws`` console script entry point, and
:func:`run_ws_client`, which connects to a WebSocket MUD server using
the ``gmcp.mudstandards.org`` subprotocol, creates reader/writer
adapters, runs the receive loop, and invokes the telix shell.

This module mirrors the role of ``telnetlib3.client`` but for WebSocket
connections.  The TUI launches it as a subprocess (the same way it
launches ``telnetlib3-client`` for telnet sessions).
"""

import sys
import asyncio
import logging
import argparse
import urllib.parse

import websockets
import websockets.typing
import websockets.exceptions
import telnetlib3.accessories
import telnetlib3._session_context

from . import ws_transport

log = logging.getLogger(__name__)

WS_SUBPROTOCOL = "gmcp.mudstandards.org"


async def run_ws_client(
    url: str, shell: str = "telix.client_shell.ws_client_shell", no_repl: bool = False
) -> None:
    """
    Connect to a WebSocket MUD server and run the telix shell.

    :param url: WebSocket URL (e.g. ``wss://gel.monster:8443``).
    :param shell: Dotted path to the shell coroutine.
    :param no_repl: If ``True``, skip the interactive REPL (raw I/O only).
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)

    reader = ws_transport.WebSocketReader()
    writer: ws_transport.WebSocketWriter | None = None

    shell_fn = telnetlib3.accessories.function_lookup(shell)

    async with websockets.connect(
        url, subprotocols=[websockets.typing.Subprotocol(WS_SUBPROTOCOL)], max_size=2**20, open_timeout=10
    ) as ws:
        if ws.subprotocol != WS_SUBPROTOCOL:
            log.warning(
                "server did not negotiate %s subprotocol (got %r), GMCP may not work", WS_SUBPROTOCOL, ws.subprotocol
            )
        writer = ws_transport.WebSocketWriter(ws, peername=(host, port))

        writer.ctx = telnetlib3._session_context.TelnetSessionContext()
        writer.ctx.no_repl = no_repl

        async def receive_loop() -> None:
            """Read WebSocket frames and dispatch to reader/writer."""
            assert writer is not None
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        reader.feed_data(message)
                        writer.fire_prompt_signal()
                    elif isinstance(message, str):
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
    """Build the argument parser for ``telix-ws``."""
    parser = argparse.ArgumentParser(prog="telix-ws", description="Connect to a WebSocket MUD server.")
    parser.add_argument("url", help="WebSocket URL (e.g. wss://gel.monster:8443)")
    parser.add_argument(
        "--shell",
        default="telix.client_shell.ws_client_shell",
        help="Dotted path to shell coroutine (default: telix WS shell).",
    )
    parser.add_argument(
        "--no-repl",
        action="store_true",
        default=False,
        dest="no_repl",
        help="Disable the interactive REPL (raw I/O only).",
    )
    return parser


def main() -> None:
    """Entry point for the ``telix-ws`` console script."""
    parser = build_parser()
    args = parser.parse_args()

    try:
        asyncio.run(run_ws_client(url=args.url, shell=args.shell, no_repl=args.no_repl))
    except KeyboardInterrupt:
        pass
    except OSError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
