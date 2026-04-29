"""``X-Robots-Tag: noindex`` for non-discovery paths on agent-only APIs.

Public-by-design endpoints (OpenAPI, llms.txt, MPP/A2A/UCP well-known files)
should NOT carry ``X-Robots-Tag: noindex`` since the whole point is for agents
and discovery crawlers to find them. Everything else on an agent-only API
should noindex by default — there's no human-shaped HTML to surface, and
accidental indexing leaks transactional endpoints into noisy SERPs.

This module ships a pure predicate (``is_discovery_path``) that vendors compose
into their framework's middleware idiom, plus an ASGI middleware
(``NoindexNonDiscoveryMiddleware``) that works directly with FastAPI / Starlette
/ aiohttp / Sanic / etc.
"""

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

# The canonical agent-discovery surfaces. Mirrored from
# ``node-commerce/src/discovery/robots_tag.ts`` — keep in sync.
DEFAULT_DISCOVERY_PATHS: frozenset[str] = frozenset(
    {
        "/openapi.json",
        "/llms.txt",
        "/.well-known/mpp.json",
        "/.well-known/agent-card.json",
        "/.well-known/ucp",
        "/favicon.png",
        "/favicon.ico",
    }
)

DEFAULT_ROBOTS_TAG = "noindex, nofollow, noarchive, nosnippet"


def is_discovery_path(
    path: str,
    *,
    custom_paths: Iterable[str] | None = None,
    replace: bool = False,
) -> bool:
    """Return True when ``path`` is a known discovery surface.

    Args:
        path: The HTTP request path (no query string).
        custom_paths: Additional paths the merchant treats as discovery (e.g.
            ``/sitemap.xml``). Merged with ``DEFAULT_DISCOVERY_PATHS`` unless
            ``replace`` is set.
        replace: When True, ignore ``DEFAULT_DISCOVERY_PATHS`` and treat only
            ``custom_paths`` as discovery. Use when the merchant deliberately
            chooses a different set (e.g. omits ``/openapi.json`` from a closed API).
    """
    if replace:
        return path in set(custom_paths or [])
    if path in DEFAULT_DISCOVERY_PATHS:
        return True
    if custom_paths is not None:
        for p in custom_paths:
            if p == path:
                return True
    return False


class NoindexNonDiscoveryMiddleware:
    """ASGI middleware that sets ``X-Robots-Tag`` on non-discovery paths.

    Mount near the top of the ASGI middleware stack:

    .. code-block:: python

        from agentscore_commerce.discovery import NoindexNonDiscoveryMiddleware
        app.add_middleware(NoindexNonDiscoveryMiddleware)
        app.add_middleware(NoindexNonDiscoveryMiddleware, custom_paths={"/sitemap.xml"})

    Pure helpers (``is_discovery_path``, ``DEFAULT_DISCOVERY_PATHS``) are exported
    for non-ASGI frameworks (Flask, Django sync) — wire them into your own
    middleware idiom.
    """

    def __init__(
        self,
        app: Any,
        custom_paths: Iterable[str] | None = None,
        replace_paths: bool = False,
        robots_tag: str = DEFAULT_ROBOTS_TAG,
    ) -> None:
        self.app = app
        self._custom = frozenset(custom_paths or [])
        self._replace = replace_paths
        self._tag = robots_tag.encode("latin-1")

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_discovery = (
            (path in self._custom) if self._replace else (path in DEFAULT_DISCOVERY_PATHS or path in self._custom)
        )

        if is_discovery:
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-robots-tag", self._tag))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)


def install_flask_noindex(
    app: Any,
    custom_paths: Iterable[str] | None = None,
    replace_paths: bool = False,
    robots_tag: str = DEFAULT_ROBOTS_TAG,
) -> None:
    """Wire ``X-Robots-Tag`` into a Flask app via ``after_request``.

    Flask is WSGI, so the ASGI middleware doesn't fit. Call this once during
    app construction:

    .. code-block:: python

        from flask import Flask
        from agentscore_commerce.discovery import install_flask_noindex
        app = Flask(__name__)
        install_flask_noindex(app)
    """
    custom = frozenset(custom_paths or [])

    @app.after_request  # type: ignore[misc]
    def _add_robots_tag(response: Any) -> Any:
        # ``request.path`` is available on Flask via the import; we read it
        # lazily to keep the import optional.
        from flask import request  # type: ignore[import-not-found]

        path = request.path
        is_discovery = (path in custom) if replace_paths else (path in DEFAULT_DISCOVERY_PATHS or path in custom)
        if not is_discovery:
            response.headers["X-Robots-Tag"] = robots_tag
        return response


class DjangoNoindexMiddleware:
    """Django middleware that emits ``X-Robots-Tag`` on non-discovery paths.

    Wire into ``settings.MIDDLEWARE``:

    .. code-block:: python

        MIDDLEWARE = [
            ...,
            'agentscore_commerce.discovery.DjangoNoindexMiddleware',
        ]

    Configure via ``settings.AGENTSCORE_NOINDEX = {"custom_paths": [...],
    "replace_paths": False, "robots_tag": "..."}`` (all keys optional).
    """

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response
        # Read settings lazily so importing this file doesn't require Django.
        try:
            from django.conf import settings  # type: ignore[import-not-found]

            cfg = getattr(settings, "AGENTSCORE_NOINDEX", {}) or {}
        except Exception:
            cfg = {}
        self._custom = frozenset(cfg.get("custom_paths") or [])
        self._replace = bool(cfg.get("replace_paths", False))
        self._tag = cfg.get("robots_tag", DEFAULT_ROBOTS_TAG)

    def __call__(self, request: Any) -> Any:
        response = self.get_response(request)
        path = getattr(request, "path", "")
        is_discovery = (
            (path in self._custom) if self._replace else (path in DEFAULT_DISCOVERY_PATHS or path in self._custom)
        )
        if not is_discovery:
            response["X-Robots-Tag"] = self._tag
        return response
