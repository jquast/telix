"""Tests for telix.session_context."""

from __future__ import annotations

from telix.session_context import TelixSessionContext


def test_ansi_keys_default_false():
    """New TelixSessionContext has ansi_keys == False."""
    ctx = TelixSessionContext()
    assert ctx.ansi_keys is False
