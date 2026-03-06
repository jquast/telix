"""
WebSocket reader/writer adapters for MUD client sessions.

Provides :class:`WebSocketReader` and :class:`WebSocketWriter`, which
present a compatible interface to telnetlib3's
:class:`~telnetlib3.stream_reader.TelnetReader` and
:class:`~telnetlib3.stream_writer.TelnetWriter` so that the REPL and
shell can operate over a WebSocket transport without modification.

The ``gmcp.mudstandards.org`` wire format is used:

- **BINARY frames** carry raw ANSI/UTF-8 game text.
- **TEXT frames** carry GMCP messages in ``"Package.Name json"`` format.

Each delivered BINARY frame fires the stored GA/EOR IAC callback as a
pseudo-prompt signal, giving the REPL the same prompt boundary that
telnet provides via IAC GA / IAC EOR.
"""

import json
import typing
import asyncio
import logging
from collections.abc import Callable

import telnetlib3.telopt
import websockets.exceptions

if typing.TYPE_CHECKING:
    from telix.session_context import TelixSessionContext

log = logging.getLogger(__name__)

# IAC command bytes used as callback keys (matching telnetlib3 conventions).
GA = b"\xf9"
CMD_EOR = b"\xef"
# GMCP telopt byte used as ext callback key.
GMCP = b"\xc9"

__all__ = ("WebSocketReader", "WebSocketWriter", "extract_iac", "parse_gmcp_frame")


def parse_gmcp_frame(text: str) -> tuple[str, typing.Any]:
    """
    Parse a GMCP TEXT frame into ``(package_name, payload)``.

    The format is ``"Package.Name optional_json_payload"``.  If no payload
    is present, *payload* is ``None``.  Malformed JSON is returned as a
    raw string.

    :param text: Raw TEXT frame content.
    :returns: ``(package_name, parsed_payload)``
    :raises ValueError: If *text* is empty.
    """
    if not text:
        raise ValueError("empty GMCP frame")
    parts = text.split(" ", 1)
    package = parts[0]
    if len(parts) == 1:
        return (package, None)
    raw = parts[1]
    try:
        return (package, json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return (package, raw)


IacEvent = tuple[str, bytes] | tuple[str, bytes, bytes]
"""
Type alias for IAC events returned by :func:`extract_iac`.

Each event is a tuple whose first element is the event kind:

- ``("will", option)`` / ``("wont", option)`` / ``("do", option)`` / ``("dont", option)``
- ``("cmd", cmd_byte)``
- ``("sb", option, payload)``
"""

NEGOTIATION_CMDS = {
    telnetlib3.telopt.WILL: "will",
    telnetlib3.telopt.WONT: "wont",
    telnetlib3.telopt.DO: "do",
    telnetlib3.telopt.DONT: "dont",
}

TWO_BYTE_CMDS = {telnetlib3.telopt.GA, telnetlib3.telopt.CMD_EOR}


def extract_iac(data: bytes, remainder: bytes = b"") -> tuple[bytes, list[IacEvent], bytes]:
    """
    Extract IAC sequences from a binary telnet stream.

    Scans *data* (prepended with any *remainder* from a previous call) for IAC sequences and returns clean game data
    with the IAC bytes stripped, a list of parsed IAC events, and any trailing partial IAC sequence that should be
    passed as *remainder* to the next call.

    :param data: Raw bytes from a BINARY WebSocket frame.
    :param remainder: Leftover bytes from the previous frame.
    :returns:``(clean_data, events, new_remainder)``
    """
    buf = remainder + data
    iac_byte = telnetlib3.telopt.IAC[0]
    sb_byte = telnetlib3.telopt.SB[0]
    se_byte = telnetlib3.telopt.SE[0]

    clean = bytearray()
    events: list[IacEvent] = []
    i = 0
    length = len(buf)

    while i < length:
        if buf[i] != iac_byte:
            clean.append(buf[i])
            i += 1
            continue

        # IAC at end of buffer -- partial sequence
        if i + 1 >= length:
            return bytes(clean), events, buf[i:]

        cmd = buf[i + 1]

        # IAC IAC -- escaped 0xFF
        if cmd == iac_byte:
            clean.append(iac_byte)
            i += 2
            continue

        # IAC SB option ... IAC SE
        if cmd == sb_byte:
            if i + 2 >= length:
                return bytes(clean), events, buf[i:]
            option = bytes([buf[i + 2]])
            # scan for IAC SE
            j = i + 3
            while j < length - 1:
                if buf[j] == iac_byte and buf[j + 1] == se_byte:
                    payload = bytes(buf[i + 3 : j])
                    events.append(("sb", option, payload))
                    i = j + 2
                    break
                j += 1
            else:
                # no IAC SE found -- partial
                return bytes(clean), events, buf[i:]
            continue

        # 3-byte negotiation: IAC WILL/WONT/DO/DONT option
        neg_name = NEGOTIATION_CMDS.get(bytes([cmd]))
        if neg_name is not None:
            if i + 2 >= length:
                return bytes(clean), events, buf[i:]
            option = bytes([buf[i + 2]])
            events.append((neg_name, option))
            i += 3
            continue

        # 2-byte commands: IAC GA, IAC CMD_EOR
        if bytes([cmd]) in TWO_BYTE_CMDS:
            events.append(("cmd", bytes([cmd])))
            i += 2
            continue

        # Unknown IAC command -- skip
        i += 2

    return bytes(clean), events, b""


class WSTelnetTransport(asyncio.Transport):
    """
    Asyncio Transport that sends bytes as WebSocket binary frames.

    Writes are enqueued for delivery by a drain coroutine.  Inbound data is
    injected by calling the owning ``asyncio.Protocol``'s
    ``data_received()`` directly from the receive loop.

    :param send_queue: Queue shared with the drain coroutine.
    :param extra: Mapping for :meth:`get_extra_info` (``"peername"`` etc.).
    """

    def __init__(self, send_queue: "asyncio.Queue[bytes | None]", extra: "dict[str, object]") -> None:
        """Initialise with a shared send queue and extra-info mapping."""
        super().__init__(extra)
        self._send_queue = send_queue
        self._closing = False

    def write(self, data: bytes) -> None:
        """Enqueue *data* for sending as a binary WebSocket frame."""
        if not self._closing and data:
            self._send_queue.put_nowait(data)

    def write_eof(self) -> None:
        """No-op: WebSocket framing does not use EOF."""

    def can_write_eof(self) -> bool:
        """Return ``False``: WebSocket does not support write-EOF."""
        return False

    def close(self) -> None:
        """Signal the drain coroutine to stop."""
        if not self._closing:
            self._closing = True
            self._send_queue.put_nowait(None)

    def is_closing(self) -> bool:
        """Return ``True`` if :meth:`close` has been called."""
        return self._closing

    def set_write_buffer_limits(self, high: "int | None" = None, low: "int | None" = None) -> None:
        """No-op: no backpressure implementation."""

    def get_write_buffer_size(self) -> int:
        """Return 0: backpressure not tracked."""
        return 0

    def pause_reading(self) -> None:
        """No-op: reading is managed by the WebSocket receive loop."""

    def resume_reading(self) -> None:
        """No-op: reading is managed by the WebSocket receive loop."""


class WebSocketReader:
    """
    Async reader fed by WebSocket BINARY frames.

    Presents the same ``read()`` / ``at_eof()`` interface as
    :class:`~telnetlib3.stream_reader.TelnetReader` so the REPL's
    ``_read_server`` loop works without changes.
    """

    def __init__(self, encoding: str = "utf-8", encoding_errors: str = "replace") -> None:
        """
        Initialise the reader with an empty queue.

        :param encoding: Character encoding for decoding binary frames.
        :param encoding_errors: Error handler for decoding (default ``"replace"``).
        """
        self._buffer: asyncio.Queue[str | None] = asyncio.Queue()
        self._eof = False
        self._encoding = encoding
        self._encoding_errors = encoding_errors

    def feed_data(self, data: bytes) -> None:
        """
        Enqueue decoded text from a BINARY WebSocket frame.

        :param data: Raw bytes from the server.
        """
        self._buffer.put_nowait(data.decode(self._encoding, errors=self._encoding_errors))

    def feed_eof(self) -> None:
        """Signal end-of-stream."""
        self._eof = True
        self._buffer.put_nowait(None)

    def at_eof(self) -> bool:
        """Return ``True`` if EOF has been signalled."""
        return self._eof

    async def read(self, n: int = -1) -> str:
        """
        Read the next chunk of server text.

        Blocks until data is available.  Returns an empty string at EOF.

        :param n: Ignored (present for API compatibility).
        """
        if self._eof and self._buffer.empty():
            return ""
        chunk = await self._buffer.get()
        if chunk is None:
            return ""
        return chunk

    # telnetlib3 TelnetReader compatibility -- the REPL calls this
    # to wake the reader when a prompt signal arrives mid-read.
    def _wakeup_waiter(self) -> None:
        """Wake any blocked ``read()`` call (feed empty string to unblock)."""
        self._buffer.put_nowait("")


class NullOptionSet:
    """Stub for ``telnet_writer.local_option`` / ``remote_option``."""

    @staticmethod
    def enabled(key: object) -> bool:
        """Return ``False`` for all telnet options."""
        return False


class WebSocketWriter:
    """
    Writer that sends data over a WebSocket connection.

    Presents the subset of :class:`~telnetlib3.stream_writer.TelnetWriter`
    that the REPL and shell actually use: ``write()``, ``close()``,
    ``is_closing()``, ``will_echo``, ``mode``, ``get_extra_info()``,
    ``set_ext_callback()``, ``set_iac_callback()``, and ``log``.

    Also provides stubs for telnet-specific attributes (``local_option``,
    ``remote_option``, ``client``, ``_send_naws``, ``handle_send_naws``)
    so that code shared with the telnet path does not need conditionals.
    """

    def __init__(self, ws: typing.Any, peername: tuple[str, int] | None = None, encoding: str = "utf-8") -> None:
        """
        Initialise the writer.

        :param ws: A ``websockets`` connection object.
        :param peername: ``(host, port)`` tuple for ``get_extra_info("peername")``.
        :param encoding: Character encoding for outgoing text.
        """
        self._ws = ws
        self._closing = False
        self._peername = peername
        self._ext_callback: dict[bytes, Callable[..., object]] = {}
        self._iac_callback: dict[bytes, Callable[..., object]] = {}
        self._send_queue: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        self.encoding = encoding
        self.ctx: TelixSessionContext = None
        self.log = logging.getLogger("telix.ws_transport")
        self.will_echo: bool = False
        self.mode: str = "local"

        # Telnetlib3 compatibility stubs.
        self.local_option = NullOptionSet()
        self.remote_option = NullOptionSet()
        self.client: bool = True
        self.handle_send_naws: Callable[[], None] | None = None

    def write(self, text: str | bytes) -> None:
        """
        Enqueue *text* for sending as a BINARY WebSocket frame.

        The actual send is performed by the :meth:`drain` background task.

        :param text: Text to send (str is encoded to UTF-8; bytes passed through).
        """
        self._send_queue.put_nowait(text.encode(self.encoding) if isinstance(text, str) else text)

    def _write(self, buf: bytes, escape_iac: bool = True) -> None:
        """
        Write raw bytes for sending (telnetlib3 raw event loop compatibility).

        IAC escaping is ignored -- WebSocket framing does not use IAC.

        :param buf: Bytes to send.
        :param escape_iac: Ignored (present for API compatibility).
        """
        self._send_queue.put_nowait(buf)

    def _send_naws(self) -> None:
        """No-op stub for telnetlib3 NAWS negotiation."""

    def close(self) -> None:
        """Mark the writer as closing and signal :meth:`drain` to stop."""
        self._closing = True
        self._send_queue.put_nowait(None)

    def is_closing(self) -> bool:
        """Return ``True`` if :meth:`close` has been called."""
        return self._closing

    def get_extra_info(self, name: str, default: object = None) -> object:
        """
        Return transport metadata.

        :param name: Key name (only ``"peername"`` and ``"ssl_object"`` supported).
        :param default: Value to return if *name* is not available.
        """
        if name == "peername":
            return self._peername if self._peername is not None else default
        if name == "ssl_object":
            return None
        return default

    def set_ext_callback(self, key: bytes, callback: Callable[..., object]) -> None:
        r"""
        Register an extension callback (e.g. GMCP).

        :param key: Telopt byte (e.g. ``b'\xc9'`` for GMCP).
        :param callback: Callable receiving ``(package, data)``.
        """
        self._ext_callback[key] = callback

    def set_iac_callback(self, key: bytes, callback: Callable[..., object]) -> None:
        r"""
        Register an IAC callback (e.g. GA, EOR).

        :param key: IAC command byte (e.g. ``b'\xf9'`` for GA).
        :param callback: Callable receiving the command byte.
        """
        self._iac_callback[key] = callback

    def dispatch_gmcp(self, package: str, data: typing.Any) -> None:
        """
        Dispatch a parsed GMCP message to the registered ext callback.

        :param package: GMCP package name (e.g. ``"Room.Info"``).
        :param data: Parsed JSON payload (or ``None``).
        """
        cb = self._ext_callback.get(GMCP)
        if cb is not None:
            cb(package, data)

    def write_iac(self, cmd: bytes, option: bytes) -> None:
        """
        Enqueue an IAC response (e.g. ``IAC DO ECHO``) as a binary frame.

        Used by the telnet-over-WebSocket receive loop to send negotiation
        responses back to the server.

        :param cmd: IAC command byte (e.g. ``WILL``, ``WONT``, ``DO``, ``DONT``).
        :param option: Telnet option byte.
        """
        self._send_queue.put_nowait(telnetlib3.telopt.IAC + cmd + option)

    def send_gmcp(self, package: str, data: typing.Any = None) -> None:
        """
        Enqueue a GMCP message for sending as a TEXT WebSocket frame.

        The actual send is performed by the :meth:`drain` background task.

        :param package: GMCP package name.
        :param data: JSON-serialisable payload, or ``None``.
        """
        if data is None:
            self._send_queue.put_nowait(package)
        else:
            self._send_queue.put_nowait(f"{package} {json.dumps(data)}")

    async def drain(self) -> None:
        """
        Send queued messages over the WebSocket until closed.

        Must be run as a background task.  Items are sent in FIFO order.
        Stops when a ``None`` sentinel is received (placed by :meth:`close`)
        or when the connection closes unexpectedly.
        """
        while True:
            item = await self._send_queue.get()
            if item is None:
                break
            try:
                await self._ws.send(item)
            except websockets.exceptions.ConnectionClosed:
                log.debug("WebSocket closed during drain, discarding remaining queue")
                break

    def fire_prompt_signal(self) -> None:
        """
        Fire a GA or EOR IAC callback as a pseudo-prompt signal.

        Called after delivering each BINARY frame's content.  WebSocket message boundaries are a reliable prompt signal
        since each server output cycle produces one message.

        Fires EOR if registered, otherwise GA, to ensure a single prompt boundary per frame even if both callbacks point
        to the same handler.
        """
        eor_cb = self._iac_callback.get(CMD_EOR)
        if eor_cb is not None:
            eor_cb(CMD_EOR)
        else:
            ga_cb = self._iac_callback.get(GA)
            if ga_cb is not None:
                ga_cb(GA)
