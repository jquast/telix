"""Tests for directory loader and import script."""

from __future__ import annotations

# std imports
import os
import sys
import json
import ipaddress
from typing import Any

# 3rd party
import pytest

from telix.directory import load_directory, load_favorites, directory_to_sessions

# local
from telix.client_tui import SessionConfig

_TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from import_modem_xyz import parse_file, _parse_line


class TestLoadDirectory:
    def test_returns_list(self) -> None:
        entries = load_directory()
        assert isinstance(entries, list)
        assert len(entries) > 100

    def test_entry_has_required_fields(self) -> None:
        entries = load_directory()
        for entry in entries[:10]:
            assert "host" in entry
            assert "port" in entry
            assert "name" in entry
            assert "type" in entry

    def test_entry_types(self) -> None:
        entries = load_directory()
        types = {e["type"] for e in entries}
        assert types == {"mud", "bbs"}

    def test_ssl_entries_present(self) -> None:
        entries = load_directory()
        ssl_entries = [e for e in entries if e.get("ssl")]
        assert len(ssl_entries) > 0

    def test_no_ip_addresses(self) -> None:
        entries = load_directory()
        for entry in entries:
            try:
                ipaddress.ip_address(entry["host"])
                raise AssertionError(f"IP address found: {entry['host']}")
            except ValueError:
                pass


class TestLoadFavorites:
    def test_returns_list(self) -> None:
        entries = load_favorites()
        assert isinstance(entries, list)
        assert len(entries) > 0

    def test_entry_fields(self) -> None:
        entries = load_favorites()
        for entry in entries:
            assert "host" in entry
            assert "port" in entry
            assert "name" in entry
            assert "type" in entry

    def test_topaz_alias(self) -> None:
        from telix.directory import _ENCODING_ALIASES

        assert _ENCODING_ALIASES["topaz"] == "latin1"

    def test_port_is_int(self) -> None:
        entries = load_favorites()
        for entry in entries:
            assert isinstance(entry["port"], int)

    def test_types(self) -> None:
        entries = load_favorites()
        types = {e["type"] for e in entries}
        assert types == {"mud", "bbs"}

    def test_encoding_override(self) -> None:
        entries = load_favorites()
        by_name = {e["name"]: e for e in entries}
        assert by_name["Absinthe BBS"]["encoding"] == "latin1"

    def test_websocket_entry_fields(self) -> None:
        entries = load_favorites()
        by_name = {e["name"]: e for e in entries}
        cryosphere = by_name["Cryosphere"]
        assert cryosphere["protocol"] == "websocket"
        assert cryosphere["ws_path"] == "/telnet/"
        assert cryosphere["ssl"] is True


class TestDirectoryToSessions:
    def test_returns_session_configs(self) -> None:
        sessions = directory_to_sessions()
        assert isinstance(sessions, dict)
        assert len(sessions) > 100
        for key, cfg in list(sessions.items())[:5]:
            assert isinstance(cfg, SessionConfig)
            assert ":" in key
            assert cfg.host in key

    def test_ssl_mapped(self) -> None:
        sessions = directory_to_sessions()
        ssl_sessions = {k: v for k, v in sessions.items() if v.ssl}
        assert len(ssl_sessions) > 0

    def test_encoding_mapped(self) -> None:
        sessions = directory_to_sessions()
        encodings = {v.encoding for v in sessions.values()}
        assert "gbk" in encodings
        assert "big5" in encodings

    def test_key_format(self) -> None:
        sessions = directory_to_sessions()
        for key in list(sessions.keys())[:20]:
            host, port = key.rsplit(":", 1)
            assert host
            assert port.isdigit()

    def test_websocket_session_mapped(self) -> None:
        sessions = directory_to_sessions()
        cfg = sessions["dev.cryosphere.org:4443"]
        assert cfg.protocol == "websocket"
        assert cfg.ws_path == "/telnet/"
        assert cfg.ssl is True
        assert cfg.bookmarked is True

    def test_favorites_bookmarked(self) -> None:
        sessions = directory_to_sessions()
        favorites = load_favorites()
        for fav in favorites:
            key = f"{fav['host']}:{fav.get('port', 23)}"
            assert sessions[key].bookmarked is True

    def test_favorites_new_entry_added(self) -> None:
        sessions = directory_to_sessions()
        favorites = load_favorites()
        directory_keys = {f"{e['host']}:{e.get('port', 23)}" for e in load_directory()}
        fav_only = [f for f in favorites if f"{f['host']}:{f.get('port', 23)}" not in directory_keys]
        for fav in fav_only:
            key = f"{fav['host']}:{fav.get('port', 23)}"
            assert key in sessions
            assert sessions[key].bookmarked is True
            assert sessions[key].name == fav["name"]

    def test_bbs_presets_applied(self) -> None:
        sessions = directory_to_sessions()
        bbs = next(v for v in sessions.values() if v.server_type == "bbs")
        assert bbs.colormatch == "vga"
        assert bbs.ice_colors is True
        assert bbs.compression is None

    def test_mud_presets_applied(self) -> None:
        sessions = directory_to_sessions()
        mud = next(v for v in sessions.values() if v.compression is True)
        assert mud.colormatch == "none"
        assert mud.ice_colors is False
        assert mud.mode == "line"
        assert mud.no_repl is False


class TestParseLine:
    def test_comment_line(self) -> None:
        assert _parse_line("# comment", "mud") is None

    def test_blank_line(self) -> None:
        assert _parse_line("", "mud") is None

    def test_host_only(self) -> None:
        entry = _parse_line("example.org", "mud")
        assert entry is not None
        assert entry["host"] == "example.org"
        assert entry["port"] == 23
        assert entry["type"] == "mud"
        assert "ssl" not in entry

    def test_ip_address_skipped(self) -> None:
        assert _parse_line("192.168.1.1 4000", "mud") is None

    def test_ipv6_address_skipped(self) -> None:
        assert _parse_line("::1 4000", "mud") is None

    def test_host_port(self) -> None:
        entry = _parse_line("example.org 4000", "bbs")
        assert entry is not None
        assert entry["port"] == 4000
        assert entry["type"] == "bbs"

    def test_host_port_encoding(self) -> None:
        entry = _parse_line("example.org 4000 gbk", "mud")
        assert entry is not None
        assert entry["encoding"] == "gbk"

    def test_host_port_encoding_columns(self) -> None:
        entry = _parse_line("example.org 4000 utf-8 100", "mud")
        assert entry is not None
        assert entry["columns"] == 100
        assert "encoding" not in entry  # utf-8 is default, omitted

    def test_ssl_flag(self) -> None:
        entry = _parse_line("example.org 2000 ssl", "mud")
        assert entry is not None
        assert entry["ssl"] is True
        assert entry["port"] == 2000

    def test_ssl_with_encoding(self) -> None:
        entry = _parse_line("example.org 3334 utf-8 90 ssl", "mud")
        assert entry is not None
        assert entry["ssl"] is True
        assert entry["columns"] == 90

    def test_tall_flag(self) -> None:
        entry = _parse_line("example.org 23 cp437 80 tall", "bbs")
        assert entry is not None
        assert entry["rows"] == 1
        assert entry["columns"] == 80
        assert entry["encoding"] == "cp437"

    @pytest.mark.parametrize("encoding", ["gbk", "big5", "cp437", "latin-1", "atascii", "petscii"])
    def test_known_encodings(self, encoding: str) -> None:
        entry = _parse_line(f"host.example 1234 {encoding}", "mud")
        assert entry is not None
        assert entry["encoding"] == encoding

    def test_topaz_becomes_latin1(self) -> None:
        entry = _parse_line("host.example 1234 topaz", "bbs")
        assert entry is not None
        assert entry["encoding"] == "latin1"


class TestParseFile:
    def test_mudlist(self) -> None:
        mudlist = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "..", "modem.xyz", "mudlist.txt"
        )
        if not os.path.isfile(mudlist):
            pytest.skip("modem.xyz not available")
        entries = parse_file(mudlist, "mud")
        assert len(entries) > 100
        assert all(e["type"] == "mud" for e in entries)

    def test_bbslist(self) -> None:
        bbslist = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "..", "modem.xyz", "bbslist.txt"
        )
        if not os.path.isfile(bbslist):
            pytest.skip("modem.xyz not available")
        entries = parse_file(bbslist, "bbs")
        assert len(entries) > 100
        assert all(e["type"] == "bbs" for e in entries)
