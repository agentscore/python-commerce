"""Cross-framework rate-limit middleware tests.

Mirrors `node-commerce/tests/middleware/rate_limit.test.ts`. Each adapter is exercised
against its native test harness (Starlette TestClient for FastAPI / ASGI, Flask test
client, Sanic test client, aiohttp test client, Django AsyncClient).
"""

from __future__ import annotations

import json

import pytest

from agentscore_commerce.middleware._core import create_rate_limiter


@pytest.mark.asyncio
async def test_core_allows_then_blocks() -> None:
    limiter = create_rate_limiter(max_requests=2, window_seconds=60)
    d1 = await limiter.check("k")
    d2 = await limiter.check("k")
    d3 = await limiter.check("k")
    assert d1.allowed is True
    assert d1.remaining == 1
    assert d1.limit == 2
    assert d2.allowed is True
    assert d3.allowed is False


@pytest.mark.asyncio
async def test_core_isolates_buckets_across_factory_calls() -> None:
    a = create_rate_limiter(max_requests=1)
    b = create_rate_limiter(max_requests=1)
    assert (await a.check("same-key")).allowed is True
    assert (await b.check("same-key")).allowed is True
    assert (await a.check("same-key")).allowed is False
    assert (await b.check("same-key")).allowed is False


def test_asgi_middleware_starlette() -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from agentscore_commerce.middleware.asgi import RateLimitMiddleware

    async def health(_request: object) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/health", health)])
    app.add_middleware(
        RateLimitMiddleware,
        max_requests=2,
        window_seconds=60,
        key_resolver=lambda _scope: "fixed",
    )
    client = TestClient(app)
    r1 = client.get("/health")
    r2 = client.get("/health")
    r3 = client.get("/health")
    assert r1.status_code == 200
    assert r1.headers["x-ratelimit-limit"] == "2"
    assert r1.headers["x-ratelimit-remaining"] == "1"
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert r3.headers["cache-control"] == "no-store"
    assert r3.json() == {"error": {"code": "rate_limited", "message": "Too many requests"}}


def test_fastapi_dependency() -> None:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from agentscore_commerce.middleware.fastapi import rate_limit_fastapi

    limiter = rate_limit_fastapi(max_requests=2, window_seconds=60, key_resolver=lambda _r: "fixed")

    app = FastAPI()

    @app.get("/health", dependencies=[Depends(limiter)])
    async def health() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200
    r3 = client.get("/health")
    assert r3.status_code == 429
    assert r3.headers["cache-control"] == "no-store"
    assert r3.headers["x-ratelimit-limit"] == "2"


def test_flask_install() -> None:
    from flask import Flask

    from agentscore_commerce.middleware.flask import rate_limit_flask

    app = Flask(__name__)
    rate_limit_flask(app, max_requests=2, window_seconds=60, key_resolver=lambda _r: "fixed")

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    client = app.test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200
    r3 = client.get("/health")
    assert r3.status_code == 429
    assert r3.headers["Cache-Control"] == "no-store"
    assert r3.headers["X-RateLimit-Limit"] == "2"
    assert json.loads(r3.data) == {"error": {"code": "rate_limited", "message": "Too many requests"}}


@pytest.mark.asyncio
async def test_aiohttp_middleware() -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from agentscore_commerce.middleware.aiohttp import rate_limit_aiohttp

    middleware = rate_limit_aiohttp(max_requests=2, window_seconds=60, key_resolver=lambda _r: "fixed")
    app = web.Application(middlewares=[middleware])

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)

    async with TestClient(TestServer(app)) as client:
        r1 = await client.get("/health")
        r2 = await client.get("/health")
        r3 = await client.get("/health")
        assert r1.status == 200
        assert r1.headers["X-RateLimit-Limit"] == "2"
        assert r2.status == 200
        assert r3.status == 429
        assert r3.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_django_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    import django
    from django.conf import settings as dj_settings
    from django.http import JsonResponse

    if not dj_settings.configured:
        dj_settings.configure(
            DEBUG=False,
            ROOT_URLCONF=__name__,
            ALLOWED_HOSTS=["*"],
            SECRET_KEY="rate-limit-test",
        )
        django.setup()

    # Force our middleware-specific override regardless of what an earlier test set.
    monkeypatch.setattr(
        dj_settings,
        "AGENTSCORE_RATE_LIMIT",
        {"max_requests": 2, "window_seconds": 60},
        raising=False,
    )

    from django.test import AsyncRequestFactory

    from agentscore_commerce.middleware.django import RateLimitMiddleware

    async def get_response(_request: object) -> JsonResponse:
        return JsonResponse({"ok": True})

    mw = RateLimitMiddleware(get_response)
    factory = AsyncRequestFactory()

    r1 = await mw(factory.get("/health", HTTP_X_FORWARDED_FOR="1.1.1.1"))
    r2 = await mw(factory.get("/health", HTTP_X_FORWARDED_FOR="1.1.1.1"))
    r3 = await mw(factory.get("/health", HTTP_X_FORWARDED_FOR="1.1.1.1"))
    assert r1.status_code == 200
    assert r1["X-RateLimit-Limit"] == "2"
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert r3["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_asgi_passthrough_non_http() -> None:
    from agentscore_commerce.middleware.asgi import RateLimitMiddleware

    seen: list[str] = []

    async def app(scope: dict[str, object], _receive: object, _send: object) -> None:
        seen.append(str(scope["type"]))

    mw = RateLimitMiddleware(app, max_requests=1)
    await mw({"type": "lifespan", "headers": []}, lambda: None, lambda _m: None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


def test_asgi_default_key_resolver_uses_client_tuple() -> None:
    from agentscore_commerce.middleware.asgi import _default_scope_key_resolver

    assert _default_scope_key_resolver({"headers": [], "client": ("203.0.113.1", 5555)}) == "203.0.113.1"
    assert _default_scope_key_resolver({"headers": []}) == "unknown"
    assert _default_scope_key_resolver({"headers": [(b"x-forwarded-for", b"10.0.0.1, 10.0.0.2")]}) == "10.0.0.1"


@pytest.mark.asyncio
async def test_core_redis_path_with_stub() -> None:
    """When Redis is configured and reachable, the limiter uses Redis-side counters."""
    import sys
    import types

    counts: dict[str, int] = {}

    class _StubRedis:
        async def incr(self, key: str) -> int:
            counts[key] = counts.get(key, 0) + 1
            return counts[key]

        async def expire(self, _key: str, _seconds: int) -> bool:
            return True

    def from_url(_url: str, **_kw: object) -> _StubRedis:
        return _StubRedis()

    module = types.ModuleType("redis.asyncio")
    module.from_url = from_url  # type: ignore[attr-defined]
    sys.modules["redis.asyncio"] = module

    try:
        from agentscore_commerce.middleware._core import create_rate_limiter

        limiter = create_rate_limiter(max_requests=2, redis_url="redis://stub", key_prefix="testrl:")
        d1 = await limiter.check("k")
        d2 = await limiter.check("k")
        d3 = await limiter.check("k")
        assert d1.allowed and d2.allowed
        assert not d3.allowed
        assert counts["testrl:k"] == 3
    finally:
        sys.modules.pop("redis.asyncio", None)


@pytest.mark.asyncio
async def test_sanic_install() -> None:
    from sanic import Sanic, response

    from agentscore_commerce.middleware.sanic import rate_limit_sanic

    app = Sanic.get_app("rate-limit-test", force_create=True)
    rate_limit_sanic(app, max_requests=2, window_seconds=60, key_resolver=lambda _r: "fixed")

    @app.get("/health")
    async def health(_request: object) -> response.HTTPResponse:
        return response.json({"ok": True})

    _request, r1 = await app.asgi_client.get("/health")
    _request, r2 = await app.asgi_client.get("/health")
    _request, r3 = await app.asgi_client.get("/health")
    assert r1.status == 200
    assert r1.headers["x-ratelimit-limit"] == "2"
    assert r2.status == 200
    assert r3.status == 429
    assert r3.headers["cache-control"] == "no-store"
