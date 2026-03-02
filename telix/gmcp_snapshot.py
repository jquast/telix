"""
Rolling GMCP data snapshot persistence.

Persists all received GMCP packages to a JSON file with per-package timestamps, enabling offline
analysis and progress bar auto-detection.
"""

from __future__ import annotations

# std imports
import os
import json
import datetime
from typing import Any

# local
from .paths import atomic_json_write

__all__ = ("load_gmcp_snapshot", "save_gmcp_snapshot")


def save_gmcp_snapshot(path: str, session_key: str, gmcp_data: dict[str, Any]) -> None:
    """
    Merge current GMCP data into the on-disk snapshot.

    Each top-level key in *gmcp_data* becomes a package entry with its
    own ``last_updated`` timestamp.  Existing packages not present in
    *gmcp_data* are preserved.

    :param path: Path to the snapshot JSON file.
    :param session_key: Session identifier (``host:port``).
    :param gmcp_data: Current ``ctx.gmcp_data`` dict.
    """
    if not gmcp_data:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    existing = load_raw(path)
    packages: dict[str, Any] = existing.get("packages", {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for pkg_name, pkg_data in gmcp_data.items():
        packages[pkg_name] = {"data": pkg_data, "last_updated": now}
    snapshot = {"session_key": session_key, "last_updated": now, "packages": packages}
    atomic_json_write(path, snapshot)


def load_gmcp_snapshot(path: str) -> dict[str, Any]:
    """
    Read a GMCP snapshot from disk.

    :param path: Path to the snapshot JSON file.
    :returns: The ``packages`` dict, or empty dict if file is missing.
    """
    data = load_raw(path)
    return data.get("packages", {})


def load_raw(path: str) -> dict[str, Any]:
    """Read raw JSON from *path*, returning empty dict on missing/invalid file."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)
