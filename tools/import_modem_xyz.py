#!/usr/bin/env python3
"""Parse modem.xyz mudlist/bbslist into telix/data/directory.json."""

from __future__ import annotations

import ipaddress
import json
import os
import sys

# Tokens recognised as encoding names (non-numeric, non-ssl).
_KNOWN_ENCODINGS = frozenset({
    "ascii", "atascii", "big5", "cp437", "gbk", "gb18030",
    "latin-1", "latin1", "petscii", "topaz", "utf-8", "utf8",
})


def _parse_line(line: str, entry_type: str) -> dict[str, object] | None:
    """Parse a single data line into a directory entry dict.

    :param line: whitespace-separated fields ``host [port [enc [cols [tall]]]] [ssl]``
    :param entry_type: ``"mud"`` or ``"bbs"``
    :returns: entry dict or ``None`` for blank/comment lines
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    parts = line.split()
    host = parts[0]

    # Skip bare IP addresses -- only keep named hosts.
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass

    port = 23
    encoding = "utf-8"
    columns = 0
    rows = 0
    ssl = False

    rest = parts[1:]

    # Remove 'ssl' flag from anywhere in rest.
    if "ssl" in rest:
        ssl = True
        rest = [t for t in rest if t != "ssl"]

    # Remove 'tall' flag from anywhere in rest.
    if "tall" in rest:
        rows = 1  # signals tall mode
        rest = [t for t in rest if t != "tall"]

    # Field 1: port (numeric)
    if rest and rest[0].isdigit():
        port = int(rest.pop(0))

    # Field 2: encoding (non-numeric string)
    if rest and rest[0].lower() in _KNOWN_ENCODINGS:
        encoding = rest.pop(0).lower()
        if encoding == "topaz":
            encoding = "latin1"

    # Field 3: columns (numeric)
    if rest and rest[0].isdigit():
        columns = int(rest.pop(0))

    entry: dict[str, object] = {
        "host": host,
        "port": port,
        "name": host,
        "type": entry_type,
    }
    if encoding not in ("utf-8", "utf8"):
        entry["encoding"] = encoding
    if ssl:
        entry["ssl"] = True
    if columns:
        entry["columns"] = columns
    if rows:
        entry["rows"] = rows

    return entry


def parse_file(path: str, entry_type: str) -> list[dict[str, object]]:
    """Parse a modem.xyz list file.

    :param path: path to ``mudlist.txt`` or ``bbslist.txt``
    :param entry_type: ``"mud"`` or ``"bbs"``
    :returns: list of directory entry dicts
    """
    entries: list[dict[str, object]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            entry = _parse_line(line, entry_type)
            if entry is not None:
                entries.append(entry)
    return entries


def main() -> None:
    """Read modem.xyz lists and write ``telix/data/directory.json``."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    modem_dir = os.path.join(os.path.dirname(project_dir), "modem.xyz")

    if not os.path.exists(modem_dir):
        print(f"Error: {modem_dir} not found")
        print("? git clone https://github.com/jquast/modem.xyz.git ../modem.xyz")
        sys.exit(1)

    mudlist = os.path.join(modem_dir, "mudlist.txt")
    bbslist = os.path.join(modem_dir, "bbslist.txt")

    if not os.path.isfile(mudlist):
        print(f"Error: {mudlist} not found", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(bbslist):
        print(f"Error: {bbslist} not found", file=sys.stderr)
        sys.exit(1)

    entries = parse_file(mudlist, "mud") + parse_file(bbslist, "bbs")

    out_path = os.path.join(project_dir, "telix", "data", "directory.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    print(f"Wrote {len(entries)} entries to {out_path}")


if __name__ == "__main__":
    main()
