"""Tests for telix.ssh_transport -- SSH reader/writer adapters."""

import asyncio
from unittest.mock import MagicMock

import pytest

from telix.ssh_transport import SSHReader, SSHWriter, NullOptionSet


class TestSSHReader:
    """SSHReader provides an async read() interface fed by feed_data/feed_eof."""

    @pytest.mark.asyncio
    async def test_read_returns_fed_data(self):
        """Read() returns data previously fed via feed_data."""
        reader = SSHReader()
        reader.feed_data("hello")
        result = await reader.read(1024)
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_read_blocks_until_data(self):
        """Read() blocks until feed_data is called."""
        reader = SSHReader()
        loop = asyncio.get_event_loop()
        loop.call_later(0.01, reader.feed_data, "delayed")
        result = await asyncio.wait_for(reader.read(1024), timeout=1.0)
        assert result == "delayed"

    @pytest.mark.asyncio
    async def test_read_returns_empty_at_eof(self):
        """Read() returns empty string after feed_eof."""
        reader = SSHReader()
        reader.feed_eof()
        result = await reader.read(1024)
        assert result == ""

    @pytest.mark.asyncio
    async def test_at_eof_false_initially(self):
        """at_eof() returns False before feed_eof."""
        reader = SSHReader()
        assert reader.at_eof() is False

    @pytest.mark.asyncio
    async def test_at_eof_true_after_feed_eof(self):
        """at_eof() returns True after feed_eof."""
        reader = SSHReader()
        reader.feed_eof()
        assert reader.at_eof() is True

    @pytest.mark.asyncio
    async def test_multiple_feeds_returned_in_order(self):
        """Multiple feed_data calls are returned in FIFO order."""
        reader = SSHReader()
        reader.feed_data("aaa")
        reader.feed_data("bbb")
        assert await reader.read(1024) == "aaa"
        assert await reader.read(1024) == "bbb"

    @pytest.mark.asyncio
    async def test_wakeup_waiter_unblocks_read(self):
        """_wakeup_waiter() feeds an empty string to unblock a pending read()."""
        reader = SSHReader()
        loop = asyncio.get_event_loop()
        loop.call_later(0.01, reader._wakeup_waiter)
        result = await asyncio.wait_for(reader.read(1024), timeout=1.0)
        assert result == ""

    @pytest.mark.asyncio
    async def test_wakeup_waiter_does_not_set_eof(self):
        """_wakeup_waiter() unblocks read but does not signal EOF."""
        reader = SSHReader()
        reader._wakeup_waiter()
        await reader.read(1024)
        assert reader.at_eof() is False

    @pytest.mark.asyncio
    async def test_read_returns_empty_when_eof_and_buffer_empty(self):
        """Read() returns empty string immediately when EOF and buffer is drained."""
        reader = SSHReader()
        reader.feed_eof()
        await reader.read(1024)  # drain the None sentinel
        result = await reader.read(1024)
        assert result == ""

    @pytest.mark.asyncio
    async def test_feed_data_string_passthrough(self):
        """feed_data passes the string through unchanged (no encoding)."""
        reader = SSHReader()
        reader.feed_data("hello \xe9")
        result = await reader.read(1024)
        assert result == "hello \xe9"


class TestSSHWriter:
    """SSHWriter wraps an asyncssh process for sending."""

    def _make_writer(self, **kwargs) -> SSHWriter:
        process = MagicMock()
        process.stdin = MagicMock()
        process.change_terminal_size = MagicMock()
        return SSHWriter(process=process, **kwargs)

    def test_write_calls_process_stdin(self):
        """Write() delegates to process.stdin.write."""
        writer = self._make_writer()
        writer.write("hello\r\n")
        writer._process.stdin.write.assert_called_once_with("hello\r\n")

    def test_write_decodes_bytes(self):
        """Write() decodes bytes to str before writing."""
        writer = self._make_writer()
        writer.write(b"hello")
        writer._process.stdin.write.assert_called_once_with("hello")

    def test_write_no_process_is_noop(self):
        """Write() is a no-op when process is None."""
        writer = SSHWriter(process=None)
        writer.write("hello")  # should not raise

    def test_close_closes_stdin(self):
        """Close() closes the process stdin."""
        writer = self._make_writer()
        writer.close()
        writer._process.stdin.close.assert_called_once()

    def test_close_sets_is_closing(self):
        """Close() marks the writer as closing."""
        writer = self._make_writer()
        assert writer.is_closing() is False
        writer.close()
        assert writer.is_closing() is True

    def test_close_idempotent(self):
        """Close() called twice only closes stdin once."""
        writer = self._make_writer()
        writer.close()
        writer.close()
        writer._process.stdin.close.assert_called_once()

    def test_will_echo_default_false(self):
        """will_echo defaults to False."""
        writer = SSHWriter()
        assert writer.will_echo is False

    def test_mode_default_local(self):
        """Mode defaults to 'local' for REPL compatibility."""
        writer = SSHWriter()
        assert writer.mode == "local"

    def test_pending_auth_default_false(self):
        """pending_auth defaults to False."""
        writer = SSHWriter()
        assert writer.pending_auth is False

    def test_auth_response_queue_is_asyncio_queue(self):
        """auth_response_queue is an asyncio.Queue instance."""
        writer = SSHWriter()
        assert isinstance(writer.auth_response_queue, asyncio.Queue)

    def test_change_terminal_size_delegates(self):
        """change_terminal_size() calls process.change_terminal_size."""
        writer = self._make_writer()
        writer.change_terminal_size(80, 24)
        writer._process.change_terminal_size.assert_called_once_with(80, 24)

    def test_change_terminal_size_no_process_is_noop(self):
        """change_terminal_size() is a no-op when process is None."""
        writer = SSHWriter(process=None)
        writer.change_terminal_size(80, 24)  # should not raise

    def test_get_extra_info_peername(self):
        """get_extra_info('peername') returns configured peername."""
        writer = SSHWriter(peername=("bbs.example.com", 22))
        assert writer.get_extra_info("peername") == ("bbs.example.com", 22)

    def test_get_extra_info_ssl_object_is_none(self):
        """get_extra_info('ssl_object') returns None."""
        writer = SSHWriter()
        assert writer.get_extra_info("ssl_object") is None

    def test_get_extra_info_unknown_returns_default(self):
        """get_extra_info with unknown key returns default."""
        writer = SSHWriter()
        assert writer.get_extra_info("nonexistent", "fallback") == "fallback"

    def test_process_property_setter(self):
        """Process property setter updates the underlying process."""
        writer = SSHWriter()
        assert writer.process is None
        mock_proc = MagicMock()
        writer.process = mock_proc
        assert writer.process is mock_proc

    def test_null_option_set_always_false(self):
        """NullOptionSet.enabled() always returns False."""
        nos = NullOptionSet()
        assert nos.enabled(b"\x55") is False
        assert nos.enabled(None) is False

    def test_local_option_is_null_option_set(self):
        """local_option is a NullOptionSet instance."""
        writer = SSHWriter()
        assert isinstance(writer.local_option, NullOptionSet)

    def test_remote_option_is_null_option_set(self):
        """remote_option is a NullOptionSet instance."""
        writer = SSHWriter()
        assert isinstance(writer.remote_option, NullOptionSet)

    def test_client_flag_is_true(self):
        """Client attribute is True for REPL compatibility."""
        writer = SSHWriter()
        assert writer.client is True

    def test_send_naws_is_noop(self):
        """_send_naws() does not raise."""
        writer = SSHWriter()
        writer._send_naws()  # should not raise
