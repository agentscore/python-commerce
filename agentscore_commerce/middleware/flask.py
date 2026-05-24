"""Flask rate-limit adapter.

Flask is sync; the underlying limiter is async. We run every coroutine on a single
persistent background event loop (daemon thread) so a Redis client binds to one
stable loop and is never stranded on a closed per-call loop. Works drop-in for both
vanilla WSGI Flask and async-Flask/ASGI.

Usage::

    from flask import Flask
    from agentscore_commerce.middleware.flask import rate_limit_flask

    app = Flask(__name__)
    rate_limit_flask(app, max_requests=60, window_seconds=60)
"""

from __future__ import annotations

import asyncio
import threading
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

    # Flask's before_request hook is sync, but the limiter is async. Run every
    # coroutine on a single persistent background loop (daemon thread) rather than
    # a fresh per-call loop: a Redis client binds to the loop that created it, so a
    # throwaway loop would close that loop and silently degrade Redis-backed
    # limiting to the in-memory fallback after the first request. Submitting from
    # the request thread and blocking on .result() never deadlocks because the loop
    # runs on its own thread; works for both sync WSGI and async-Flask/ASGI.
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True, name="agentscore-ratelimit").start()

    def _run_async(coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

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
