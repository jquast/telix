"""Load the bundled MUD/BBS directory and convert to session configs."""

# std imports
import configparser
import json
import typing
import importlib.resources

from . import client_tui_base

# Encoding aliases applied when loading favorites.
_ENCODING_ALIASES: dict[str, str] = {"topaz": "latin1"}


def load_directory() -> list[dict[str, typing.Any]]:
    """
    Read the bundled ``telix/data/directory.json``.

    :returns: list of directory entry dicts
    """
    ref = importlib.resources.files("telix.data").joinpath("directory.json")
    text = ref.read_text(encoding="utf-8")
    result: list[dict[str, typing.Any]] = json.loads(text)
    return result


def load_favorites() -> list[dict[str, typing.Any]]:
    """
    Read the bundled ``telix/data/favorites.ini``.

    :returns: list of entry dicts matching the directory.json schema
    """
    ref = importlib.resources.files("telix.data").joinpath("favorites.ini")
    text = ref.read_text(encoding="utf-8")
    parser = configparser.ConfigParser()
    parser.read_string(text)
    entries: list[dict[str, typing.Any]] = []
    for section in parser.sections():
        values = dict(parser[section])
        entry: dict[str, typing.Any] = {
            "name": section,
            "host": values["host"],
            "port": int(values.get("port", "23")),
            "type": values.get("type", "mud"),
        }
        if values.get("ssl", "no").lower() in ("yes", "true", "1"):
            entry["ssl"] = True
        enc = values.get("encoding")
        if enc:
            enc = _ENCODING_ALIASES.get(enc, enc)
            entry["encoding"] = enc
        entries.append(entry)
    return entries


def _apply_type_presets(cfg: client_tui_base.SessionConfig, entry_type: str) -> None:
    """Apply type-specific presets matching the session manager radio buttons."""
    if entry_type == "bbs":
        cfg.colormatch = "vga"
        cfg.ice_colors = True
        cfg.mode = "raw"
        cfg.no_repl = True
        cfg.compression = None  # passive
    elif entry_type == "mud":
        cfg.colormatch = "none"
        cfg.ice_colors = False
        cfg.mode = "line"
        cfg.no_repl = False
        cfg.compression = True


def _entry_to_session(entry: dict[str, typing.Any]) -> client_tui_base.SessionConfig:
    """Convert a single directory/favorites entry dict to a SessionConfig."""
    host = entry["host"]
    port = entry.get("port", 23)
    cfg = client_tui_base.SessionConfig(
        host=host, port=port, name=entry.get("name", host)
    )
    if entry.get("ssl"):
        cfg.ssl = True
    enc = entry.get("encoding")
    if enc:
        cfg.encoding = enc
    _apply_type_presets(cfg, entry.get("type", ""))
    return cfg


def directory_to_sessions() -> dict[str, typing.Any]:
    """
    Convert directory entries to a sessions dict.

    Each entry becomes a :class:`~telix.client_tui_base.SessionConfig` keyed by
    ``"host:port"``.  Only fields that differ from ``SessionConfig`` defaults
    are set.  Favorites are merged in and bookmarked.

    :returns: dict mapping ``"host:port"`` to ``SessionConfig``
    """
    entries = load_directory()
    sessions: dict[str, typing.Any] = {}
    for entry in entries:
        key = f"{entry['host']}:{entry.get('port', 23)}"
        sessions[key] = _entry_to_session(entry)

    for fav in load_favorites():
        key = f"{fav['host']}:{fav.get('port', 23)}"
        if key in sessions:
            sessions[key].bookmarked = True
        else:
            cfg = _entry_to_session(fav)
            cfg.bookmarked = True
            sessions[key] = cfg

    return sessions
