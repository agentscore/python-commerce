"""Flask rate-limit adapter.

Flask is sync; the underlying limiter is async. We run it on a thread-local event
loop so this adapter stays drop-in for vanilla WSGI Flask. For async-Flask
(Flask 3+) consumers we provide an async-compatible variant too.

Usage::

    from flask import Flask
    from agentscore_commerce.middleware.flask import rate_limit_flask

    app = Flask(__name__)
    rate_limit_flask(app, max_requests=60, window_seconds=60)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agentscore_commerce.middleware._core import (
    RATE_LIMIT_JSON_BODY,
    create_rate_limiter,
    default_key_from_forwarded_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask import Flask, Request


def rate_limit_flask(
    app: Flask,
    *,
    window_seconds: int = 60,
    max_requests: int = 60,
    key_resolver: Callable[[Request], str] | None = None,
    redis_url: str | None = None,
    key_prefix: str = "rl:",
) -> None:
    """Install a global ``before_request`` hook that enforces the limit."""
    from flask import after_this_request, g, jsonify, request

    limiter = create_rate_limiter(
        window_seconds=window_seconds,
        max_requests=max_requests,
        redis_url=redis_url,
        key_prefix=key_prefix,
    )
    resolver = key_resolver or (lambda r: default_key_from_forwarded_for(r.headers.get("X-Forwarded-For")))

    def _run_async(coro: Any) -> Any:
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
            if loop.is_running():  # async Flask path
                return asyncio.run_coroutine_threadsafe(coro, loop).result()
        except RuntimeError:
            # No current event loop on this thread (sync Flask path); fall through
            # to asyncio.run which constructs one for this call.
            pass
        return asyncio.run(coro)

    @app.before_request
    def _enforce() -> Any:
        decision = _run_async(limiter.check(resolver(request)))
        g.rate_limit_limit = decision.limit
        g.rate_limit_remaining = decision.remaining
        if not decision.allowed:
            resp = jsonify(RATE_LIMIT_JSON_BODY)
            resp.status_code = 429
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-RateLimit-Limit"] = str(decision.limit)
            resp.headers["X-RateLimit-Remaining"] = str(decision.remaining)
            return resp

        @after_this_request
        def _add_headers(resp: Any) -> Any:
            resp.headers["X-RateLimit-Limit"] = str(decision.limit)
            resp.headers["X-RateLimit-Remaining"] = str(decision.remaining)
            return resp

        return None
