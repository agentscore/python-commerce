"""Django rate-limit middleware.

Async middleware class compatible with Django 4+. Install in ``MIDDLEWARE`` or
construct via the factory so multiple instances don't share buckets::

    # settings.py
    MIDDLEWARE = [
        "agentscore_commerce.middleware.django.RateLimitMiddleware",
        # ...
    ]

    # optional: override defaults via env
    AGENTSCORE_RATE_LIMIT = {"max_requests": 60, "window_seconds": 60}
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

    from django.http import HttpRequest, HttpResponse


class RateLimitMiddleware:
    """Async Django middleware enforcing the shared rate-limit core."""

    async_capable = True
    sync_capable = False

    def __init__(self, get_response: Callable[[HttpRequest], Any]) -> None:
        self.get_response = get_response
        try:
            from django.conf import settings  # local import: django is an optional peer dep

            cfg: dict[str, Any] = getattr(settings, "AGENTSCORE_RATE_LIMIT", {}) or {}
        except Exception:
            cfg = {}
        self._limiter = create_rate_limiter(
            window_seconds=cfg.get("window_seconds", 60),
            max_requests=cfg.get("max_requests", 60),
            redis_url=cfg.get("redis_url"),
            key_prefix=cfg.get("key_prefix", "rl:"),
        )

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        from django.http import JsonResponse

        key = default_key_from_forwarded_for(request.META.get("HTTP_X_FORWARDED_FOR"))
        decision = await self._limiter.check(key)

        if not decision.allowed:
            resp = JsonResponse(RATE_LIMIT_JSON_BODY, status=429)
            resp["Cache-Control"] = "no-store"
            resp["X-RateLimit-Limit"] = str(decision.limit)
            resp["X-RateLimit-Remaining"] = str(decision.remaining)
            return resp

        response = await self.get_response(request)
        response["X-RateLimit-Limit"] = str(decision.limit)
        response["X-RateLimit-Remaining"] = str(decision.remaining)
        return response
