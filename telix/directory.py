"""Load the bundled MUD/BBS directory and convert to session configs."""

# std imports
import json
import typing
import configparser
import importlib.resources

from . import client_tui_session_manager

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
        mode = values.get("mode")
        if mode in ("auto", "raw", "line"):
            entry["mode"] = mode
        protocol = values.get("protocol")
        if protocol in ("telnet", "websocket"):
            entry["protocol"] = protocol
        ws_path = values.get("ws_path")
        if ws_path:
            entry["ws_path"] = ws_path
        entries.append(entry)
    return entries


def _apply_overrides(
    cfg: client_tui_session_manager.SessionConfig, enc: str | None, mode: str | None, protocol: str | None
) -> None:
    """Apply optional field overrides to *cfg* when values are present and valid."""
    if enc:
        cfg.encoding = enc
    if mode in ("auto", "raw", "line"):
        cfg.mode = mode
    if protocol in ("telnet", "websocket"):
        cfg.protocol = protocol


def _apply_type_presets(cfg: client_tui_session_manager.SessionConfig, entry_type: str) -> None:
    """Apply type-specific presets matching the session manager radio buttons."""
    if entry_type == "bbs":
        cfg.colormatch = "vga"
        cfg.ice_colors = True
        cfg.compression = None  # passive
        if cfg.term == "XTERM-TRUECOLOR":
            cfg.term = ""
    elif entry_type == "mud":
        cfg.colormatch = "none"
        cfg.ice_colors = False
        cfg.mode = "line"
        cfg.no_repl = False
        cfg.compression = True
        if not cfg.term:
            cfg.term = "XTERM-TRUECOLOR"


def _entry_to_session(entry: dict[str, typing.Any]) -> client_tui_session_manager.SessionConfig:
    """Convert a single directory/favorites entry dict to a SessionConfig."""
    host = entry["host"]
    port = entry.get("port", 23)
    cfg = client_tui_session_manager.SessionConfig(host=host, port=port, name=entry.get("name", host))
    if entry.get("ssl"):
        cfg.ssl = True
    enc = entry.get("encoding")
    entry_type = entry.get("type", "")
    _apply_type_presets(cfg, entry_type)
    cfg.server_type = entry_type
    _apply_overrides(cfg, enc, entry.get("mode"), entry.get("protocol"))
    ws_path = entry.get("ws_path")
    if ws_path:
        cfg.ws_path = ws_path
    return cfg


def directory_to_sessions() -> dict[str, typing.Any]:
    """
    Convert directory entries to a sessions dict.

    Each entry becomes a :class:`~telix.client_tui_session_manager.SessionConfig` keyed by
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
            _apply_overrides(
                sessions[key],
                fav.get("encoding"),
                fav.get("mode"),
                fav.get("protocol"),
            )
        else:
            cfg = _entry_to_session(fav)
            cfg.bookmarked = True
            sessions[key] = cfg

    return sessions
