"""Entry point for the telix CLI."""

# std imports
import os
import sys
import asyncio
import argparse

if os.environ.get("COVERAGE_PROCESS_START"):
    import coverage

    coverage.process_startup()

import telnetlib3.client

# local
from . import mtts, directory, ws_client, ssh_client, client_tui_dialogs, client_tui_session_manager


def _parse_option_list(values: list[str]) -> set[bytes]:
    """
    Parse a list of option arguments, splitting comma-separated values.

    :param values: List of option strings, each may be comma-separated.
    :returns: Set of parsed option bytes.
    """
    result: set[bytes] = set()
    for v in values:
        for item in v.split(","):
            item = item.strip()
            if item:
                result.add(telnetlib3.client._parse_option_arg(item))
    return result


# Module-level store for telix-specific args, set by main() before
# telnetlib3 starts the shell.  Read by client_shell._setup_color_filter().
_color_args: argparse.Namespace | None = None


def _detect_terminal_colors() -> "str | None":
    """
    Query the terminal for background/foreground colors and software name.

    Must be called before Textual or telnetlib3 takes over stdin,
    otherwise the OSC/XTVERSION responses are consumed by the framework.
    Stores background/foreground results in :envvar:`TELIX_DETECTED_BG` and
    :envvar:`TELIX_DETECTED_FG` (format ``R,G,B``) so subprocess connections
    inherit them without re-querying.

    :returns: Detected terminal software name, or ``None`` if not detected.
    """
    import blessed

    term = blessed.Terminal()
    with term.cbreak():
        bg = term.get_bgcolor(timeout=0.5, bits=8)
        fg = term.get_fgcolor(timeout=0.5, bits=8)
        sw = term.get_software_version(timeout=0.5)
    if bg != (-1, -1, -1):
        os.environ["TELIX_DETECTED_BG"] = f"{bg[0]},{bg[1]},{bg[2]}"
    else:
        os.environ.pop("TELIX_DETECTED_BG", None)
    if fg != (-1, -1, -1):
        os.environ["TELIX_DETECTED_FG"] = f"{fg[0]},{fg[1]},{fg[2]}"
    else:
        os.environ.pop("TELIX_DETECTED_FG", None)
    return sw.name if sw is not None else None


def _build_telix_parser() -> argparse.ArgumentParser:
    """
    Build argument parser for telix-specific CLI flags.

    These flags are consumed by telix and stripped from ``sys.argv``
    before telnetlib3 parses its own arguments.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--colormatch", default="vga")
    parser.add_argument("--color-brightness", type=float, default=1.0, dest="color_brightness")
    parser.add_argument("--color-contrast", type=float, default=1.0, dest="color_contrast")
    parser.add_argument("--background-color", default="#000000", dest="background_color")
    parser.add_argument("--no-ice-colors", action="store_true", default=False, dest="no_ice_colors")
    parser.add_argument("--no-repl", action="store_true", default=False, dest="no_repl")
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


def _build_help_parser() -> argparse.ArgumentParser:
    """
    Build a unified help-only parser showing all telix options.

    Groups connection and telix-specific options alphabetically, marking telnet-only options.
    """
    parser = argparse.ArgumentParser(
        prog="telix",
        description=(
            "Telnet, WebSocket, and SSH MUD/BBS client.\n\n"
            "  telix host [port]                 -- Telnet\n"
            "  telix telnet://host[:port]        -- Telnet\n"
            "  telix telnets://host[:port]       -- Telnet with SSL\n"
            "  telix ws://host[:port][/path]     -- WebSocket\n"
            "  telix wss://host[:port][/path]    -- WebSocket with SSL\n"
            "  telix ssh://[user@]host[:port]    -- SSH"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    conn = parser.add_argument_group("Connection options")
    conn.add_argument(
        "--always-do", metavar="OPT", help="always send DO for this option (comma-separated, named like GMCP)"
    )
    conn.add_argument(
        "--always-dont", metavar="OPT", help="always send DONT for this option, refusing even natively supported"
    )
    conn.add_argument(
        "--always-will", metavar="OPT", help="always send WILL for this option (comma-separated, named like MXP)"
    )
    conn.add_argument(
        "--always-wont", metavar="OPT", help="always send WONT for this option, refusing even natively supported"
    )
    conn.add_argument(
        "--ansi-keys", action="store_true", help="transmit raw ANSI escape sequences for arrow/function keys"
    )
    conn.add_argument("--ascii-eol", action="store_true", help="use ASCII CR/LF instead of encoding-native EOL")
    conn.add_argument("--compression", action="store_true", default=None, help="request MCCP compression")
    conn.add_argument(
        "--connect-maxwait", metavar="N", type=float, help="timeout for pending negotiation (default: 4.0)"
    )
    conn.add_argument("--connect-minwait", metavar="N", type=float, help="shell delay for negotiation (default: 0)")
    conn.add_argument(
        "--connect-timeout", metavar="N", type=float, help="timeout for connection in seconds (default: 10)"
    )
    conn.add_argument("--encoding", default="utf-8", help="encoding name (default: utf-8)")
    conn.add_argument("--encoding-errors", default="replace", help="handler for encoding errors (default: replace)")
    conn.add_argument("--gmcp-modules", metavar="MODULES", help="comma-separated list of GMCP modules to request")
    conn.add_argument("--line-mode", action="store_true", help="force line-mode input (default: auto-detect)")
    conn.add_argument("--logfile", metavar="FILE", help="write log to FILE")
    conn.add_argument("--logfile-mode", choices=["append", "rewrite"], help="log file write mode (default: append)")
    conn.add_argument("--loglevel", help="logging level (default: warn)")
    conn.add_argument("--no-repl", action="store_true", help="disable the interactive REPL (raw I/O only)")
    conn.add_argument("--raw-mode", action="store_true", help="force raw-mode input (default: auto-detect)")
    conn.add_argument(
        "--send-environ", metavar="VARS", help="comma-separated environment variables to send via NEW-ENVIRON"
    )
    conn.add_argument("--shell", metavar="SHELL", help="dotted path to shell coroutine")
    conn.add_argument("--speed", metavar="N", type=int, help="terminal speed to report (default: 38400)")
    conn.add_argument("--ssl", action="store_true", help="enable SSL/TLS (telnet only)")
    conn.add_argument("--ssl-cafile", metavar="PATH", help="CA bundle for SSL verification (telnet only)")
    conn.add_argument("--ssl-no-verify", action="store_true", help="disable SSL certificate verification (telnet only)")
    conn.add_argument("--term", metavar="TERM", help="terminal type to negotiate (default: $TERM)")
    conn.add_argument("--typescript", metavar="FILE", help="record session to FILE")
    conn.add_argument(
        "--typescript-mode", choices=["append", "rewrite"], help="typescript write mode (default: append)"
    )

    ssh = parser.add_argument_group("SSH options (ssh://[user@]host[:port] connections)")
    ssh.add_argument("--key-file", metavar="FILE", help="path to private key file")
    ssh.add_argument("--username", metavar="USER", help="login username (default: system login)")

    telix = parser.add_argument_group("Telix options")
    telix.add_argument(
        "--background-color", metavar="COLOR", help="terminal background color as #RRGGBB (default: #000000)"
    )
    telix.add_argument("--bbs", action="store_true", help="apply BBS connection presets")
    telix.add_argument("--color-brightness", metavar="N", type=float, help="color brightness multiplier (default: 1.0)")
    telix.add_argument("--color-contrast", metavar="N", type=float, help="color contrast multiplier (default: 1.0)")
    telix.add_argument(
        "--colormatch", metavar="PALETTE", help="color palette for remapping (default: vga, 'none' to disable)"
    )
    telix.add_argument("--mud", action="store_true", help="apply MUD connection presets")
    telix.add_argument(
        "--no-ice-colors", action="store_true", help="disable iCE color (blink as bright background) support"
    )

    return parser


def reinit() -> None:
    """Overwrite sessions.json with the bundled directory."""
    sessions = directory.directory_to_sessions()
    client_tui_session_manager.save_sessions(sessions)
    print(f"Loaded {len(sessions)} sessions from directory.")


BBS_TELNET_FLAGS = ["--raw-mode", "--colormatch", "vga"]

MUD_TELNET_FLAGS = ["--line-mode", "--compression", "--colormatch", "none", "--no-ice-colors"]


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


def _get_argv_value(flag: str, default: str) -> str:
    """
    Return the value of a ``--flag`` argument from ``sys.argv``.

    Supports both ``--flag=value`` and ``--flag value`` forms.

    :param flag: The flag name including dashes (e.g. ``"--term"``).
    :param default: Value to return if the flag is not found.
    """
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
        if arg == flag and i < len(sys.argv) - 1:
            return sys.argv[i + 1]
    return default


def _get_term_value() -> str:
    """
    Return the terminal type for MTTS negotiation.

    Uses ``--term`` from ``sys.argv`` if present, otherwise ``$TERM``, or "ansi" when not present.
    """
    return _get_argv_value("--term", os.environ.get("TERM", "ansi"))


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

    detected_sw_name = _detect_terminal_colors()

    server_type = pop_server_type()

    # Rewrite telnet:// and telnets:// URLs to bare host/port argv so the
    # standard telnet path handles them.  telnets:// also injects --ssl.
    telnet_url = next((a for a in sys.argv[1:] if a.startswith(("telnet://", "telnets://"))), None)
    if telnet_url is not None:
        import urllib.parse

        parsed = urllib.parse.urlparse(telnet_url)
        idx = sys.argv.index(telnet_url)
        replacement = [parsed.hostname or ""]
        if parsed.port:
            replacement.append(str(parsed.port))
        sys.argv[idx : idx + 1] = replacement
        if telnet_url.startswith("telnets://") and "--ssl" not in sys.argv:
            sys.argv.append("--ssl")

    has_ssh_url = any(arg.startswith("ssh://") for arg in sys.argv[1:])
    has_ws_url = any(arg.startswith(("ws://", "wss://")) for arg in sys.argv[1:])

    if has_ws_url:
        parser = ws_client.build_parser()
        args = parser.parse_args()
        no_repl = args.no_repl or server_type == "bbs"
        raw_mode: bool | None = True if args.raw_mode else (False if args.line_mode else None)
        _color_args = argparse.Namespace(
            colormatch=args.colormatch,
            color_brightness=args.color_brightness,
            color_contrast=args.color_contrast,
            background_color=args.background_color,
            no_ice_colors=args.no_ice_colors,
        )
        compression: bool | None = True if args.compression else (False if args.no_compression else None)
        always_do = _parse_option_list(args.always_do)
        always_will = _parse_option_list(args.always_will)
        always_dont = _parse_option_list(args.always_dont)
        always_wont = _parse_option_list(args.always_wont)
        gmcp_modules = [m.strip() for m in args.gmcp_modules.split(",") if m.strip()] if args.gmcp_modules else None
        send_environ = (
            tuple(e.strip() for e in args.send_environ.split(",") if e.strip()) if args.send_environ else None
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
                    raw_mode=raw_mode,
                    ansi_keys=args.ansi_keys,
                    ascii_eol=args.ascii_eol,
                    always_do=always_do,
                    always_will=always_will,
                    always_dont=always_dont,
                    always_wont=always_wont,
                    term=args.term,
                    speed=args.speed,
                    send_environ=send_environ,
                    gmcp_modules=gmcp_modules,
                    connect_minwait=args.connect_minwait,
                    connect_maxwait=args.connect_maxwait,
                    connect_timeout=args.connect_timeout,
                    compression=compression,
                    color_args=_color_args,
                )
            )
        except KeyboardInterrupt:
            pass
        except OSError as err:
            print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)
        return

    if has_ssh_url:
        import urllib.parse

        from .client_shell import ssh_client_shell

        ssh_url = next(arg for arg in sys.argv[1:] if arg.startswith("ssh://"))
        parsed = urllib.parse.urlparse(ssh_url)
        host = parsed.hostname or ""
        # Build an argv for ssh_client.build_parser(): positional host plus optional flags.
        # URL-encoded username and port are injected as flags unless already supplied.
        argv = [a for a in sys.argv[1:] if a != ssh_url]
        argv.insert(0, host)
        if parsed.port and "--port" not in argv:
            argv[1:1] = ["--port", str(parsed.port)]
        if parsed.username and "--username" not in argv:
            argv += ["--username", parsed.username]
        args = ssh_client.build_parser().parse_args(argv)
        term_type = args.term or os.environ.get("TERM", "xterm-256color")
        _color_args = argparse.Namespace(
            colormatch=args.colormatch,
            color_brightness=args.color_brightness,
            color_contrast=args.color_contrast,
            background_color=args.background_color,
            no_ice_colors=args.no_ice_colors,
        )
        try:
            asyncio.run(
                ssh_client.run_ssh_client(
                    host=args.host,
                    port=args.port,
                    username=args.username,
                    key_file=args.key_file,
                    term_type=term_type,
                    shell=ssh_client_shell,
                    color_args=_color_args,
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

    # Inject the telix shell so telnetlib3 uses our REPL-aware shell.
    # Our shell waits for echo negotiation before entering the raw event loop,
    # preventing a race where local_echo is set before WILL ECHO arrives and
    # causes software echo of user input and CPR responses (visible as [r;cR
    # garbage on screen).  BBS connections also benefit from this fix since
    # gambatte-style servers negotiate WILL ECHO + SGA (kludge mode) and the
    # default telnetlib3 shell computes local_echo before negotiation completes.
    if "--shell" not in sys.argv and not telix_args.no_repl:
        sys.argv.insert(1, "--shell=telix.client_shell.telix_client_shell")

    # Install MTTS TTYPE cycling and MNES for MUD connections.
    if server_type != "bbs":
        is_ssl = "--ssl" in sys.argv or "--ssl-no-verify" in sys.argv
        mtts.install_mtts(
            _get_term_value(), ssl=is_ssl, sw_name=detected_sw_name, encoding=_get_argv_value("--encoding", "utf-8")
        )

    try:
        asyncio.run(telnetlib3.client.run_client())
    except KeyboardInterrupt:
        pass
    except OSError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
