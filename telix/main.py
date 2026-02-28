"""Entry point for the telix CLI."""

# std imports
import sys
import asyncio


def main() -> None:
    """
    Entry point for the ``telix`` command.

    Without a host argument, launches the TUI session manager.
    With a host argument, connects directly via telnetlib3's client.
    """
    has_host = any(not arg.startswith("-") for arg in sys.argv[1:])
    wants_help = "-h" in sys.argv[1:] or "--help" in sys.argv[1:]
    if not has_host and not wants_help:
        from .client_tui import tui_main

        tui_main()
        return

    from telnetlib3.client import run_client

    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        pass
    except OSError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
