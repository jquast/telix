"""Tests for telix.ws_client -- WebSocket client connection logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_ws(messages):
    """Return a mock WebSocket that yields *messages* from async iteration."""
    ws = MagicMock()

    async def _aiter():
        for msg in messages:
            yield msg

    ws.__aiter__ = MagicMock(return_value=_aiter())
    ws.send = AsyncMock()
    return ws


class TestRunGmcpWsEcho:
    """_run_gmcp_ws initialises will_echo and processes IAC ECHO negotiation."""

    async def _run(self, messages, shell_fn, monkeypatch, **kwargs):
        from telix import ws_client

        monkeypatch.setattr("telnetlib3.accessories.function_lookup", lambda _: shell_fn)
        ws = _make_ws(messages)
        defaults = {
            "host": "test.bbs",
            "port": 44512,
            "encoding": "utf-8",
            "encoding_errors": "replace",
            "no_repl": False,
            "raw_mode": None,
            "ansi_keys": False,
            "ascii_eol": False,
            "typescript": "",
            "typescript_mode": "append",
            "shell": "stub",
        }
        defaults.update(kwargs)
        await ws_client._run_gmcp_ws(ws=ws, **defaults)

    @pytest.mark.asyncio
    async def test_raw_mode_sets_will_echo(self, monkeypatch):
        """will_echo is True when raw_mode=True, even without no_repl."""
        captured = []

        async def shell(reader, writer):
            captured.append(writer.will_echo)

        await self._run([], shell, monkeypatch, raw_mode=True, no_repl=False)
        assert captured == [True]

    @pytest.mark.asyncio
    async def test_no_repl_sets_will_echo(self, monkeypatch):
        """will_echo is True when no_repl=True."""
        captured = []

        async def shell(reader, writer):
            captured.append(writer.will_echo)

        await self._run([], shell, monkeypatch, raw_mode=None, no_repl=True)
        assert captured == [True]

    @pytest.mark.asyncio
    async def test_default_will_echo_false(self, monkeypatch):
        """will_echo is False when neither raw_mode nor no_repl is set."""
        captured = []

        async def shell(reader, writer):
            captured.append(writer.will_echo)

        await self._run([], shell, monkeypatch, raw_mode=None, no_repl=False)
        assert captured == [False]

    @pytest.mark.asyncio
    async def test_iac_will_echo_frame_sets_will_echo(self, monkeypatch):
        """IAC WILL ECHO in a BINARY frame updates writer.will_echo to True."""
        captured = []

        async def shell(reader, writer):
            await asyncio.sleep(0)  # yield to let receive_loop process frames
            captured.append(writer.will_echo)

        await self._run([b"\xff\xfb\x01"], shell, monkeypatch, raw_mode=None, no_repl=False)
        assert captured == [True]

    @pytest.mark.asyncio
    async def test_iac_bytes_stripped_from_reader(self, monkeypatch):
        """IAC sequences embedded in BINARY frames are stripped before feeding the reader."""
        received = []

        async def shell(reader, writer):
            await asyncio.sleep(0)  # yield to let receive_loop process frames
            text = await reader.read()
            received.append(text)

        # IAC WILL ECHO prefix + game text: reader should only see "hello"
        await self._run([b"\xff\xfb\x01hello"], shell, monkeypatch, raw_mode=None, no_repl=False)
        assert received == ["hello"]

    @pytest.mark.asyncio
    async def test_iac_only_frame_does_not_feed_empty_string(self, monkeypatch):
        """A BINARY frame consisting entirely of IAC bytes does not enqueue empty string in reader."""
        received = []

        async def shell(reader, writer):
            await asyncio.sleep(0)
            # reader should have no data (eof only, from receive_loop ending)
            text = await reader.read()
            received.append(text)

        await self._run([b"\xff\xfb\x01"], shell, monkeypatch, raw_mode=None, no_repl=False)
        # Empty string means EOF was signalled -- no data frame was queued
        assert received == [""]


class TestAnsiKeysPropagation:
    """ansi_keys is stored on writer.ctx after _run_gmcp_ws setup."""

    @pytest.mark.asyncio
    async def test_ansi_keys_stored_on_ctx(self, monkeypatch):
        """ansi_keys=True is stored on writer.ctx.ansi_keys."""
        from telix import ws_client

        captured = []

        async def shell(reader, writer):
            captured.append(writer.ctx.ansi_keys)

        monkeypatch.setattr("telnetlib3.accessories.function_lookup", lambda _: shell)
        ws = _make_ws([])
        await ws_client._run_gmcp_ws(
            ws=ws,
            host="test.bbs",
            port=23,
            encoding="utf-8",
            encoding_errors="replace",
            no_repl=False,
            raw_mode=None,
            ansi_keys=True,
            ascii_eol=False,
            typescript="",
            typescript_mode="append",
            shell="stub",
        )
        assert captured == [True]

    @pytest.mark.asyncio
    async def test_ansi_keys_false_by_default(self, monkeypatch):
        """ansi_keys=False is stored on writer.ctx.ansi_keys."""
        from telix import ws_client

        captured = []

        async def shell(reader, writer):
            captured.append(writer.ctx.ansi_keys)

        monkeypatch.setattr("telnetlib3.accessories.function_lookup", lambda _: shell)
        ws = _make_ws([])
        await ws_client._run_gmcp_ws(
            ws=ws,
            host="test.bbs",
            port=23,
            encoding="utf-8",
            encoding_errors="replace",
            no_repl=False,
            raw_mode=None,
            ansi_keys=False,
            ascii_eol=False,
            typescript="",
            typescript_mode="append",
            shell="stub",
        )
        assert captured == [False]


class TestRunWsClientRouting:
    """run_ws_client routes to telnet engine when first BINARY frame contains IAC."""

    async def _run(self, monkeypatch, first_msg, subprotocol="gmcp.mudstandards.org"):
        import websockets

        from telix import ws_client

        calls = {}

        async def fake_telnet_over_ws(**kwargs):
            calls["path"] = "telnet"
            calls["first_msg"] = kwargs.get("first_msg")

        async def fake_gmcp_ws(**kwargs):
            calls["path"] = "gmcp"
            calls["first_msg"] = kwargs.get("first_msg")

        monkeypatch.setattr(ws_client, "_run_telnet_over_ws", fake_telnet_over_ws)
        monkeypatch.setattr(ws_client, "_run_gmcp_ws", fake_gmcp_ws)

        ws = MagicMock()
        ws.subprotocol = subprotocol
        ws.recv = AsyncMock(return_value=first_msg)

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=ws)
        cm.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(websockets, "connect", lambda *a, **kw: cm)

        await ws_client.run_ws_client(url="wss://test.bbs:44512")
        return calls

    @pytest.mark.asyncio
    async def test_iac_in_first_frame_routes_to_telnet_engine(self, monkeypatch):
        """First BINARY frame with IAC routes to telnetlib3 engine."""
        calls = await self._run(monkeypatch, first_msg=b"\xff\xfb\x01hello")
        assert calls["path"] == "telnet"

    @pytest.mark.asyncio
    async def test_clean_first_frame_stays_on_gmcp_path(self, monkeypatch):
        """First BINARY frame without IAC stays on GMCP path."""
        calls = await self._run(monkeypatch, first_msg=b"hello world")
        assert calls["path"] == "gmcp"

    @pytest.mark.asyncio
    async def test_first_msg_forwarded_to_handler(self, monkeypatch):
        """first_msg is passed to the selected handler."""
        calls = await self._run(monkeypatch, first_msg=b"\xff\xfb\x01")
        assert calls["first_msg"] == b"\xff\xfb\x01"

    @pytest.mark.asyncio
    async def test_telnet_subprotocol_routes_to_telnet_engine(self, monkeypatch):
        """telnet.mudstandards.org subprotocol always routes to telnet engine."""
        calls = await self._run(monkeypatch, first_msg=b"hello", subprotocol="telnet.mudstandards.org")
        assert calls["path"] == "telnet"

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_gmcp_path(self, monkeypatch):
        """When no initial frame arrives within timeout, GMCP path is used."""
        import websockets

        from telix import ws_client

        calls = {}

        async def fake_telnet_over_ws(**kwargs):
            calls["path"] = "telnet"

        async def fake_gmcp_ws(**kwargs):
            calls["path"] = "gmcp"
            calls["first_msg"] = kwargs.get("first_msg")

        monkeypatch.setattr(ws_client, "_run_telnet_over_ws", fake_telnet_over_ws)
        monkeypatch.setattr(ws_client, "_run_gmcp_ws", fake_gmcp_ws)

        async def slow_recv():
            await asyncio.sleep(10)

        ws = MagicMock()
        ws.subprotocol = "gmcp.mudstandards.org"
        ws.recv = slow_recv

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=ws)
        cm.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(websockets, "connect", lambda *a, **kw: cm)

        await ws_client.run_ws_client(url="wss://test.bbs:44512")
        assert calls == {"path": "gmcp", "first_msg": None}
