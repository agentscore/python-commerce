"""Generic ASGI rate-limit middleware.

Works with any starlette-compatible app (FastAPI, Starlette, Sanic-on-asgi, etc.).
Mount with ``app.add_middleware(RateLimitMiddleware, max_requests=60, window_seconds=60)``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agentscore_commerce.middleware._core import (
    RATE_LIMIT_JSON_BODY,
    create_rate_limiter,
    default_key_from_forwarded_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

    from starlette.types import ASGIApp, Receive, Scope, Send


class RateLimitMiddleware:
    """ASGI rate-limit middleware (60 req / 60 s / IP by default).

    Constructor args (all keyword):
      * ``window_seconds`` — bucket size in seconds (default 60).
      * ``max_requests`` — max requests per bucket (default 60).
      * ``key_resolver`` — ``(scope) -> str`` override. Default = first hop of ``x-forwarded-for``.
      * ``redis_url`` — when set, lazy-imports ``redis.asyncio``; otherwise in-memory.
      * ``key_prefix`` — Redis key prefix (default ``'rl:'``).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        window_seconds: int = 60,
        max_requests: int = 60,
        key_resolver: Callable[[MutableMapping[str, Any]], str] | None = None,
        redis_url: str | None = None,
        key_prefix: str = "rl:",
    ) -> None:
        self.app = app
        self._limiter = create_rate_limiter(
            window_seconds=window_seconds,
            max_requests=max_requests,
            redis_url=redis_url,
            key_prefix=key_prefix,
        )
        self._key_resolver = key_resolver or _default_scope_key_resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        decision = await self._limiter.check(self._key_resolver(scope))

        if decision.allowed:

            async def send_with_headers(message: MutableMapping[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"x-ratelimit-limit", str(decision.limit).encode()))
                    headers.append((b"x-ratelimit-remaining", str(decision.remaining).encode()))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_with_headers)
            return

        body = json.dumps(RATE_LIMIT_JSON_BODY).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"x-ratelimit-limit", str(decision.limit).encode()),
                    (b"x-ratelimit-remaining", str(decision.remaining).encode()),
                ],
            },
        )
        await send({"type": "http.response.body", "body": body})


def _default_scope_key_resolver(scope: MutableMapping[str, Any]) -> str:
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            return default_key_from_forwarded_for(value.decode(errors="replace"))
    client = scope.get("client")
    if client and isinstance(client, (list, tuple)) and client:
        return str(client[0])
    return "unknown"
