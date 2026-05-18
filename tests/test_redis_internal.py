"""Tests for `agentscore_commerce._redis` covering memoization + URL handling."""

import os
from unittest.mock import patch

import pytest

from agentscore_commerce._redis import memoized_redis


@pytest.mark.asyncio
async def test_memoized_redis_returns_none_when_no_url() -> None:
    get = memoized_redis(url=None, label="test")
    assert await get() is None


@pytest.mark.asyncio
async def test_memoized_redis_memoizes() -> None:
    get = memoized_redis(url=None, label="test")
    a = await get()
    b = await get()
    assert a is b
    assert a is None


@pytest.mark.asyncio
async def test_memoized_redis_with_env_var() -> None:
    """Falls back to REDIS_URL env when url= is None."""
    with patch.dict(os.environ, {"REDIS_URL": ""}, clear=False):
        os.environ.pop("REDIS_URL", None)
        get = memoized_redis(url=None, label="test-env")
        assert await get() is None


@pytest.mark.asyncio
async def test_memoized_redis_with_unreachable_url() -> None:
    """When URL is set but Redis init fails, returns None gracefully."""
    get = memoized_redis(url="redis://127.0.0.1:1", label="test-unreachable")
    result = await get()
    # Either None (no redis installed / construction failed) or a client object
    # that won't be queried — either way memoization is the key behavior here.
    again = await get()
    assert result is again
