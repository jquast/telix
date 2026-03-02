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

# local
from telix.directory import load_directory, directory_to_sessions


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


class TestDirectoryToSessions:
    def test_returns_session_configs(self) -> None:
        from telix.client_tui import SessionConfig

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

    def test_default_bookmark(self) -> None:
        sessions = directory_to_sessions()
        assert sessions["1984.ws:23"].bookmarked is True


class TestParseLine:
    @pytest.fixture(autouse=True)
    def import_parser(self) -> None:
        tools_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tools"
        )
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)

    def test_comment_line(self) -> None:
        from import_modem_xyz import _parse_line

        assert _parse_line("# comment", "mud") is None

    def test_blank_line(self) -> None:
        from import_modem_xyz import _parse_line

        assert _parse_line("", "mud") is None

    def test_host_only(self) -> None:
        from import_modem_xyz import _parse_line

        entry = _parse_line("example.org", "mud")
        assert entry is not None
        assert entry["host"] == "example.org"
        assert entry["port"] == 23
        assert entry["type"] == "mud"
        assert "ssl" not in entry

    def test_ip_address_skipped(self) -> None:
        from import_modem_xyz import _parse_line

        assert _parse_line("192.168.1.1 4000", "mud") is None

    def test_ipv6_address_skipped(self) -> None:
        from import_modem_xyz import _parse_line

        assert _parse_line("::1 4000", "mud") is None

    def test_host_port(self) -> None:
        from import_modem_xyz import _parse_line

        entry = _parse_line("example.org 4000", "bbs")
        assert entry is not None
        assert entry["port"] == 4000
        assert entry["type"] == "bbs"

    def test_host_port_encoding(self) -> None:
        from import_modem_xyz import _parse_line

        entry = _parse_line("example.org 4000 gbk", "mud")
        assert entry is not None
        assert entry["encoding"] == "gbk"

    def test_host_port_encoding_columns(self) -> None:
        from import_modem_xyz import _parse_line

        entry = _parse_line("example.org 4000 utf-8 100", "mud")
        assert entry is not None
        assert entry["columns"] == 100
        assert "encoding" not in entry  # utf-8 is default, omitted

    def test_ssl_flag(self) -> None:
        from import_modem_xyz import _parse_line

        entry = _parse_line("example.org 2000 ssl", "mud")
        assert entry is not None
        assert entry["ssl"] is True
        assert entry["port"] == 2000

    def test_ssl_with_encoding(self) -> None:
        from import_modem_xyz import _parse_line

        entry = _parse_line("example.org 3334 utf-8 90 ssl", "mud")
        assert entry is not None
        assert entry["ssl"] is True
        assert entry["columns"] == 90

    def test_tall_flag(self) -> None:
        from import_modem_xyz import _parse_line

        entry = _parse_line("example.org 23 cp437 80 tall", "bbs")
        assert entry is not None
        assert entry["rows"] == 1
        assert entry["columns"] == 80
        assert entry["encoding"] == "cp437"

    @pytest.mark.parametrize("encoding", ["gbk", "big5", "cp437", "latin-1", "atascii", "petscii"])
    def test_known_encodings(self, encoding: str) -> None:
        from import_modem_xyz import _parse_line

        entry = _parse_line(f"host.example 1234 {encoding}", "mud")
        assert entry is not None
        assert entry["encoding"] == encoding


class TestParseFile:
    @pytest.fixture(autouse=True)
    def import_parser(self) -> None:
        tools_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tools"
        )
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)

    def test_mudlist(self) -> None:
        from import_modem_xyz import parse_file

        mudlist = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "..",
            "modem.xyz",
            "mudlist.txt",
        )
        if not os.path.isfile(mudlist):
            pytest.skip("modem.xyz not available")
        entries = parse_file(mudlist, "mud")
        assert len(entries) > 100
        assert all(e["type"] == "mud" for e in entries)

    def test_bbslist(self) -> None:
        from import_modem_xyz import parse_file

        bbslist = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "..",
            "modem.xyz",
            "bbslist.txt",
        )
        if not os.path.isfile(bbslist):
            pytest.skip("modem.xyz not available")
        entries = parse_file(bbslist, "bbs")
        assert len(entries) > 100
        assert all(e["type"] == "bbs" for e in entries)
