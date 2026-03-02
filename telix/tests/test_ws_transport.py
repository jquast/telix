"""Tests for telix.ws_transport -- WebSocket reader/writer adapters."""

import json
import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
import websockets.exceptions

from telix.ws_transport import WebSocketReader, WebSocketWriter, parse_gmcp_frame


class TestWebSocketReader:
    """WebSocketReader provides an async read() interface fed by feed_data/feed_eof."""

    @pytest.mark.asyncio
    async def test_read_returns_fed_data(self):
        """Read() returns data previously fed via feed_data."""
        reader = WebSocketReader()
        reader.feed_data(b"hello")
        result = await reader.read(1024)
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_read_blocks_until_data(self):
        """Read() blocks until feed_data is called."""
        reader = WebSocketReader()
        loop = asyncio.get_event_loop()
        loop.call_later(0.01, reader.feed_data, b"delayed")
        result = await asyncio.wait_for(reader.read(1024), timeout=1.0)
        assert result == "delayed"

    @pytest.mark.asyncio
    async def test_read_returns_empty_at_eof(self):
        """Read() returns empty string after feed_eof."""
        reader = WebSocketReader()
        reader.feed_eof()
        result = await reader.read(1024)
        assert result == ""

    @pytest.mark.asyncio
    async def test_at_eof(self):
        """at_eof() reflects EOF state."""
        reader = WebSocketReader()
        assert reader.at_eof() is False
        reader.feed_eof()
        assert reader.at_eof() is True

    @pytest.mark.asyncio
    async def test_multiple_feeds_concatenate(self):
        """Multiple feed_data calls are returned in order."""
        reader = WebSocketReader()
        reader.feed_data(b"aaa")
        reader.feed_data(b"bbb")
        result = await reader.read(1024)
        assert result == "aaa"
        result = await reader.read(1024)
        assert result == "bbb"

    @pytest.mark.asyncio
    async def test_feed_data_decodes_utf8(self):
        """Binary data is decoded as UTF-8."""
        reader = WebSocketReader()
        reader.feed_data(b"hello \xc3\xa9")
        result = await reader.read(1024)
        assert result == "hello \xe9"

    @pytest.mark.asyncio
    async def test_wakeup_waiter_unblocks_read(self):
        """_wakeup_waiter() feeds an empty string to unblock a pending read()."""
        reader = WebSocketReader()
        loop = asyncio.get_event_loop()
        loop.call_later(0.01, reader._wakeup_waiter)
        result = await asyncio.wait_for(reader.read(1024), timeout=1.0)
        assert result == ""

    @pytest.mark.asyncio
    async def test_wakeup_waiter_does_not_set_eof(self):
        """_wakeup_waiter() unblocks read but does not signal EOF."""
        reader = WebSocketReader()
        reader._wakeup_waiter()
        await reader.read(1024)
        assert reader.at_eof() is False


class TestWebSocketWriter:
    """WebSocketWriter wraps a websockets connection for sending."""

    def _make_writer(self, **overrides: Any) -> WebSocketWriter:
        ws = MagicMock()
        ws.send = MagicMock()
        return WebSocketWriter(ws, **overrides)

    def test_write_queues_binary_frame(self):
        """Write() enqueues text encoded as bytes for the drain task."""
        writer = self._make_writer()
        writer.write("hello\r\n")
        item = writer._send_queue.get_nowait()
        assert item == b"hello\r\n"

    def test_write_passes_bytes_through(self):
        """Write() with bytes input passes them through without re-encoding."""
        writer = self._make_writer()
        raw = b"\xff\xfe\x01\x02"
        writer.write(raw)
        item = writer._send_queue.get_nowait()
        assert item is raw

    def test_will_echo_default_false(self):
        """will_echo defaults to False (server not echoing)."""
        writer = self._make_writer()
        assert writer.will_echo is False

    def test_mode_default_local(self):
        """Mode defaults to 'local' for REPL compatibility."""
        writer = self._make_writer()
        assert writer.mode == "local"

    def test_get_extra_info_peername(self):
        """get_extra_info returns configured peername."""
        writer = self._make_writer(peername=("gel.monster", 8443))
        assert writer.get_extra_info("peername") == ("gel.monster", 8443)

    def test_get_extra_info_default(self):
        """get_extra_info returns default for unknown keys."""
        writer = self._make_writer()
        assert writer.get_extra_info("unknown", "fallback") == "fallback"

    def test_get_extra_info_ssl_object_returns_none(self):
        """get_extra_info('ssl_object') always returns None (no TLS on inner WS)."""
        writer = self._make_writer()
        assert writer.get_extra_info("ssl_object") is None

    def test_get_extra_info_peername_default_when_unset(self):
        """get_extra_info('peername') returns default when peername not configured."""
        writer = self._make_writer()
        assert writer.get_extra_info("peername", "fallback") == "fallback"

    def test_close_signals_drain_to_stop(self):
        """Close() places a None sentinel on the send queue."""
        writer = self._make_writer()
        writer.close()
        assert writer.is_closing() is True
        item = writer._send_queue.get_nowait()
        assert item is None

    def test_set_ext_callback(self):
        """set_ext_callback stores callback by key."""
        writer = self._make_writer()
        cb = MagicMock()
        writer.set_ext_callback(b"\xc9", cb)
        assert writer._ext_callback[b"\xc9"] is cb

    def test_set_iac_callback(self):
        """set_iac_callback stores callback by key."""
        writer = self._make_writer()
        cb = MagicMock()
        writer.set_iac_callback(b"\xf9", cb)
        assert writer._iac_callback[b"\xf9"] is cb

    def test_log_attribute(self):
        """Writer exposes a log attribute."""
        writer = self._make_writer()
        assert writer.log is not None

    def test_local_option_enabled_returns_false(self):
        """local_option.enabled() returns False for any telnet option."""
        writer = self._make_writer()
        assert writer.local_option.enabled(b"\x18") is False
        assert writer.local_option.enabled(b"\x01") is False

    def test_remote_option_enabled_returns_false(self):
        """remote_option.enabled() returns False for any telnet option."""
        writer = self._make_writer()
        assert writer.remote_option.enabled(b"\x18") is False
        assert writer.remote_option.enabled(b"\x01") is False


class TestGMCPDispatch:
    """GMCP TEXT frames are dispatched to ext callbacks."""

    def test_gmcp_text_frame_dispatched(self):
        """A TEXT frame triggers the GMCP ext callback."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        received: list[tuple[str, Any]] = []
        writer.set_ext_callback(b"\xc9", lambda pkg, data: received.append((pkg, data)))
        writer.dispatch_gmcp("Room.Info", {"num": "1", "name": "Town"})
        assert len(received) == 1
        assert received[0] == ("Room.Info", {"num": "1", "name": "Town"})

    def test_gmcp_no_callback_no_error(self):
        """dispatch_gmcp with no registered callback does not raise."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        writer.dispatch_gmcp("Room.Info", {"num": "1"})

    def test_gmcp_string_payload(self):
        """dispatch_gmcp handles string-only GMCP payload."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        received: list[tuple[str, Any]] = []
        writer.set_ext_callback(b"\xc9", lambda pkg, data: received.append((pkg, data)))
        writer.dispatch_gmcp("Core.Goodbye", None)
        assert received[0] == ("Core.Goodbye", None)


class TestPseudoPromptSignal:
    """WebSocket message boundaries fire GA/EOR callbacks as pseudo-prompt."""

    def test_prompt_signal_fires_ga_callback(self):
        """fire_prompt_signal invokes the GA IAC callback when no EOR registered."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        calls: list[bytes] = []
        writer.set_iac_callback(b"\xf9", calls.append)
        writer.fire_prompt_signal()
        assert calls == [b"\xf9"]

    def test_prompt_signal_fires_eor_callback(self):
        """fire_prompt_signal invokes the EOR IAC callback."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        calls: list[bytes] = []
        writer.set_iac_callback(b"\xef", calls.append)
        writer.fire_prompt_signal()
        assert calls == [b"\xef"]

    def test_prompt_signal_prefers_eor_over_ga(self):
        """fire_prompt_signal fires only EOR when both GA and EOR are registered."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        calls: list[bytes] = []
        writer.set_iac_callback(b"\xf9", calls.append)
        writer.set_iac_callback(b"\xef", calls.append)
        writer.fire_prompt_signal()
        assert calls == [b"\xef"]

    def test_prompt_signal_no_callbacks_no_error(self):
        """fire_prompt_signal with no registered callbacks does not raise."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        writer.fire_prompt_signal()


class TestParseGMCPFrame:
    """parse_gmcp_frame extracts package name and JSON payload from TEXT frames."""

    def test_package_with_json_object(self):
        """Standard GMCP: 'Package.Name {...}'."""
        pkg, data = parse_gmcp_frame('Room.Info {"num":"1","name":"Town"}')
        assert pkg == "Room.Info"
        assert data == {"num": "1", "name": "Town"}

    def test_package_with_json_array(self):
        """GMCP with array payload."""
        pkg, data = parse_gmcp_frame('Comm.Channel.List [{"name":"chat"}]')
        assert pkg == "Comm.Channel.List"
        assert data == [{"name": "chat"}]

    def test_package_no_payload(self):
        """GMCP with no JSON payload returns None."""
        pkg, data = parse_gmcp_frame("Core.Goodbye")
        assert pkg == "Core.Goodbye"
        assert data is None

    def test_package_with_string_payload(self):
        """GMCP with a JSON string payload."""
        pkg, data = parse_gmcp_frame('Core.Hello "telnetlib3"')
        assert pkg == "Core.Hello"
        assert data == "telnetlib3"

    def test_package_with_numeric_payload(self):
        """GMCP with a numeric payload."""
        pkg, data = parse_gmcp_frame("Char.Level 42")
        assert pkg == "Char.Level"
        assert data == 42

    def test_empty_string_raises(self):
        """Empty string raises ValueError."""
        with pytest.raises(ValueError):
            parse_gmcp_frame("")

    def test_malformed_json_returns_raw_string(self):
        """Malformed JSON payload is returned as raw string."""
        pkg, data = parse_gmcp_frame("Foo.Bar {bad json}")
        assert pkg == "Foo.Bar"
        assert data == "{bad json}"


class TestSendGMCP:
    """WebSocketWriter.send_gmcp enqueues TEXT frames in GMCP format."""

    def test_send_gmcp_with_dict(self):
        """send_gmcp enqueues 'Package.Name json' for the drain task."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        writer.send_gmcp("Core.Supports.Set", ["Room 1", "Char 1"])
        sent = writer._send_queue.get_nowait()
        assert isinstance(sent, str)
        assert sent.startswith("Core.Supports.Set ")
        assert json.loads(sent.split(" ", 1)[1]) == ["Room 1", "Char 1"]

    def test_send_gmcp_no_payload(self):
        """send_gmcp with None payload enqueues package name only."""
        ws = MagicMock()
        writer = WebSocketWriter(ws)
        writer.send_gmcp("Core.Goodbye", None)
        sent = writer._send_queue.get_nowait()
        assert sent == "Core.Goodbye"


class TestDrain:
    """WebSocketWriter.drain() sends queued items over the WebSocket."""

    @pytest.mark.asyncio
    async def test_drain_sends_queued_items(self):
        """Drain() awaits ws.send() for each queued item in order."""
        ws = MagicMock()
        sent: list[Any] = []

        async def fake_send(item):
            sent.append(item)

        ws.send = fake_send
        writer = WebSocketWriter(ws)
        writer.write("hello")
        writer.send_gmcp("Core.Ping")
        writer.close()
        await writer.drain()
        assert sent == [b"hello", "Core.Ping"]

    @pytest.mark.asyncio
    async def test_drain_stops_on_close(self):
        """Drain() exits when close() places a None sentinel."""
        ws = MagicMock()
        ws.send = MagicMock(side_effect=lambda _: asyncio.sleep(0))
        writer = WebSocketWriter(ws)
        writer.close()
        await asyncio.wait_for(writer.drain(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_drain_handles_connection_closed(self):
        """Drain() exits cleanly when send raises ConnectionClosed."""
        ws = MagicMock()

        async def send_raises(_):
            raise websockets.exceptions.ConnectionClosed(None, None)

        ws.send = send_raises
        writer = WebSocketWriter(ws)
        writer.write("hello")
        writer.close()
        await asyncio.wait_for(writer.drain(), timeout=1.0)
