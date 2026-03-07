"""
SSH reader/writer adapters for telix sessions.

Provides :class:`SSHReader` and :class:`SSHWriter`, which present a compatible
interface to telnetlib3's reader/writer so that the REPL can operate over an
SSH transport without modification.

:class:`SSHReader` is a queue-based reader fed by the asyncssh receive loop.
:class:`SSHWriter` wraps an :class:`asyncssh.SSHClientProcess` for writing and
carries the auth coordination state used by :class:`~telix.ssh_client.SSHTelix`.
"""

import typing
import asyncio
import logging
from collections.abc import Callable

if typing.TYPE_CHECKING:
    import asyncssh

    from telix.session_context import TelixSessionContext

log = logging.getLogger(__name__)

__all__ = ("SSHReader", "SSHWriter")


class SSHReader:
    """
    Async reader fed by asyncssh process stdout.

    Presents the same ``read()`` / ``at_eof()`` interface as
    :class:`~telnetlib3.stream_reader.TelnetReader` so the REPL's
    ``_read_server`` loop works without changes.

    Unlike :class:`~telix.ws_transport.WebSocketReader`, ``feed_data`` accepts
    a ``str`` because asyncssh already decodes the stream.
    """

    def __init__(self) -> None:
        """Initialise the reader with an empty queue."""
        self._buffer: asyncio.Queue[str | None] = asyncio.Queue()
        self._eof = False

    def feed_data(self, data: str) -> None:
        """
        Enqueue text received from the SSH process.

        :param data: Decoded text from the server.
        """
        self._buffer.put_nowait(data)

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

    def _wakeup_waiter(self) -> None:
        """Wake any blocked ``read()`` call (feed empty string to unblock)."""
        self._buffer.put_nowait("")


class NullOptionSet:
    """Stub for ``telnet_writer.local_option`` / ``remote_option``."""

    @staticmethod
    def enabled(key: object) -> bool:
        """Return ``False`` for all telnet options."""
        return False


class SSHWriter:
    """
    Writer that sends data over an SSH process stdin.

    Presents the subset of :class:`~telnetlib3.stream_writer.TelnetWriter`
    that the REPL and shell actually use: ``write()``, ``close()``,
    ``is_closing()``, ``will_echo``, ``mode``, ``get_extra_info()``,
    ``set_ext_callback()``, ``set_iac_callback()``, and ``log``.

    Also provides stubs for telnet-specific attributes (``local_option``,
    ``remote_option``, ``client``, ``_send_naws``, ``handle_send_naws``)
    so that code shared with the telnet path does not need conditionals.

    Auth coordination attributes:

    - ``pending_auth``: when ``True`` the REPL routes Enter-key input to
      ``auth_response_queue`` instead of the SSH process.
    - ``auth_response_queue``: filled by the REPL, awaited by SSH auth callbacks.
    - ``change_terminal_size(cols, rows)``: forwards a terminal resize to the
      SSH process so the remote PTY tracks the local window.
    """

    def __init__(
        self,
        process: "asyncssh.SSHClientProcess | None" = None,
        peername: tuple[str, int] | None = None,
    ) -> None:
        """
        Initialise the writer.

        :param process: The asyncssh process; may be ``None`` initially and set
            later once the connection is established.
        :param peername: ``(host, port)`` tuple for ``get_extra_info("peername")``.
        """
        self._process: asyncssh.SSHClientProcess | None = process
        self._peername = peername
        self._closing = False
        self._ext_callback: dict[bytes, Callable[..., object]] = {}
        self._iac_callback: dict[bytes, Callable[..., object]] = {}
        self.log = logging.getLogger("telix.ssh_transport")
        self.encoding: str = "utf-8"
        self.ctx: TelixSessionContext = None  # type: ignore[assignment]
        self.will_echo: bool = False
        self.mode: str = "local"

        # Telnetlib3 compatibility stubs.
        self.local_option = NullOptionSet()
        self.remote_option = NullOptionSet()
        self.client: bool = True
        self.handle_send_naws: Callable[[], None] | None = None

        # Auth coordination.
        self.pending_auth: bool = False
        self.auth_response_queue: asyncio.Queue[str] = asyncio.Queue()

    @property
    def process(self) -> "asyncssh.SSHClientProcess | None":
        """Return the underlying asyncssh process, or ``None`` before connect."""
        return self._process

    @process.setter
    def process(self, value: "asyncssh.SSHClientProcess | None") -> None:
        self._process = value

    def write(self, text: str | bytes) -> None:
        """
        Write *text* to the SSH process stdin.

        :param text: Text to send; bytes are decoded to str before writing.
        """
        if self._process is None:
            return
        if isinstance(text, bytes):
            text = text.decode(self.encoding, errors="replace")
        self._process.stdin.write(text)

    def _write(self, buf: bytes, escape_iac: bool = True) -> None:
        """
        Write raw bytes (telnetlib3 raw event-loop compatibility).

        :param buf: Bytes to send.
        :param escape_iac: Ignored (present for API compatibility).
        """
        self.write(buf)

    def _send_naws(self) -> None:
        """No-op stub for telnetlib3 NAWS negotiation."""

    def close(self) -> None:
        """Close the SSH process stdin."""
        if not self._closing:
            self._closing = True
            if self._process is not None:
                self._process.stdin.close()

    def is_closing(self) -> bool:
        """Return ``True`` if :meth:`close` has been called."""
        return self._closing

    def change_terminal_size(self, cols: int, rows: int) -> None:
        """
        Request an SSH terminal resize.

        :param cols: New terminal width in columns.
        :param rows: New terminal height in rows.
        """
        if self._process is not None:
            self._process.change_terminal_size(cols, rows)

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
        Register an extension callback.

        :param key: Telopt byte (e.g. ``b'\xc9'`` for GMCP).
        :param callback: Callable receiving ``(package, data)``.
        """
        self._ext_callback[key] = callback

    def set_iac_callback(self, key: bytes, callback: Callable[..., object]) -> None:
        r"""
        Register an IAC callback.

        :param key: IAC command byte (e.g. ``b'\xf9'`` for GA).
        :param callback: Callable receiving the command byte.
        """
        self._iac_callback[key] = callback
