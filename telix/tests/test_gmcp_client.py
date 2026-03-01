"""Tests for GMCP client integration."""

# std imports
import sys
from unittest import mock

# 3rd party
import pytest
from telnetlib3.client import _DEFAULT_GMCP_MODULES, TelnetClient, _get_argument_parser
from telnetlib3.telopt import GMCP

_CLIENT_DEFAULTS = {
    "encoding": "utf8",
    "encoding_errors": "strict",
    "force_binary": False,
    "connect_maxwait": 0.02,
}


class _MockTransport:
    def __init__(self):
        self.data = bytearray()
        self._closing = False

    def write(self, data):
        self.data.extend(data)

    def is_closing(self):
        return self._closing

    def close(self):
        self._closing = True

    def get_extra_info(self, name, default=None):
        return default


def _make_client(**kwargs):
    return TelnetClient(**{**_CLIENT_DEFAULTS, **kwargs})


def _make_connected_client(**kwargs):
    client = _make_client(**kwargs)
    transport = _MockTransport()
    client.connection_made(transport)
    return client, transport


@pytest.mark.asyncio
async def test_default_gmcp_data_dict():
    client, _ = _make_connected_client()
    assert not client.writer.ctx.gmcp_data


@pytest.mark.asyncio
async def test_default_gmcp_modules():
    client = _make_client()
    assert client._gmcp_modules == _DEFAULT_GMCP_MODULES


@pytest.mark.asyncio
async def test_custom_gmcp_modules():
    modules = ["Char 1", "IRE.Rift 1"]
    client = _make_client(gmcp_modules=modules)
    assert client._gmcp_modules == modules


@pytest.mark.asyncio
async def test_gmcp_log_default_false():
    client = _make_client()
    assert client._gmcp_log is False


@pytest.mark.asyncio
async def test_gmcp_log_enabled():
    client = _make_client(gmcp_log=True)
    assert client._gmcp_log is True


@pytest.mark.asyncio
async def test_gmcp_data_on_writer():
    client, _ = _make_connected_client()
    assert client.writer.ctx.gmcp_data is not None
    assert isinstance(client.writer.ctx.gmcp_data, dict)


@pytest.mark.asyncio
async def test_ext_callback_registered_for_gmcp():
    client, _ = _make_connected_client()
    assert client.writer._ext_callback[GMCP] == client.on_gmcp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup_calls,key,expected",
    [
        ([("Char.Vitals", {"hp": 100, "maxhp": 100})], "Char.Vitals", {"hp": 100, "maxhp": 100}),
        (
            [("Room.Info", {"name": "Town Square"}), ("Room.Info", {"name": "Dark Forest"})],
            "Room.Info",
            {"name": "Dark Forest"},
        ),
        (
            [
                ("Char.Vitals", {"hp": 100, "maxhp": 100, "sp": 50, "maxsp": 50}),
                ("Char.Vitals", {"hp": 63}),
            ],
            "Char.Vitals",
            {"hp": 63, "maxhp": 100, "sp": 50, "maxsp": 50},
        ),
        (
            [("Room.Name", "Old Name"), ("Room.Name", {"name": "New Place"})],
            "Room.Name",
            {"name": "New Place"},
        ),
        (
            [("Room.Info", {"name": "Town"}), ("Room.Info", "plain string")],
            "Room.Info",
            "plain string",
        ),
        ([("Core.Goodbye", None)], "Core.Goodbye", None),
    ],
)
async def test_on_gmcp_data_storage(setup_calls, key, expected):
    client, _ = _make_connected_client()
    for module, data in setup_calls:
        client.on_gmcp(module, data)
    assert client.writer.ctx.gmcp_data[key] == expected


@pytest.mark.asyncio
async def test_on_gmcp_logs_debug_by_default():
    client, _ = _make_connected_client()
    with mock.patch.object(client.log, "debug") as mock_debug:
        client.on_gmcp("Char.Vitals", {"hp": 50})
        mock_debug.assert_called_once()


@pytest.mark.asyncio
async def test_on_gmcp_logs_info_when_enabled():
    client, _ = _make_connected_client(gmcp_log=True)
    with mock.patch.object(client.log, "info") as mock_info:
        client.on_gmcp("Char.Vitals", {"hp": 50})
        mock_info.assert_called_once()


def _install_telix_gmcp_wrapper(client):
    """Install the same GMCP dispatch wrapper that telix_client_shell uses."""
    from telnetlib3.telopt import GMCP as _GMCP

    ctx = client.writer.ctx
    base = client.writer._ext_callback.get(_GMCP)

    def _wrapper(package, data):
        if base is not None:
            base(package, data)
        if package == "Comm.Channel.Text":
            if ctx.on_chat_text is not None:
                ctx.on_chat_text(data)
        elif package == "Comm.Channel.List":
            if ctx.on_chat_channels is not None:
                ctx.on_chat_channels(data)
        elif package == "Room.Info":
            if ctx.on_room_info is not None:
                ctx.on_room_info(data)

    client.writer.set_ext_callback(_GMCP, _wrapper)


@pytest.mark.asyncio
async def test_on_gmcp_dispatches_chat_text_callback():
    client, _ = _make_connected_client()
    _install_telix_gmcp_wrapper(client)
    received = []
    client.writer.ctx.on_chat_text = lambda data: received.append(data)
    msg = {"channel": "chat", "talker": "Bob", "text": "hi\n"}
    client.writer._ext_callback[GMCP]("Comm.Channel.Text", msg)
    assert received == [msg]


@pytest.mark.asyncio
async def test_on_gmcp_dispatches_chat_channels_callback():
    client, _ = _make_connected_client()
    _install_telix_gmcp_wrapper(client)
    received = []
    client.writer.ctx.on_chat_channels = lambda data: received.append(data)
    channels = [{"name": "chat", "command": "chat"}]
    client.writer._ext_callback[GMCP]("Comm.Channel.List", channels)
    assert received == [channels]


@pytest.mark.asyncio
async def test_on_gmcp_dispatches_room_info_callback():
    client, _ = _make_connected_client()
    _install_telix_gmcp_wrapper(client)
    received = []
    client.writer.ctx.on_room_info = lambda data: received.append(data)
    info = {"num": "abc123", "name": "Dark Forest", "exits": {"north": "xyz"}}
    client.writer._ext_callback[GMCP]("Room.Info", info)
    assert received == [info]


@pytest.mark.asyncio
async def test_hello_sent_on_will_gmcp():
    client, transport = _make_connected_client()
    client.writer.always_do = {GMCP}
    transport.data.clear()
    client.writer.handle_will(GMCP)
    data = bytes(transport.data)
    assert b"Core.Hello" in data
    assert b"Core.Supports.Set" in data


@pytest.mark.asyncio
async def test_hello_idempotent():
    client, transport = _make_connected_client()
    client.writer.always_do = {GMCP}
    client.writer.handle_will(GMCP)
    transport.data.clear()
    client.writer.remote_option[GMCP] = True
    client.writer.handle_will(GMCP)
    data = bytes(transport.data)
    assert b"Core.Hello" not in data


@pytest.mark.asyncio
async def test_hello_includes_version():
    from telnetlib3.accessories import get_version

    client, transport = _make_connected_client()
    client.writer.always_do = {GMCP}
    transport.data.clear()
    client.writer.handle_will(GMCP)
    data = bytes(transport.data)
    assert get_version().encode() in data


@pytest.mark.asyncio
async def test_hello_uses_custom_modules():
    modules = ["IRE.Rift 1", "Char 1"]
    client, transport = _make_connected_client(gmcp_modules=modules)
    client.writer.always_do = {GMCP}
    transport.data.clear()
    client.writer.handle_will(GMCP)
    data = bytes(transport.data)
    assert b"IRE.Rift 1" in data


@pytest.mark.asyncio
async def test_no_hello_without_always_do():
    client, transport = _make_connected_client()
    transport.data.clear()
    client.writer.handle_will(GMCP)
    data = bytes(transport.data)
    assert b"Core.Hello" not in data


def test_gmcp_modules_cli_flag():
    parser = _get_argument_parser()
    args = parser.parse_args(["example.com", "--gmcp-modules", "Char 1,Room 1"])
    assert args.gmcp_modules == "Char 1,Room 1"


def test_gmcp_modules_cli_default_none():
    parser = _get_argument_parser()
    args = parser.parse_args(["example.com"])
    assert args.gmcp_modules is None


def test_gmcp_log_cli_flag():
    parser = _get_argument_parser()
    args = parser.parse_args(["example.com", "--gmcp-log"])
    assert args.gmcp_log is True


def test_gmcp_log_cli_default_false():
    parser = _get_argument_parser()
    args = parser.parse_args(["example.com"])
    assert args.gmcp_log is False


def test_transform_args_gmcp_modules():
    from telnetlib3.client import _transform_args

    parser = _get_argument_parser()
    args = parser.parse_args(["example.com", "--gmcp-modules", "Char 1,IRE.Rift 1"])
    result = _transform_args(args)
    assert result["gmcp_modules"] == ["Char 1", "IRE.Rift 1"]


def test_transform_args_gmcp_modules_none():
    from telnetlib3.client import _transform_args

    parser = _get_argument_parser()
    args = parser.parse_args(["example.com"])
    result = _transform_args(args)
    assert result["gmcp_modules"] is None


if sys.platform != "win32":

    def test_vital_bar_shows_vitals():
        pytest.importorskip("blessed")
        from telix.client_repl import _segmented, _vital_bar

        bars = _vital_bar(100, 200, 16, "hp")
        text = "".join(t for _, t in bars)
        assert _segmented("100/200") in text
        assert _segmented("50%") in text

    def test_vital_bar_hp_only():
        pytest.importorskip("blessed")
        from telix.client_repl import _segmented, _vital_bar

        bars = _vital_bar(50, None, 16, "hp")
        text = "".join(t for _, t in bars)
        assert _segmented("50") in text
        assert "hp" in text

    def test_vital_bar_full():
        pytest.importorskip("blessed")
        from telix.client_repl import _segmented, _vital_bar

        bars = _vital_bar(100, 100, 16, "hp")
        text = "".join(t for _, t in bars)
        assert _segmented("100/100") in text
        assert _segmented("100%") in text

    def test_vital_bar_returns_sgr():
        pytest.importorskip("blessed")
        from telix.client_repl import _segmented, _vital_bar

        bars = _vital_bar(50, 100, 16, "mp")
        for sgr, _text in bars:
            assert not sgr.startswith("fg:#")
            assert not sgr.startswith("bg:#")
