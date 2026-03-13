"""
MTTS and MNES protocol support.

Implements the MTTS (Mud Terminal Type Standard) capability bitvector,
the TTYPE cycling protocol, and MNES (Mud New Environment Standard)
variable responses.

MTTS TTYPE cycling -- the server sends ``SB TTYPE SEND`` up to three
times; the client responds with:

1. Client name (``TELIX``)
2. Terminal type (uppercased ``$TERM``)
3. Capability bitvector (``MTTS <N>``)

MNES adds standardised NEW_ENVIRON variables: ``CLIENT_NAME``,
``CLIENT_VERSION``, ``TERMINAL_TYPE``, ``MTTS``, and ``CHARSET``.
"""

import typing
import dataclasses
from collections.abc import Callable, Sequence

import telnetlib3.client

from . import __version__ as version


@dataclasses.dataclass
class MttsCapabilities:
    """Named boolean fields for each MTTS capability bit."""

    ansi: bool = True  # 1
    vt100: bool = True  # 2
    utf8: bool = True  # 4
    colors_256: bool = True  # 8
    mouse_tracking: bool = False  # 16
    osc_color_palette: bool = False  # 32
    screen_reader: bool = False  # 64
    proxy: bool = False  # 128
    truecolor: bool = True  # 256
    mnes: bool = True  # 512
    mslp: bool = True  # 1024
    ssl: bool = False  # 2048

    @property
    def bitvector(self) -> int:
        """Return the integer bitvector for all enabled capabilities."""
        bits = (
            (self.ansi, 1),
            (self.vt100, 2),
            (self.utf8, 4),
            (self.colors_256, 8),
            (self.mouse_tracking, 16),
            (self.osc_color_palette, 32),
            (self.screen_reader, 64),
            (self.proxy, 128),
            (self.truecolor, 256),
            (self.mnes, 512),
            (self.mslp, 1024),
            (self.ssl, 2048),
        )
        return sum(val for flag, val in bits if flag)


def make_ttype_callback(term: str, ssl: bool = False, encoding: str = "utf-8") -> Callable[[], str]:
    """
    Return a closure that cycles TTYPE responses per MTTS protocol.

    :param term: Terminal type string (e.g. from ``$TERM``).
    :param ssl: Whether the connection uses TLS.
    :param encoding: Character encoding (e.g. ``"utf-8"``, ``"cp437"``).
    :returns: Callable that returns the next TTYPE response on each call.
    """
    caps = MttsCapabilities(ssl=ssl, utf8=encoding.lower().replace("-", "") == "utf8")
    mtts_str = f"MTTS {caps.bitvector}"
    upper_term = term.upper()
    call_count = 0

    def ttype_callback() -> str:
        nonlocal call_count
        n = call_count
        call_count += 1
        if n == 0:
            return "TELIX"
        if n == 1:
            return upper_term
        return mtts_str

    return ttype_callback


class TelixClient(telnetlib3.client.TelnetClient):
    """TelnetClient subclass with MTTS TTYPE cycling, MNES, and Core.Hello override."""

    ttype_factory: Callable[[], str] | None = None
    mnes_env: dict[str, str] | None = None
    gmcp_hello: dict[str, str] | None = None

    def send_ttype(self) -> str:
        """Cycle TTYPE responses per MTTS protocol when a factory is set."""
        if self.ttype_factory is not None:
            return self.ttype_factory()
        return super().send_ttype()

    def send_env(self, keys: Sequence[str]) -> dict[str, typing.Any]:
        """Extend base env response with MNES variables."""
        env = super().send_env(keys)
        if self.mnes_env is not None:
            if keys:
                for k in keys:
                    if k in self.mnes_env:
                        env[k] = self.mnes_env[k]
            else:
                env.update(self.mnes_env)
        return env

    def _send_gmcp_hello(self) -> None:
        """Send Core.Hello identifying Telix instead of telnetlib3."""
        if self._gmcp_hello_sent:
            return
        self._gmcp_hello_sent = True
        hello = self.gmcp_hello or {"client": "Telix", "version": version}
        self.writer.send_gmcp("Core.Hello", hello)
        self.writer.send_gmcp("Core.Supports.Set", self._gmcp_modules)
        self.log.info("GMCP handshake: Core.Hello + Core.Supports.Set %s", self._gmcp_modules)


class TelixTerminalClient(TelixClient, telnetlib3.client.TelnetTerminalClient):
    """TelnetTerminalClient subclass with MTTS TTYPE cycling and MNES support."""


def client_name(sw_name: str | None = None) -> str:
    """
    Build the MNES CLIENT_NAME value.

    :param sw_name: Terminal software name detected via XTVERSION (e.g. ``"Konsole"``).
    :returns: ``"Telix"`` or ``"Telix/Konsole"`` when a name is available.
    """
    if sw_name:
        return f"Telix/{sw_name}"
    return "Telix"


def install_mtts(term: str, ssl: bool = False, sw_name: str | None = None, encoding: str = "utf-8") -> None:
    """
    Patch telnetlib3 client classes to use MTTS/MNES.

    Replaces ``TelnetClient`` and ``TelnetTerminalClient`` in the
    ``telnetlib3.client`` module so that :func:`telnetlib3.client.run_client`
    picks up the subclasses.

    :param term: Terminal type string (e.g. from ``$TERM``).
    :param ssl: Whether the connection uses TLS.
    :param sw_name: Terminal software name from XTVERSION query.
    :param encoding: Character encoding (e.g. ``"utf-8"``, ``"cp437"``).
    """
    caps = MttsCapabilities(ssl=ssl, utf8=encoding.lower().replace("-", "") == "utf8")
    TelixClient.ttype_factory = staticmethod(make_ttype_callback(term, ssl=ssl, encoding=encoding))
    TelixClient.mnes_env = {
        "CLIENT_NAME": client_name(sw_name),
        "CLIENT_VERSION": version,
        "TERMINAL_TYPE": term.upper(),
        "MTTS": str(caps.bitvector),
        "CHARSET": encoding.upper(),
    }
    telnetlib3.client.TelnetClient = TelixClient  # type: ignore[misc]
    telnetlib3.client.TelnetTerminalClient = TelixTerminalClient  # type: ignore[misc]
