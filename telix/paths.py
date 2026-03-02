"""
Consolidated XDG Base Directory paths for telix.

Provides config and data directory resolution following the `XDG Base Directory Specification
<https://specifications.freedesktop.org/basedir-spec/latest/>`_.

Constants are frozen at import time from environment variables.
"""

# std imports
import os
import json
import typing
import hashlib
import pathlib
import tempfile

XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
XDG_DATA = os.environ.get("XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share"))

APP_NAME = os.environ.get("TELIX_XDGNAME", "telix")

CONFIG_DIR = os.path.join(XDG_CONFIG, APP_NAME)
DATA_DIR = os.path.join(XDG_DATA, APP_NAME)

SESSIONS_FILE = pathlib.Path(CONFIG_DIR) / "sessions.json"
HISTORY_FILE = os.path.join(DATA_DIR, "history")


def safe_session_slug(session_key: str) -> str:
    """
    Return a filesystem-safe slug for *session_key*.

    Uses a SHA-256 hash (first 12 hex chars) to avoid path traversal
    and special-character issues with arbitrary hostnames.

    :param session_key: Session identifier, typically ``host:port``.
    :returns: 12-character hex string.
    """
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]


def history_path(session_key: str) -> str:
    """
    Return per-session history file path.

    :param session_key: Session identifier, typically ``host:port``.
    :returns: Absolute path under :data:`DATA_DIR`.
    """
    return os.path.join(DATA_DIR, f"history-{safe_session_slug(session_key)}")


def gmcp_snapshot_path(session_key: str) -> str:
    """
    Return per-session GMCP snapshot file path.

    :param session_key: Session identifier, typically ``host:port``.
    :returns: Absolute path under :data:`DATA_DIR`.
    """
    return os.path.join(DATA_DIR, f"gmcp-{safe_session_slug(session_key)}.json")


def progressbars_path() -> str:
    """
    Return the progress bars configuration file path.

    :returns: Absolute path under :data:`CONFIG_DIR`.
    """
    return os.path.join(CONFIG_DIR, "progressbars.json")


def chat_path(session_key: str) -> str:
    """
    Return per-session chat history file path.

    :param session_key: Session identifier, typically ``host:port``.
    :returns: Absolute path under :data:`DATA_DIR`.
    """
    return os.path.join(DATA_DIR, f"chat-{safe_session_slug(session_key)}.json")


def xdg_config_dir() -> pathlib.Path:
    """Return the XDG config directory for telix."""
    return pathlib.Path(CONFIG_DIR)


def xdg_data_dir() -> pathlib.Path:
    """Return the XDG data directory for telix."""
    return pathlib.Path(DATA_DIR)


def safe_terminal_size() -> str:
    """Return ``os.get_terminal_size()`` as a string, or ``"?"`` on error."""
    try:
        sz = os.get_terminal_size()
        return f"{sz.columns}x{sz.lines}"
    except OSError:
        return "?"


class BytesSafeEncoder(json.JSONEncoder):
    """JSON encoder that converts bytes to str (UTF-8) or hex."""

    def default(self, o: typing.Any) -> typing.Any:
        if isinstance(o, bytes):
            try:
                return o.decode("utf-8")
            except UnicodeDecodeError:
                return o.hex()
        return super().default(o)


def atomic_json_write(filepath: str, data: dict[str, typing.Any]) -> None:
    """Atomically write JSON data to file via write-to-new + rename."""
    tmp_path = os.path.splitext(filepath)[0] + ".json.new"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, cls=BytesSafeEncoder)
    os.replace(tmp_path, filepath)


def atomic_write(path: str, content: str) -> None:
    """
    Atomically write *content* to *path* via temp file and :func:`os.replace`.

    :param path: Target file path.
    :param content: String content to write.
    """
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
