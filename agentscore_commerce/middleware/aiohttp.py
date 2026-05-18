"""aiohttp rate-limit middleware.

Usage::

    from aiohttp import web
    from agentscore_commerce.middleware.aiohttp import rate_limit_aiohttp

    app = web.Application(middlewares=[rate_limit_aiohttp(max_requests=60, window_seconds=60)])
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentscore_commerce.middleware._core import (
    RATE_LIMIT_JSON_BODY,
    create_rate_limiter,
    default_key_from_forwarded_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiohttp import web


def rate_limit_aiohttp(
    *,
    window_seconds: int = 60,
    max_requests: int = 60,
    key_resolver: Callable[[web.Request], str] | None = None,
    redis_url: str | None = None,
    key_prefix: str = "rl:",
) -> Any:
    """Return an aiohttp middleware enforcing the shared rate-limit core."""
    from aiohttp import web as _web

    limiter = create_rate_limiter(
        window_seconds=window_seconds,
        max_requests=max_requests,
        redis_url=redis_url,
        key_prefix=key_prefix,
    )
    resolver = key_resolver or (lambda r: default_key_from_forwarded_for(r.headers.get("X-Forwarded-For")))

    @_web.middleware
    async def middleware(request: _web.Request, handler: Callable[[_web.Request], Any]) -> _web.StreamResponse:
        decision = await limiter.check(resolver(request))
        if not decision.allowed:
            return _web.json_response(
                RATE_LIMIT_JSON_BODY,
                status=429,
                headers={
                    "Cache-Control": "no-store",
                    "X-RateLimit-Limit": str(decision.limit),
                    "X-RateLimit-Remaining": str(decision.remaining),
                },
            )
        response = await handler(request)
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response

    return middleware
