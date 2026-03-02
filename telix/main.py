"""Entry point for the telix CLI."""

# std imports
import sys
import asyncio

import telnetlib3.client

# local
from . import directory, client_tui_base, client_tui_dialogs


def reinit() -> None:
    """Overwrite sessions.json with the bundled directory."""
    sessions = directory.directory_to_sessions()
    client_tui_base.save_sessions(sessions)
    print(f"Loaded {len(sessions)} sessions from directory.")


def main() -> None:
    """
    Entry point for the ``telix`` command.

    Without a host argument, launches the TUI session manager. With a host argument, connects
    directly via telnetlib3's client.
    """
    if "--reinit" in sys.argv[1:]:
        reinit()
        return

    has_host = any(not arg.startswith("-") for arg in sys.argv[1:])
    wants_help = "-h" in sys.argv[1:] or "--help" in sys.argv[1:]
    if not has_host and not wants_help:
        client_tui_dialogs.tui_main()
        return

    # Inject the telix shell so telnetlib3 uses our REPL-enabled shell.
    if "--shell" not in sys.argv:
        sys.argv.insert(1, "--shell=telix.client_shell.telix_client_shell")

    try:
        asyncio.run(telnetlib3.client.run_client())
    except KeyboardInterrupt:
        pass
    except OSError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
