"""FastAPI/Starlette rate-limit adapter.

Two surfaces:
  * :class:`~agentscore_commerce.middleware.asgi.RateLimitMiddleware` for global mount via
    ``app.add_middleware(RateLimitMiddleware, ...)``.
  * :func:`rate_limit_fastapi` returns an async ``Depends``-able callable for
    per-route gating.

The middleware approach is the canonical mount. The dependency approach is here
for routes that need finer control or a custom key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.requests import Request  # noqa: TC002 - runtime import required for FastAPI DI

from agentscore_commerce.middleware._core import (
    RATE_LIMIT_JSON_BODY,
    create_rate_limiter,
    default_key_from_forwarded_for,
)
from agentscore_commerce.middleware.asgi import RateLimitMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable


def rate_limit_fastapi(
    *,
    window_seconds: int = 60,
    max_requests: int = 60,
    key_resolver: Callable[[Request], str] | None = None,
    redis_url: str | None = None,
    key_prefix: str = "rl:",
) -> Callable[[Request], Any]:
    """Return a FastAPI dependency that enforces a rate limit.

    Usage::

        from fastapi import Depends, FastAPI
        from agentscore_commerce.middleware.fastapi import rate_limit_fastapi

        app = FastAPI()
        limiter = rate_limit_fastapi(max_requests=60, window_seconds=60)

        @app.post("/purchase", dependencies=[Depends(limiter)])
        async def purchase():
            ...
    """
    from fastapi import HTTPException

    limiter = create_rate_limiter(
        window_seconds=window_seconds,
        max_requests=max_requests,
        redis_url=redis_url,
        key_prefix=key_prefix,
    )
    resolver = key_resolver or (lambda r: default_key_from_forwarded_for(r.headers.get("x-forwarded-for")))

    async def dependency(request: Request) -> None:
        decision = await limiter.check(resolver(request))
        request.state.rate_limit_limit = decision.limit  # type: ignore[attr-defined]
        request.state.rate_limit_remaining = decision.remaining  # type: ignore[attr-defined]
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail=RATE_LIMIT_JSON_BODY["error"],
                headers={
                    "X-RateLimit-Limit": str(decision.limit),
                    "X-RateLimit-Remaining": str(decision.remaining),
                    "Cache-Control": "no-store",
                },
            )

    return dependency


__all__ = ["RateLimitMiddleware", "rate_limit_fastapi"]
