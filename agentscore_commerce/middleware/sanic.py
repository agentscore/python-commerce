"""Sanic rate-limit adapter.

Usage::

    from sanic import Sanic
    from agentscore_commerce.middleware.sanic import rate_limit_sanic

    app = Sanic("my-app")
    rate_limit_sanic(app, max_requests=60, window_seconds=60)
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

    from sanic import Request, Sanic


def rate_limit_sanic(
    app: Sanic,
    *,
    window_seconds: int = 60,
    max_requests: int = 60,
    key_resolver: Callable[[Request], str] | None = None,
    redis_url: str | None = None,
    key_prefix: str = "rl:",
) -> None:
    """Wire ``request`` and ``response`` Sanic middleware that enforce the limit."""
    from sanic import response as _response

    limiter = create_rate_limiter(
        window_seconds=window_seconds,
        max_requests=max_requests,
        redis_url=redis_url,
        key_prefix=key_prefix,
    )
    resolver = key_resolver or (lambda r: default_key_from_forwarded_for(r.headers.get("X-Forwarded-For")))

    @app.middleware("request")
    async def _enforce(request: Request) -> Any:
        decision = await limiter.check(resolver(request))
        request.ctx.rate_limit_limit = decision.limit
        request.ctx.rate_limit_remaining = decision.remaining
        if not decision.allowed:
            return _response.json(
                RATE_LIMIT_JSON_BODY,
                status=429,
                headers={
                    "Cache-Control": "no-store",
                    "X-RateLimit-Limit": str(decision.limit),
                    "X-RateLimit-Remaining": str(decision.remaining),
                },
            )
        return None

    @app.middleware("response")
    async def _attach_headers(request: Request, response: Any) -> None:
        limit = getattr(request.ctx, "rate_limit_limit", None)
        remaining = getattr(request.ctx, "rate_limit_remaining", None)
        if limit is not None:
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
