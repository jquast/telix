"""Smoke test for telix package."""

import telix


def test_import():
    """Verify telix package can be imported."""
    assert telix.__version__ == "0.1.0"
