"""Redis-backed paths in ``_redis`` and ``quote_cache``.

Monkeypatches ``importlib.import_module`` so the lazy ``redis.asyncio`` import
inside ``_try_create_redis`` resolves to an in-memory fake. Covers the three
error branches (ImportError, generic Exception) and the happy path that drives
``quote_cache.read`` / ``write`` / ``clear``.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import agentscore_commerce._redis as redis_mod
from agentscore_commerce.quote_cache import create_quote_cache


class _FakeAsyncRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, **_kwargs: Any) -> str:
        self._store[key] = value
        return "OK"

    async def flushdb(self) -> str:
        self._store.clear()
        return "OK"


def _install_fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeAsyncRedis:
    """Patch importlib.import_module to return a stub redis.asyncio.from_url."""
    fake_client = _FakeAsyncRedis()
    fake_module = SimpleNamespace(from_url=lambda *_a, **_k: fake_client)
    real_import = sys.modules["importlib"].import_module

    def _patched(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "redis.asyncio":
            return fake_module
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(redis_mod, "import_module", _patched, raising=False)
    # _try_create_redis uses `from importlib import import_module` inline; patch
    # the importlib namespace too so the local binding resolves to ours.
    import importlib

    monkeypatch.setattr(importlib, "import_module", _patched)
    return fake_client


@pytest.mark.asyncio
async def test_try_create_redis_returns_none_when_redis_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """ImportError branch (lines 67-72)."""
    import importlib

    real_import = importlib.import_module

    def _raise_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "redis.asyncio":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _raise_import)
    result = await redis_mod._try_create_redis(url="redis://localhost:6379", label="t")
    assert result is None


@pytest.mark.asyncio
async def test_try_create_redis_returns_none_on_generic_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic Exception branch (lines 73-75) — e.g. malformed URL."""
    import importlib

    real_import = importlib.import_module

    def _patched(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "redis.asyncio":

            class _BadModule:
                @staticmethod
                def from_url(*_a: Any, **_k: Any) -> Any:
                    raise RuntimeError("connection refused")

            return _BadModule
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _patched)
    result = await redis_mod._try_create_redis(url="redis://localhost:6379", label="t")
    assert result is None


@pytest.mark.asyncio
async def test_quote_cache_redis_write_then_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy-path covers quote_cache write/read via the Redis branch (lines 89-94, 113-118)."""
    _install_fake_redis(monkeypatch)
    cache = create_quote_cache(redis_url="redis://fake", ttl_ms=60_000)
    key = cache.body_hash_key("search", {"q": "hello"})
    await cache.write(key, {"matches": ["a"]}, 2, recipients={"tempo": "0xabc"})
    got = await cache.read(key)
    assert got is not None
    assert got.body == {"matches": ["a"]}
    assert got.price_cents == 2
    assert got.recipients == {"tempo": "0xabc"}


@pytest.mark.asyncio
async def test_quote_cache_redis_read_returns_none_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Redis-read path returns None on missing key (line 92)."""
    _install_fake_redis(monkeypatch)
    cache = create_quote_cache(redis_url="redis://fake", ttl_ms=60_000)
    got = await cache.read("missing")
    assert got is None


@pytest.mark.asyncio
async def test_quote_cache_redis_clear_calls_flushdb(monkeypatch: pytest.MonkeyPatch) -> None:
    """clear() exercises the Redis flushdb path (lines 125-126)."""
    fake = _install_fake_redis(monkeypatch)
    cache = create_quote_cache(redis_url="redis://fake", ttl_ms=60_000)
    key = cache.body_hash_key("search", {"q": "x"})
    await cache.write(key, {}, 1)
    # ensure the entry is in the fake store
    assert any(json.loads(v).get("price_cents") == 1 for v in fake._store.values())
    await cache.clear()
    assert fake._store == {}


@pytest.mark.asyncio
async def test_quote_cache_in_memory_evicts_expired_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eviction loop in the in-memory branch (line 84): expired entry dropped."""
    import agentscore_commerce.quote_cache as qc_mod

    cache = create_quote_cache(ttl_ms=10)
    key = cache.body_hash_key("search", {"q": "evict"})
    await cache.write(key, {"v": 1}, 1)
    # Fast-forward monotonic time so the entry is past its expiry.
    real_monotonic = qc_mod.time.monotonic
    monkeypatch.setattr(qc_mod.time, "monotonic", lambda: real_monotonic() + 10)
    got = await cache.read(key)
    assert got is None
