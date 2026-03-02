"""Load the bundled MUD/BBS directory and convert to session configs."""

# std imports
import json
import typing
import importlib.resources

from . import client_tui_base


def load_directory() -> list[dict[str, typing.Any]]:
    """
    Read the bundled ``telix/data/directory.json``.

    :returns: list of directory entry dicts
    """
    ref = importlib.resources.files("telix.data").joinpath("directory.json")
    text = ref.read_text(encoding="utf-8")
    result: list[dict[str, typing.Any]] = json.loads(text)
    return result


def directory_to_sessions() -> dict[str, typing.Any]:
    """
    Convert directory entries to a sessions dict.

    Each entry becomes a :class:`~telix.client_tui_base.SessionConfig` keyed by
    ``"host:port"``.  Only fields that differ from ``SessionConfig`` defaults
    are set.

    :returns: dict mapping ``"host:port"`` to ``SessionConfig``
    """
    entries = load_directory()
    sessions: dict[str, typing.Any] = {}
    for entry in entries:
        host = entry["host"]
        port = entry.get("port", 23)
        key = f"{host}:{port}"
        cfg = client_tui_base.SessionConfig(host=host, port=port, name=entry.get("name", host))
        if entry.get("ssl"):
            cfg.ssl = True
        enc = entry.get("encoding")
        if enc:
            cfg.encoding = enc
        sessions[key] = cfg

    if "1984.ws:23" in sessions:
        sessions["1984.ws:23"].bookmarked = True
    return sessions
