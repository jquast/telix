"""Pytest configuration and fixtures."""

# std imports
import asyncio

# 3rd party
import pytest


@pytest.fixture(scope="module", params=["127.0.0.1"])
def bind_host(request):
    """Localhost bind address."""
    return request.param


@pytest.fixture
def fast_sleep(monkeypatch):
    """Replace ``asyncio.sleep`` with a zero-delay yield to the event loop."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _: real_sleep(0))
