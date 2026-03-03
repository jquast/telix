"""Entry point for the telix CLI."""

# std imports
import sys
import argparse
import asyncio

import telnetlib3.client

# local
from . import directory, client_tui_base, client_tui_dialogs, ws_client

# Module-level store for telix-specific args, set by main() before
# telnetlib3 starts the shell.  Read by client_shell._setup_color_filter().
_color_args: argparse.Namespace | None = None

# Cached terminal background/foreground colors detected before any
# framework (Textual, telnetlib3) takes over stdin.  Set once by
# _detect_terminal_colors() in main(), read by client_shell and
# client_tui_base.
_detected_bg: tuple[int, int, int] | None = None
_detected_fg: tuple[int, int, int] | None = None


def _detect_terminal_colors() -> None:
    """
    Query the terminal for background and foreground colors.

    Must be called before Textual or telnetlib3 takes over stdin,
    otherwise the OSC 11/10 response is consumed by the framework.
    Stores results in :data:`_detected_bg` and :data:`_detected_fg`.
    """
    global _detected_bg, _detected_fg
    import blessed
    term = blessed.Terminal()
    with term.cbreak():
        bg = term.get_bgcolor(timeout=0.5, bits=8)
        fg = term.get_fgcolor(timeout=0.5, bits=8)
    _detected_bg = bg if bg != (-1, -1, -1) else None
    _detected_fg = fg if fg != (-1, -1, -1) else None


def _build_telix_parser() -> argparse.ArgumentParser:
    """
    Build argument parser for telix-specific CLI flags.

    These flags are consumed by telix and stripped from ``sys.argv``
    before telnetlib3 parses its own arguments.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--colormatch", default="vga")
    parser.add_argument("--color-brightness", type=float, default=1.0,
                        dest="color_brightness")
    parser.add_argument("--color-contrast", type=float, default=1.0,
                        dest="color_contrast")
    parser.add_argument("--background-color", default="#000000",
                        dest="background_color")
    parser.add_argument("--no-ice-colors", action="store_true", default=False,
                        dest="no_ice_colors")
    parser.add_argument("--no-repl", action="store_true", default=False,
                        dest="no_repl")
    return parser


def _strip_telix_args() -> argparse.Namespace:
    """
    Parse and remove telix-specific flags from ``sys.argv``.

    :returns: Namespace with the parsed telix-specific values.
    """
    parser = _build_telix_parser()
    telix_args, remaining = parser.parse_known_args(sys.argv[1:])
    sys.argv[1:] = remaining
    return telix_args


DAGGER = "\u2020"


def _build_help_parser() -> argparse.ArgumentParser:
    """
    Build a unified help-only parser showing all telix options.

    Groups connection and telix-specific options alphabetically, marking
    telnet-only options with a dagger.
    """
    parser = argparse.ArgumentParser(
        prog="telix",
        usage="telix [options] {host [port] | ws-url}",
        description="Telnet and WebSocket MUD/BBS client.",
    )

    conn = parser.add_argument_group("Connection options")
    conn.add_argument(
        "--always-do", metavar="OPT",
        help=f"always send DO for this option {DAGGER}",
    )
    conn.add_argument(
        "--always-dont", metavar="OPT",
        help=f"always send DONT for this option {DAGGER}",
    )
    conn.add_argument(
        "--always-will", metavar="OPT",
        help=f"always send WILL for this option {DAGGER}",
    )
    conn.add_argument(
        "--always-wont", metavar="OPT",
        help=f"always send WONT for this option {DAGGER}",
    )
    conn.add_argument(
        "--ansi-keys", action="store_true",
        help=f"transmit raw ANSI escape sequences for arrow/function keys {DAGGER}",
    )
    conn.add_argument(
        "--ascii-eol", action="store_true",
        help=f"use ASCII CR/LF instead of encoding-native EOL {DAGGER}",
    )
    conn.add_argument(
        "--compression", action="store_true", default=None,
        help=f"request MCCP compression {DAGGER}",
    )
    conn.add_argument(
        "--connect-maxwait", metavar="N", type=float,
        help=f"timeout for pending negotiation (default: 4.0) {DAGGER}",
    )
    conn.add_argument(
        "--connect-minwait", metavar="N", type=float,
        help=f"shell delay for negotiation (default: 0) {DAGGER}",
    )
    conn.add_argument(
        "--connect-timeout", metavar="N", type=float,
        help=f"timeout for TCP connection in seconds (default: 10) {DAGGER}",
    )
    conn.add_argument(
        "--encoding", default="utf-8",
        help="encoding name (default: utf-8)",
    )
    conn.add_argument(
        "--encoding-errors", default="replace",
        help="handler for encoding errors (default: replace)",
    )
    conn.add_argument(
        "--force-binary", action="store_true",
        help=f"force binary mode negotiation {DAGGER}",
    )
    conn.add_argument(
        "--gmcp-modules", metavar="MODULES",
        help=f"comma-separated list of GMCP modules to request {DAGGER}",
    )
    conn.add_argument(
        "--line-mode", action="store_true",
        help="force line-mode input (default: auto-detect)",
    )
    conn.add_argument(
        "--logfile", metavar="FILE",
        help="write log to FILE",
    )
    conn.add_argument(
        "--logfile-mode", choices=["append", "rewrite"],
        help="log file write mode (default: append)",
    )
    conn.add_argument(
        "--loglevel",
        help="logging level (default: warn)",
    )
    conn.add_argument(
        "--no-repl", action="store_true",
        help="disable the interactive REPL (raw I/O only)",
    )
    conn.add_argument(
        "--raw-mode", action="store_true",
        help="force raw-mode input (default: auto-detect)",
    )
    conn.add_argument(
        "--send-environ", metavar="VARS",
        help=f"comma-separated environment variables to send {DAGGER}",
    )
    conn.add_argument(
        "--shell", metavar="SHELL",
        help="dotted path to shell coroutine",
    )
    conn.add_argument(
        "--speed", metavar="N", type=int,
        help=f"terminal speed to report (default: 38400) {DAGGER}",
    )
    conn.add_argument(
        "--ssl", action="store_true",
        help=f"enable SSL/TLS {DAGGER}",
    )
    conn.add_argument(
        "--ssl-cafile", metavar="PATH",
        help=f"CA bundle for SSL verification {DAGGER}",
    )
    conn.add_argument(
        "--ssl-no-verify", action="store_true",
        help=f"disable SSL certificate verification {DAGGER}",
    )
    conn.add_argument(
        "--term", metavar="TERM",
        help=f"terminal type to negotiate (default: $TERM) {DAGGER}",
    )
    conn.add_argument(
        "--typescript", metavar="FILE",
        help="record session to FILE",
    )
    conn.add_argument(
        "--typescript-mode", choices=["append", "rewrite"],
        help="typescript write mode (default: append)",
    )

    telix = parser.add_argument_group("Telix options")
    telix.add_argument(
        "--background-color", metavar="COLOR",
        help="terminal background color as #RRGGBB (default: #000000)",
    )
    telix.add_argument(
        "--bbs", action="store_true",
        help="apply BBS connection presets",
    )
    telix.add_argument(
        "--color-brightness", metavar="N", type=float,
        help="color brightness multiplier (default: 1.0)",
    )
    telix.add_argument(
        "--color-contrast", metavar="N", type=float,
        help="color contrast multiplier (default: 1.0)",
    )
    telix.add_argument(
        "--colormatch", metavar="PALETTE",
        help="color palette for remapping (default: vga, 'none' to disable)",
    )
    telix.add_argument(
        "--mud", action="store_true",
        help="apply MUD connection presets",
    )
    telix.add_argument(
        "--no-ice-colors", action="store_true",
        help="disable iCE color (blink as bright background) support",
    )

    parser.epilog = f"{DAGGER} telnet-only option (not applicable to WebSocket connections)"

    return parser


def reinit() -> None:
    """Overwrite sessions.json with the bundled directory."""
    sessions = directory.directory_to_sessions()
    client_tui_base.save_sessions(sessions)
    print(f"Loaded {len(sessions)} sessions from directory.")


BBS_TELNET_FLAGS = [
    "--raw-mode",
    "--colormatch", "vga",
]

MUD_TELNET_FLAGS = [
    "--line-mode",
    "--compression",
    "--colormatch", "none",
    "--no-ice-colors",
]


def pop_server_type() -> str:
    """
    Remove ``--bbs`` or ``--mud`` from ``sys.argv`` and return the type.

    :returns: ``"bbs"``, ``"mud"``, or ``""`` if neither flag was given.
    """
    for flag, value in (("--bbs", "bbs"), ("--mud", "mud")):
        if flag in sys.argv[1:]:
            sys.argv.remove(flag)
            return value
    return ""


def main() -> None:
    """
    Entry point for the ``telix`` command.

    Without arguments, launches the TUI session manager.  With a ``ws://`` or
    ``wss://`` URL, connects directly via WebSocket.  With a host argument,
    connects directly via telnetlib3's client.

    The ``--bbs`` and ``--mud`` flags apply connection presets matching the TUI
    session editor defaults for each server type.
    """
    global _color_args

    if "--reinit" in sys.argv[1:]:
        reinit()
        return

    # Intercept --help early to show unified help output.
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        _build_help_parser().parse_args(["--help"])
        return

    _detect_terminal_colors()

    server_type = pop_server_type()

    has_ws_url = any(arg.startswith(("ws://", "wss://")) for arg in sys.argv[1:])

    if has_ws_url:
        parser = ws_client.build_parser()
        args = parser.parse_args()
        no_repl = args.no_repl or server_type == "bbs"
        _color_args = argparse.Namespace(
            colormatch=args.colormatch,
            color_brightness=args.color_brightness,
            color_contrast=args.color_contrast,
            background_color=args.background_color,
            no_ice_colors=args.no_ice_colors,
        )
        try:
            asyncio.run(
            ws_client.run_ws_client(
                url=args.url,
                shell=args.shell,
                no_repl=no_repl,
                loglevel=args.loglevel,
                logfile=args.logfile,
                typescript=args.typescript,
                logfile_mode=args.logfile_mode,
                typescript_mode=args.typescript_mode,
                encoding=args.encoding,
                encoding_errors=args.encoding_errors,
            )
        )
        except KeyboardInterrupt:
            pass
        except OSError as err:
            print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)
        return

    has_host = any(not arg.startswith("-") for arg in sys.argv[1:])
    wants_help = "-h" in sys.argv[1:] or "--help" in sys.argv[1:]
    if not has_host and not wants_help:
        client_tui_dialogs.tui_main()
        return

    # Apply server type presets before parsing.
    if server_type == "bbs":
        sys.argv.extend(BBS_TELNET_FLAGS)
    elif server_type == "mud":
        sys.argv.extend(MUD_TELNET_FLAGS)

    # Parse and strip telix-specific flags so telnetlib3 doesn't see them.
    telix_args = _strip_telix_args()
    _color_args = telix_args

    # Inject the telix shell so telnetlib3 uses our REPL-enabled shell.
    # --no-repl or BBS preset disables the REPL, so skip shell injection.
    if "--shell" not in sys.argv and not telix_args.no_repl and server_type != "bbs":
        sys.argv.insert(1, "--shell=telix.client_shell.telix_client_shell")

    try:
        asyncio.run(telnetlib3.client.run_client())
    except KeyboardInterrupt:
        pass
    except OSError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
