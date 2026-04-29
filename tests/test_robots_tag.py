"""Tests for the lifted robots-tag helpers + ASGI middleware."""

import pytest

from agentscore_commerce.discovery import (
    DEFAULT_DISCOVERY_PATHS,
    DEFAULT_ROBOTS_TAG,
    NoindexNonDiscoveryMiddleware,
    is_discovery_path,
)


def test_default_discovery_paths_includes_canonical_surfaces() -> None:
    for p in (
        "/openapi.json",
        "/llms.txt",
        "/.well-known/mpp.json",
        "/.well-known/agent-card.json",
        "/.well-known/ucp",
        "/favicon.png",
        "/favicon.ico",
    ):
        assert p in DEFAULT_DISCOVERY_PATHS


def test_is_discovery_path_matches_defaults() -> None:
    assert is_discovery_path("/openapi.json")
    assert is_discovery_path("/llms.txt")
    assert is_discovery_path("/.well-known/mpp.json")


def test_is_discovery_path_returns_false_for_arbitrary_paths() -> None:
    assert not is_discovery_path("/purchase")
    assert not is_discovery_path("/orders/abc")


def test_custom_paths_additive_when_replace_false() -> None:
    assert is_discovery_path("/sitemap.xml", custom_paths={"/sitemap.xml"})
    assert is_discovery_path("/openapi.json", custom_paths={"/sitemap.xml"})


def test_replace_true_skips_defaults() -> None:
    assert not is_discovery_path("/openapi.json", custom_paths={"/sitemap.xml"}, replace=True)
    assert is_discovery_path("/sitemap.xml", custom_paths={"/sitemap.xml"}, replace=True)


# ────────────────────────────────────────────────────────────────────────────
# ASGI middleware
# ────────────────────────────────────────────────────────────────────────────


class _FakeApp:
    """Minimal ASGI inner app — captures the headers that pass through send."""

    def __init__(self) -> None:
        self.captured_headers: list[tuple[bytes, bytes]] = []

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    async def collect(self, message: dict) -> None:
        if message.get("type") == "http.response.start":
            self.captured_headers = message.get("headers", [])


def _hdr(headers: list[tuple[bytes, bytes]], name: str) -> bytes | None:
    for k, v in headers:
        if k.lower() == name.encode("latin-1").lower():
            return v
    return None


@pytest.mark.asyncio
async def test_middleware_sets_robots_tag_on_non_discovery_path() -> None:
    inner = _FakeApp()
    mw = NoindexNonDiscoveryMiddleware(inner)
    captured: list[tuple[bytes, bytes]] = []

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: dict) -> None:
        if message.get("type") == "http.response.start":
            captured[:] = message.get("headers", [])

    await mw({"type": "http", "path": "/purchase"}, receive, send)
    assert _hdr(captured, "x-robots-tag") == DEFAULT_ROBOTS_TAG.encode("latin-1")


@pytest.mark.asyncio
async def test_middleware_does_not_set_robots_tag_on_default_discovery_path() -> None:
    inner = _FakeApp()
    mw = NoindexNonDiscoveryMiddleware(inner)
    captured: list[tuple[bytes, bytes]] = []

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: dict) -> None:
        if message.get("type") == "http.response.start":
            captured[:] = message.get("headers", [])

    await mw({"type": "http", "path": "/openapi.json"}, receive, send)
    assert _hdr(captured, "x-robots-tag") is None


@pytest.mark.asyncio
async def test_middleware_treats_custom_paths_as_discovery() -> None:
    inner = _FakeApp()
    mw = NoindexNonDiscoveryMiddleware(inner, custom_paths={"/sitemap.xml"})
    captured: list[tuple[bytes, bytes]] = []

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: dict) -> None:
        if message.get("type") == "http.response.start":
            captured[:] = message.get("headers", [])

    await mw({"type": "http", "path": "/sitemap.xml"}, receive, send)
    assert _hdr(captured, "x-robots-tag") is None


@pytest.mark.asyncio
async def test_middleware_passthrough_for_non_http_scope() -> None:
    inner = _FakeApp()
    mw = NoindexNonDiscoveryMiddleware(inner)
    captured: list[tuple[bytes, bytes]] = []

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(message: dict) -> None:
        if message.get("type") == "http.response.start":
            captured[:] = message.get("headers", [])

    # Non-http scope should pass through to inner app without header injection.
    await mw({"type": "websocket", "path": "/socket"}, receive, send)
    # Inner app emits http.response.start with content-type, no x-robots-tag injected.
    assert _hdr(captured, "x-robots-tag") is None
