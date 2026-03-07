"""
SSH client for telix.

Provides :func:`run_ssh_client`, :func:`build_parser`, and :func:`main`,
called by the ``telix-ssh`` entry point.  Connects via ``asyncssh.connect()``
and runs the REPL through :func:`~telix.client_shell.ssh_client_shell`.

Interactive authentication (banners, password prompts, keyboard-interactive
challenges) flows through the REPL's existing password-masking infrastructure.
The :class:`SSHTelix` client subclass feeds prompts into the reader queue and
awaits responses from :attr:`~telix.ssh_transport.SSHWriter.auth_response_queue`,
which the REPL fills when the user presses Enter while
:attr:`~telix.ssh_transport.SSHWriter.pending_auth` is ``True``.

Passwords are never stored to disk or passed on the command line.
"""

import shutil
import asyncio
import logging
import argparse
from collections.abc import Callable, Sequence, Awaitable

import asyncssh

from . import ssh_transport

log = logging.getLogger(__name__)


class SSHTelix(asyncssh.SSHClient):
    """
    Asyncssh client subclass that routes auth callbacks through the REPL.

    :param reader: :class:`~telix.ssh_transport.SSHReader` fed by the receive loop.
    :param writer: :class:`~telix.ssh_transport.SSHWriter` wrapping the process.
    """

    def __init__(self, reader: ssh_transport.SSHReader, writer: ssh_transport.SSHWriter) -> None:
        """Initialise with references to the shared reader/writer pair."""
        super().__init__()
        self._reader = reader
        self._writer = writer

    def banner_received(self, msg: str, lang: str) -> None:
        """
        Display an SSH banner by feeding it into the reader queue.

        :param msg: Banner text from the server.
        :param lang: Language tag (unused).
        """
        self._reader.feed_data(msg)

    async def password_auth_requested(self) -> str:
        """
        Collect a password from the REPL with masked input.

        Activates ``will_echo`` so the REPL scrambles the typed password, feeds
        a ``"Password: "`` prompt into the output stream, sets ``pending_auth``
        to route the next Enter-key submission to the auth queue, then waits.

        :returns: The password entered by the user.
        """
        self._writer.will_echo = True
        self._reader.feed_data("Password: ")
        self._writer.pending_auth = True
        response = await self._writer.auth_response_queue.get()
        self._writer.pending_auth = False
        self._writer.will_echo = False
        return response

    def kbdint_auth_requested(self) -> str:
        """
        Accept keyboard-interactive auth by returning an empty string.

        :returns: Empty string to proceed with keyboard-interactive.
        """
        return ""

    async def kbdint_challenge_received(
        self, name: str, instructions: str, lang: str, prompts: Sequence[tuple[str, bool]]
    ) -> list[str]:
        """
        Collect responses to keyboard-interactive challenge prompts.

        For each prompt, feeds the prompt text into the output stream and
        awaits a response from the REPL.  Echoing is suppressed for prompts
        where *echo* is ``False`` (passwords, PINs, etc.).

        :param name: Challenge name (displayed for context).
        :param instructions: Instructions from the server.
        :param lang: Language tag (unused).
        :param prompts: List of ``(prompt_text, echo)`` pairs.
        :returns: List of responses in the same order as *prompts*.
        """
        if instructions:
            self._reader.feed_data(instructions)
        responses = []
        for prompt_text, echo in prompts:
            self._writer.will_echo = not echo
            self._reader.feed_data(prompt_text)
            self._writer.pending_auth = True
            response = await self._writer.auth_response_queue.get()
            self._writer.pending_auth = False
            responses.append(response)
        self._writer.will_echo = False
        return responses


async def run_ssh_client(
    host: str, port: int, username: str, key_file: str, term_type: str, shell: Callable[..., Awaitable[None]]
) -> None:
    """
    Connect to an SSH server and run the telix shell.

    Creates :class:`~telix.ssh_transport.SSHReader` and
    :class:`~telix.ssh_transport.SSHWriter` adapters, starts the shell as a
    background task, then opens an SSH connection and process.  Data received
    from the server is fed directly into the reader queue; EOF is signalled on
    disconnect.

    :param host: SSH server hostname or address.
    :param port: SSH port (default 22).
    :param username: Login username; empty string uses the system login name.
    :param key_file: Path to a private key file; empty string uses password auth.
    :param term_type: Terminal type string (e.g. ``"xterm-256color"``).
    :param shell: Async callable ``shell(reader, writer)`` -- the REPL entry point.
    """
    reader = ssh_transport.SSHReader()
    writer = ssh_transport.SSHWriter(peername=(host, port))

    shell_task = asyncio.ensure_future(shell(reader, writer))

    client_keys = [key_file] if key_file else []
    cols, rows = shutil.get_terminal_size()

    async with asyncssh.connect(
        host,
        port,
        username=username or None,
        client_keys=client_keys,
        agent_path=None,
        known_hosts=None,
        client_factory=lambda: SSHTelix(reader, writer),
    ) as conn:
        async with conn.create_process(term_type=term_type, term_size=(cols, rows)) as process:
            writer.process = process
            try:
                async for data in process.stdout:
                    reader.feed_data(data)
            finally:
                reader.feed_eof()

    await shell_task


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``telix-ssh`` entry point."""
    parser = argparse.ArgumentParser(prog="telix-ssh", description="Connect to an SSH BBS/MUD server.")
    parser.add_argument("host", help="SSH server hostname")

    conn = parser.add_argument_group("Connection options")
    conn.add_argument("--port", type=int, default=22, metavar="N", help="SSH port (default: 22)")
    conn.add_argument("--username", default="", metavar="USER", help="login username (default: system login)")
    conn.add_argument("--key-file", default="", dest="key_file", metavar="FILE", help="path to private key file")
    conn.add_argument("--term", default="", metavar="TERM", help="terminal type to negotiate (default: $TERM)")
    conn.add_argument(
        "--loglevel",
        default="warn",
        choices=["trace", "debug", "info", "warn", "error", "critical"],
        help="logging level (default: warn)",
    )
    conn.add_argument("--logfile", default="", metavar="FILE", help="write log to FILE")
    conn.add_argument("--typescript", default="", metavar="FILE", help="record session to FILE")

    telix = parser.add_argument_group("Telix options")
    telix.add_argument(
        "--colormatch", default="vga", metavar="PALETTE", help="color palette for remapping (default: vga)"
    )
    telix.add_argument(
        "--no-ice-colors",
        action="store_true",
        default=False,
        dest="no_ice_colors",
        help="disable iCE color (blink as bright background) support",
    )
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
    return parser


def main() -> None:
    """Entry point for the ``telix-ssh`` command."""
    import os
    import sys

    from . import main as main_mod
    from .client_shell import ssh_client_shell

    parser = build_parser()
    args = parser.parse_args()

    term_type = args.term or os.environ.get("TERM", "xterm-256color")

    # Store color args so _setup_color_filter can read them in the shell.
    main_mod._color_args = args

    asyncio.run(
        run_ssh_client(
            host=args.host,
            port=args.port,
            username=args.username,
            key_file=args.key_file,
            term_type=term_type,
            shell=ssh_client_shell,
        )
    )
    sys.exit(0)
