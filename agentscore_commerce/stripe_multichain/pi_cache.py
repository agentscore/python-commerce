"""Stripe PaymentIntent + deposit-address cache.

Stripe-multichain merchants need three lookups during a request lifecycle:

1. **Is this on-chain ``pay_to`` address one we minted?** — when an MPP credential
   arrives with a ``recipient``, verify it matches a recently-minted Stripe deposit
   address. Prevents agents from sending payment to an attacker-controlled address
   and replaying the credential against the merchant's endpoint.

2. **Which PaymentIntent owns this deposit address?** — when settling, the
   ``simulate_crypto_deposit`` test_helpers call needs the PaymentIntent id for the
   deposit address that was paid to.

3. **Which sibling deposit addresses belong to the same PaymentIntent?** — when
   enriching a 402 with x402 entries, the merchant needs the Base + Solana addresses
   Stripe minted alongside the original Tempo address (one PI carries up to three).

All three are TTL-bounded (default 300s — long enough for an agent to retry, short
enough to bound memory). Backed by Redis when ``redis_url`` is set, falls back to
in-process dict otherwise. Single-instance servers can use the in-memory cache;
multi-instance deployments need a shared cache (Redis) so a deposit lands on
whichever instance settles it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("agentscore_commerce.stripe_multichain.pi_cache")

T = TypeVar("T")


class _RedisLike(Protocol):
    """Subset of redis.asyncio.Redis we use — typed structurally so ``redis`` stays an optional peer dep."""

    async def set(self, key: str, value: str, *, ex: int) -> Any: ...
    async def get(self, key: str) -> str | None: ...


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float


@dataclass
class PiCacheOptions:
    """Optional configuration for :func:`create_pi_cache`."""

    #: Redis connection URL (e.g. ``rediss://…cache.amazonaws.com:6379``). When omitted,
    #: the cache falls back to in-process dicts with the same API.
    redis_url: str | None = None
    #: TTL for cached entries in seconds. Default 300.
    ttl_seconds: int = 300
    #: Prefix for Redis keys. Default ``'payto:'``.
    key_prefix: str = "payto:"


@dataclass
class PiCache:
    """Stripe PI + deposit-address cache produced by :func:`create_pi_cache`."""

    cache_address: Callable[[str], asyncio.Future[None] | Any]
    has_address: Callable[[str], asyncio.Future[bool] | Any]
    cache_payment_intent: Callable[[str, str], None]
    get_payment_intent_id: Callable[[str], str | None]
    cache_network_addresses: Callable[[str, dict[str, str]], None]
    get_network_deposit_address: Callable[[str, str], str | None]
    stop: Callable[[], None]


def create_pi_cache(opts: PiCacheOptions | None = None) -> PiCache:
    """Construct a Stripe PI + deposit-address cache instance.

    Returns a ``PiCache`` with async ``cache_address`` / ``has_address`` (Redis-backed
    when ``redis_url`` is set) and sync helpers for PI-id and network-address lookup.
    A background task evicts expired in-memory entries every 60 seconds; call
    ``stop()`` from server shutdown handlers to cancel it.
    """
    options = opts or PiCacheOptions()
    ttl = options.ttl_seconds
    key_prefix = options.key_prefix

    redis_client: _RedisLike | None = None
    addr_mem_cache: dict[str, float] = {}
    pi_cache: dict[str, _Entry[str]] = {}
    network_address_cache: dict[str, _Entry[dict[str, str]]] = {}

    async def _get_redis() -> _RedisLike | None:
        nonlocal redis_client
        if not options.redis_url:
            return None
        if redis_client is not None:
            return redis_client
        # Dynamic import keeps `redis` as an optional peer dep — merchants without
        # Redis don't pay the install cost.
        from importlib import import_module

        try:
            redis_asyncio: Any = import_module("redis.asyncio")
        except ImportError:
            logger.error(
                "[pi-cache] redis_url set but `redis` is not installed. Run `pip install redis` or unset redis_url."
            )
            return None
        redis_client = redis_asyncio.from_url(options.redis_url)
        return redis_client

    async def cache_address(address: str) -> None:
        r = await _get_redis()
        if r is not None:
            with contextlib.suppress(Exception):
                await r.set(f"{key_prefix}{address}", "1", ex=ttl)
        addr_mem_cache[address] = time.time() + ttl

    async def has_address(address: str) -> bool:
        r = await _get_redis()
        if r is not None:
            try:
                val = await r.get(f"{key_prefix}{address}")
                if val:
                    return True
            except Exception as err:
                logger.debug("[pi-cache] redis get failed, falling back to in-memory: %s", err)
        expiry = addr_mem_cache.get(address)
        return expiry is not None and expiry > time.time()

    def cache_payment_intent(deposit_address: str, payment_intent_id: str) -> None:
        pi_cache[deposit_address] = _Entry(value=payment_intent_id, expires_at=time.time() + ttl)

    def get_payment_intent_id(deposit_address: str) -> str | None:
        entry = pi_cache.get(deposit_address)
        if entry is None:
            return None
        if entry.expires_at < time.time():
            pi_cache.pop(deposit_address, None)
            return None
        return entry.value

    def cache_network_addresses(payment_intent_id: str, addresses: dict[str, str]) -> None:
        network_address_cache[payment_intent_id] = _Entry(value=dict(addresses), expires_at=time.time() + ttl)

    def get_network_deposit_address(payment_intent_id: str, network: str) -> str | None:
        entry = network_address_cache.get(payment_intent_id)
        if entry is None:
            return None
        if entry.expires_at < time.time():
            network_address_cache.pop(payment_intent_id, None)
            return None
        return entry.value.get(network)

    evict_task: asyncio.Task[None] | None = None

    async def _evict_loop() -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for k, v in list(pi_cache.items()):
                if v.expires_at < now:
                    pi_cache.pop(k, None)
            for k, v in list(network_address_cache.items()):
                if v.expires_at < now:
                    network_address_cache.pop(k, None)
            for k, expires in list(addr_mem_cache.items()):
                if expires < now:
                    addr_mem_cache.pop(k, None)

    try:
        loop = asyncio.get_running_loop()
        evict_task = loop.create_task(_evict_loop())
    except RuntimeError:
        # No running loop (e.g. import-time use in a sync script). Caller can start the
        # eviction loop themselves by entering an asyncio.run / event loop later.
        pass

    def stop() -> None:
        if evict_task is not None and not evict_task.done():
            evict_task.cancel()

    return PiCache(
        cache_address=cache_address,
        has_address=has_address,
        cache_payment_intent=cache_payment_intent,
        get_payment_intent_id=get_payment_intent_id,
        cache_network_addresses=cache_network_addresses,
        get_network_deposit_address=get_network_deposit_address,
        stop=stop,
    )
