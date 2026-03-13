"""Tests for MTTS and MNES protocol support."""

import pytest

from telix import mtts


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, 1807),
        ({"ssl": True}, 3855),
        ({"ansi": False}, 1806),
        ({"utf8": False}, 1803),
        ({"mouse_tracking": True}, 1823),
        ({"truecolor": False}, 1551),
        ({"mnes": False}, 1295),
        (
            {
                "ansi": False,
                "vt100": False,
                "utf8": False,
                "colors_256": False,
                "truecolor": False,
                "mnes": False,
                "mslp": False,
            },
            0,
        ),
        ({"mslp": False}, 783),
    ],
)
def test_bitvector(kwargs, expected):
    caps = mtts.MttsCapabilities(**kwargs)
    assert caps.bitvector == expected


def test_make_ttype_callback_cycling():
    cb = mtts.make_ttype_callback("xterm-256color")
    assert cb() == "TELIX"
    assert cb() == "XTERM-256COLOR"
    assert cb() == "MTTS 1807"
    assert cb() == "MTTS 1807"


def test_make_ttype_callback_uppercases_term():
    cb = mtts.make_ttype_callback("vt100")
    cb()
    assert cb() == "VT100"


def test_make_ttype_callback_ssl():
    cb = mtts.make_ttype_callback("xterm", ssl=True)
    cb()
    cb()
    assert cb() == "MTTS 3855"


def test_make_ttype_callback_many_calls():
    cb = mtts.make_ttype_callback("xterm")
    for _ in range(10):
        result = cb()
    assert result == "MTTS 1807"


def test_telix_client_send_ttype_with_factory():
    factory = mtts.make_ttype_callback("xterm-256color")
    client = mtts.TelixClient.__new__(mtts.TelixClient)
    client.ttype_factory = factory
    assert client.send_ttype() == "TELIX"
    assert client.send_ttype() == "XTERM-256COLOR"
    assert client.send_ttype() == "MTTS 1807"


def test_telix_client_send_ttype_without_factory():
    client = mtts.TelixClient.__new__(mtts.TelixClient)
    client.ttype_factory = None
    client._extra = {"term": "xterm"}
    assert client.send_ttype() == "xterm"


def test_telix_client_send_env_mnes_all_keys():
    client = mtts.TelixClient.__new__(mtts.TelixClient)
    client._extra = {
        "lang": "en_US.utf8",
        "term": "xterm",
        "rows": 25,
        "cols": 80,
        "tspeed": "38400,38400",
        "xdisploc": "",
        "charset": "utf8",
    }
    client._send_environ = {"TERM", "LANG"}
    client.mnes_env = {"CLIENT_NAME": "TELIX", "CLIENT_VERSION": "0.1.0"}
    env = client.send_env([])
    assert env["CLIENT_NAME"] == "TELIX"
    assert env["CLIENT_VERSION"] == "0.1.0"
    assert "TERM" in env


def test_telix_client_send_env_mnes_specific_keys():
    client = mtts.TelixClient.__new__(mtts.TelixClient)
    client._extra = {
        "lang": "en_US.utf8",
        "term": "xterm",
        "rows": 25,
        "cols": 80,
        "tspeed": "38400,38400",
        "xdisploc": "",
        "charset": "utf8",
    }
    client._send_environ = {"TERM"}
    client.mnes_env = {"CLIENT_NAME": "TELIX", "CLIENT_VERSION": "0.1.0"}
    env = client.send_env(["CLIENT_NAME", "TERM"])
    assert env["CLIENT_NAME"] == "TELIX"
    assert "CLIENT_VERSION" not in env


def test_telix_client_send_env_no_mnes():
    client = mtts.TelixClient.__new__(mtts.TelixClient)
    client._extra = {
        "lang": "en_US.utf8",
        "term": "xterm",
        "rows": 25,
        "cols": 80,
        "tspeed": "38400,38400",
        "xdisploc": "",
        "charset": "utf8",
    }
    client._send_environ = {"TERM"}
    client.mnes_env = None
    env = client.send_env([])
    assert "CLIENT_NAME" not in env


@pytest.mark.parametrize(
    "sw_name, expected", [(None, "Telix"), ("", "Telix"), ("Konsole", "Telix/Konsole"), ("XTerm", "Telix/XTerm")]
)
def test_client_name(sw_name, expected):
    assert mtts.client_name(sw_name) == expected


def test_install_mtts_sets_mnes_env():
    mtts.install_mtts("xterm-256color", ssl=False)
    assert mtts.TelixClient.mnes_env["CLIENT_NAME"] == "Telix"
    assert mtts.TelixClient.mnes_env["TERMINAL_TYPE"] == "XTERM-256COLOR"
    assert mtts.TelixClient.mnes_env["MTTS"] == "1807"
    assert mtts.TelixClient.mnes_env["CHARSET"] == "UTF-8"
    assert "CLIENT_VERSION" in mtts.TelixClient.mnes_env


def test_install_mtts_with_sw_name():
    mtts.install_mtts("xterm-256color", sw_name="Konsole")
    assert mtts.TelixClient.mnes_env["CLIENT_NAME"] == "Telix/Konsole"


def test_install_mtts_sets_charset():
    mtts.install_mtts("xterm", encoding="cp437")
    assert mtts.TelixClient.mnes_env["CHARSET"] == "CP437"
    assert mtts.TelixClient.mnes_env["MTTS"] == "1803"


def test_install_mtts_charset_default():
    mtts.install_mtts("xterm")
    assert mtts.TelixClient.mnes_env["CHARSET"] == "UTF-8"
    assert mtts.TelixClient.mnes_env["MTTS"] == "1807"


def test_make_ttype_callback_non_utf8():
    cb = mtts.make_ttype_callback("xterm", encoding="cp437")
    cb()
    cb()
    assert cb() == "MTTS 1803"


def test_gmcp_hello_sends_telix():
    """Core.Hello identifies Telix instead of telnetlib3."""
    import types
    import logging
    client = mtts.TelixClient.__new__(mtts.TelixClient)
    client._gmcp_hello_sent = False
    client._gmcp_modules = ["Char 1"]
    client.log = logging.getLogger("test")
    sent = []
    client.writer = types.SimpleNamespace(send_gmcp=lambda pkg, data: sent.append((pkg, data)))
    client._send_gmcp_hello()
    assert sent[0] == ("Core.Hello", {"client": "Telix", "version": mtts.version})
    assert sent[1] == ("Core.Supports.Set", ["Char 1"])


def test_gmcp_hello_not_sent_twice():
    """Core.Hello guard prevents duplicate sends."""
    import types
    import logging
    client = mtts.TelixClient.__new__(mtts.TelixClient)
    client._gmcp_hello_sent = True
    client._gmcp_modules = []
    client.log = logging.getLogger("test")
    sent = []
    client.writer = types.SimpleNamespace(send_gmcp=lambda pkg, data: sent.append((pkg, data)))
    client._send_gmcp_hello()
    assert sent == []
