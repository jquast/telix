"""Tests for telix.ssh_client -- key file path resolution."""

import os

import pytest

from telix.ssh_client import resolve_key_file


@pytest.mark.parametrize(
    "key_file, expected",
    [
        ("id_ed25519", os.path.expanduser("~/.ssh/id_ed25519")),
        ("id_rsa", os.path.expanduser("~/.ssh/id_rsa")),
        ("~/.ssh/id_ed25519", os.path.expanduser("~/.ssh/id_ed25519")),
        ("/absolute/path/key", "/absolute/path/key"),
        ("subdir/key", "subdir/key"),
        ("", ""),
    ],
)
def test_resolve_key_file(key_file, expected):
    """resolve_key_file expands bare filenames to ~/.ssh/ and ~ paths."""
    assert resolve_key_file(key_file) == expected
