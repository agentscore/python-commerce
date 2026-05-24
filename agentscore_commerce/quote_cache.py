"""Short-TTL body-hash quote cache for the compute-first + exact-x402 pattern.

The probe leg writes (run-work → cache); the settle leg reads (cache hit →
settle exact at the cached price → return the cached body).

Default in-memory ``dict``; optional ``redis_url`` lazy-imports
``redis.asyncio`` for multi-instance deployments. ``redis`` is an optional
peer dep (install via the ``redis`` extra).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agentscore_commerce._redis import memoized_redis

logger = logging.getLogger("agentscore_commerce.quote_cache")

DEFAULT_TTL_MS = 5 * 60_000


@dataclass(frozen=True)
class CachedQuote:
    """An entry in the quote cache."""

    body: dict[str, Any]
    price_cents: float
    recipients: dict[str, str] = field(default_factory=dict)
    """Per-rail deposit addresses minted on the probe leg. The settle leg
    replays these instead of re-minting (avoids second Stripe PaymentIntent
    for the same logical purchase). Empty dict when no ``mint_recipients``
    hook is wired."""


def _canonicalize(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonicalize(value[k]) for k in sorted(value)}
    return value


@dataclass
class QuoteCache:
    """Async quote cache returned by :func:`create_quote_cache`."""

    body_hash_key: Any  # (prefix: str, body: dict) -> str
    read: Any  # (key: str) -> CachedQuote | None
    write: Any  # (key: str, body: dict, price_cents: float, *, recipients?: dict) -> None
    clear: Any  # () -> None


def create_quote_cache(
    *,
    ttl_ms: int = DEFAULT_TTL_MS,
    redis_url: str | None = None,
    key_prefix: str = "quote:",
) -> QuoteCache:
    """Build a fresh quote cache.

    Each call owns its own state (memory dict + Redis client). Set
    ``redis_url`` for a Redis-backed cache; otherwise the cache is
    in-process only and replicas diverge under load.
    """
    mem_state: dict[str, tuple[CachedQuote, float]] = {}
    _get_redis = memoized_redis(url=redis_url, label="quote-cache")

    def _body_hash_key(prefix: str, body: dict[str, Any]) -> str:
        canonical = json.dumps(_canonicalize(body), separators=(",", ":"))
        digest = hashlib.sha256(f"{prefix}::{canonical}".encode()).hexdigest()[:24]
        return f"{prefix}::{digest}"

    def _evict_expired() -> None:
        now = time.monotonic() * 1000
        for k, (_, expires_at) in list(mem_state.items()):
            if expires_at <= now:
                del mem_state[k]

    async def _read(key: str) -> CachedQuote | None:
        r = await _get_redis()
        if r is not None:
            with contextlib.suppress(Exception):
                raw = await r.get(f"{key_prefix}{key}")
                if raw is None:
                    return None
                data = json.loads(raw)
                return CachedQuote(
                    body=data["body"],
                    price_cents=data["price_cents"],
                    recipients=data.get("recipients", {}),
                )
        _evict_expired()
        entry = mem_state.get(key)
        return entry[0] if entry else None

    async def _write(
        key: str,
        body: dict[str, Any],
        price_cents: float,
        *,
        recipients: dict[str, str] | None = None,
    ) -> None:
        cached = CachedQuote(body=body, price_cents=price_cents, recipients=recipients or {})
        r = await _get_redis()
        if r is not None:
            with contextlib.suppress(Exception):
                payload = json.dumps(
                    {"body": cached.body, "price_cents": cached.price_cents, "recipients": cached.recipients}
                )
                await r.set(f"{key_prefix}{key}", payload, px=ttl_ms)
                return
        mem_state[key] = (cached, time.monotonic() * 1000 + ttl_ms)

    async def _clear() -> None:
        mem_state.clear()
        r = await _get_redis()
        if r is not None:
            with contextlib.suppress(Exception):
                await r.flushdb()

    return QuoteCache(
        body_hash_key=_body_hash_key,
        read=_read,
        write=_write,
        clear=_clear,
    )
