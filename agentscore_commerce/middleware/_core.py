"""Framework-agnostic rate-limit core.

Mirrors ``@agent-score/commerce/middleware/_core``. Per-framework adapters
(`fastapi`, `flask`, `django`, `aiohttp`, `sanic`, `asgi`) wrap a shared
:class:`RateLimiter` so adapter selection is framework-mechanics, not policy.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("agentscore_commerce.middleware.rate_limit")


class _RedisLike(Protocol):
    """Subset of ``redis.asyncio.Redis`` we use; structurally typed so ``redis`` stays optional."""

    async def incr(self, key: str) -> int: ...
    async def expire(self, key: str, seconds: int) -> Any: ...


@dataclass(frozen=True)
class RateLimitDecision:
    """Result of a single ``RateLimiter.check`` call."""

    allowed: bool
    remaining: int
    limit: int


@dataclass
class RateLimiter:
    """Async rate-limiter handle returned by :func:`create_rate_limiter`."""

    check: Any  # async (key: str) -> RateLimitDecision


RATE_LIMIT_JSON_BODY: dict[str, Any] = {
    "error": {"code": "rate_limited", "message": "Too many requests"},
}


def default_key_from_forwarded_for(forwarded_for: str | None) -> str:
    """Default bucket key: first hop of ``x-forwarded-for``, else ``'unknown'``."""
    if not forwarded_for:
        return "unknown"
    first = forwarded_for.split(",", 1)[0].strip()
    return first or "unknown"


def create_rate_limiter(
    *,
    window_seconds: int = 60,
    max_requests: int = 60,
    redis_url: str | None = None,
    key_prefix: str = "rl:",
) -> RateLimiter:
    """Build a rate-limiter. Each call owns its own memory map + Redis connection.

    Lazy-imports ``redis.asyncio`` when ``redis_url`` is set; falls back to an
    in-process ``dict`` when the URL is omitted, the lazy import fails, or any
    Redis call raises.
    """
    redis_client: _RedisLike | None = None
    mem_state: dict[str, tuple[int, float]] = {}  # key -> (count, reset_at_monotonic)

    async def _get_redis() -> _RedisLike | None:
        nonlocal redis_client
        if not redis_url:
            return None
        if redis_client is not None:
            return redis_client
        from importlib import import_module

        try:
            redis_asyncio: Any = import_module("redis.asyncio")
        except ImportError:
            logger.error(
                "[rate-limit] redis_url set but `redis` is not installed. Run `pip install redis` or unset redis_url.",
            )
            return None
        redis_client = redis_asyncio.from_url(redis_url)
        return redis_client

    def _check_mem(key: str) -> RateLimitDecision:
        now = time.monotonic()
        entry = mem_state.get(key)
        if entry is None or entry[1] < now:
            mem_state[key] = (1, now + window_seconds)
            return RateLimitDecision(allowed=True, remaining=max_requests - 1, limit=max_requests)
        count, reset_at = entry
        count += 1
        mem_state[key] = (count, reset_at)
        remaining = max(0, max_requests - count)
        return RateLimitDecision(allowed=count <= max_requests, remaining=remaining, limit=max_requests)

    async def check(key: str) -> RateLimitDecision:
        r = await _get_redis()
        if r is None:
            return _check_mem(key)
        try:
            full_key = f"{key_prefix}{key}"
            count = await r.incr(full_key)
            if count == 1:
                await r.expire(full_key, window_seconds)
            remaining = max(0, max_requests - count)
            return RateLimitDecision(allowed=count <= max_requests, remaining=remaining, limit=max_requests)
        except Exception:
            return _check_mem(key)

    return RateLimiter(check=check)
