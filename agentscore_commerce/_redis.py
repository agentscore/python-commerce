"""Shared lazy ``redis.asyncio`` factory.

Replaces the hand-rolled lazy-init pattern in ``quote_cache``,
``stripe_multichain.pi_cache``, and ``middleware._core`` so they don't drift
on logging posture, TLS handling, or connect-error semantics.

``redis`` is an optional peer dep — callers pass ``redis_url`` (or rely on
``REDIS_URL`` env); when unset or the lazy import fails, this returns ``None``
and the caller falls back to its in-process dict.

Mirrors node-commerce ``src/_redis.ts``. Not part of the public API.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class MinimalRedis(Protocol):
    """Minimal Redis surface.

    Each caller intersects with its own usage (get/set/incr/expire/flushdb).
    Returning ``Any`` on commands keeps the shape narrow; cast at the call site.
    """


T = TypeVar("T", bound=MinimalRedis)


async def _try_create_redis(
    *,
    url: str | None,
    label: str,
    socket_connect_timeout: float = 3.0,
    retry_on_error_max_attempts: int = 1,
) -> Any | None:
    """Lazy-import ``redis.asyncio`` and construct a client.

    Returns ``None`` when:

    - no URL is configured (caller falls back to in-memory)
    - ``redis`` isn't installed (optional peer; caller falls back to in-memory)
    - the import / construction raises for any other reason

    ``rediss://`` URLs auto-enable TLS via ``redis.asyncio.from_url``.
    Matches the node sibling's connect-timeout and retry-cap (3s, 1 retry).
    """
    resolved = url if url is not None else os.environ.get("REDIS_URL")
    if not resolved:
        return None
    try:
        from importlib import import_module

        redis_asyncio: Any = import_module("redis.asyncio")
        return redis_asyncio.from_url(
            resolved,
            socket_connect_timeout=socket_connect_timeout,
            retry_on_error=[ConnectionError, TimeoutError],
        )
    except ImportError:
        log.error(
            "[%s] redis_url set but `redis` is not installed. Run `pip install redis` or unset redis_url.",
            label,
        )
        return None
    except Exception as exc:
        log.warning("[%s] redis init failed (%s); falling back to in-memory", label, exc)
        return None


def memoized_redis(*, url: str | None, label: str) -> Callable[[], Awaitable[Any | None]]:
    """Build a memoized async getter.

    First call constructs the client; later calls return the same client
    (or the same ``None``).

    Mirrors node's ``memoizedRedis`` closure pattern. Pairs with the per-caller
    ``redis_url`` opt — when ``url`` is ``None`` AND ``REDIS_URL`` is unset, the
    getter resolves to ``None`` once and remains so for the lifetime of the
    caller.
    """
    client: Any | None = None
    attempted = False

    async def _get() -> Any | None:
        nonlocal client, attempted
        if attempted:
            return client
        attempted = True
        client = await _try_create_redis(url=url, label=label)
        return client

    return _get
