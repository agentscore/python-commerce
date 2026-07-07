"""Build an AIP :class:`VerifyRequestContext` from a framework request.

Every framework adapter ultimately has (or can produce) the request primitives the AIP
verifier needs: method, authority, path, the ``Agent-Identity`` header(s), and the RFC 9421
``Signature-Input`` / ``Signature`` pair. This helper centralizes the header + URL extraction
so adapters stay thin and the parsing rules (authority derivation, multiple ``Agent-Identity``
headers) live in one place.

Two entry points, mirroring the the reference ``aip/request`` module:

* :func:`build_verify_context_from_request` — for frameworks that expose a request object with
  ``method`` / ``url`` / ``headers`` (Starlette, FastAPI, aiohttp, Sanic, …). The node analog
  takes a WHATWG ``Request``.
* :func:`build_verify_context_from_parts` — for frameworks that hand you raw pieces (a header
  mapping + method + URL/target), e.g. Flask/Django/WSGI. The node analog takes a Node-style
  header map.

This module performs no cryptography; it only shapes request data. The ``path`` and
``authority`` it returns feed the RFC 9421 signature base, so the ``path`` derivation must
match the bytes the signer produced from ``URL.pathname`` (see :func:`build_verify_context_from_parts`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable
from urllib.parse import urlsplit

# Header constant lives with the verifier (mirrors node's `./verify` ownership).
from agentscore_commerce.aip.verify import AGENT_IDENTITY_HEADER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentscore_commerce.aip.verify import VerifyRequestContext

# Matches node's `/^[a-z][a-z0-9+.-]*:\/\//i`: an absolute URL must carry a `scheme://`
# prefix. This is intentionally stricter than `urlsplit(...).scheme` (which treats opaque
# `scheme:opaque` strings as having a scheme) so the absolute-vs-origin-form branch in
# `build_verify_context_from_parts` matches the signer's `new URL()` parse byte-for-byte.
_ABSOLUTE_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


@runtime_checkable
class HeadersLike(Protocol):
    """Minimal multidict header surface (Starlette ``Headers``, ``multidict.CIMultiDict``).

    ``get`` returns the first value (or ``None``); ``getlist`` returns every value for a
    repeated header. Both are case-insensitive for these implementations.
    """

    def get(self, key: str, default: Any = ..., /) -> Any: ...

    def getlist(self, key: str) -> list[str]: ...


@runtime_checkable
class RequestLike(Protocol):
    """Minimal request surface the AIP context builder reads.

    Satisfied structurally by Starlette/FastAPI ``Request`` (and any object exposing the same
    three attributes). ``url`` may be a string or any object whose ``str()`` is the full URL
    (Starlette's ``URL`` qualifies); ``headers`` is a :class:`HeadersLike` multidict.
    """

    @property
    def method(self) -> str: ...

    @property
    def url(self) -> Any: ...

    @property
    def headers(self) -> HeadersLike: ...


class VerifyContextParts(TypedDict, total=False):
    """Raw request parts for :func:`build_verify_context_from_parts`.

    Mirrors the reference ``buildVerifyContextFromParts`` object argument. ``method`` / ``url`` /
    ``headers`` are required in practice; ``authority`` is optional (falls back to the ``host``
    header). Typed ``total=False`` so the optional ``authority`` is expressible; missing
    ``method`` / ``url`` / ``headers`` would be a programmer error, not a supported shape.
    """

    method: str
    url: str
    headers: Mapping[str, str | list[str] | None]
    authority: str | None


def _split_agent_identity(raw: str) -> list[str]:
    """Split a (possibly comma-folded) ``Agent-Identity`` value into individual AITs.

    The Fetch/Starlette header layer may fold repeated headers into a single comma-joined
    value; for ``Agent-Identity`` we split them back out because each AIT is an independent
    JWT. JWTs are base64url dot-separated and never contain a bare comma, so splitting on
    ``,`` is safe.
    """
    return [s for s in (part.strip() for part in raw.split(",")) if s]


def _read_agent_identity_headers(headers: HeadersLike) -> list[str]:
    """Read all ``Agent-Identity`` values from a multidict header object.

    Mirrors the reference ``readAgentIdentityHeaders``. Node reads the WHATWG ``Headers.get`` value
    (which comma-folds repeats) and splits on ``,``. Starlette's ``Headers.get`` returns only
    the first match, so we prefer ``getlist`` to recover every repeated header, then split each
    on ``,`` as well — this yields the identical AIT set whether the proxy folded the headers
    into one line or kept them separate.
    """
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = getlist(AGENT_IDENTITY_HEADER)
        out: list[str] = []
        for value in values:
            if value:
                out.extend(_split_agent_identity(value))
        return out
    raw = headers.get(AGENT_IDENTITY_HEADER)
    if not raw:
        return []
    return _split_agent_identity(raw)


def _derive_authority(headers: HeadersLike, host_fallback: str) -> str:
    """Derive ``@authority`` for RFC 9421.

    Prefer the ``Host`` header (what the client addressed); fall back to the URL host. Returned
    as-is; the verifier's signature-base construction canonicalizes it (mirrors node, where
    ``normalizeAuthority`` runs there, not here).
    """
    host = headers.get("host")
    return host if isinstance(host, str) and host else host_fallback


def build_verify_context_from_request(req: RequestLike) -> VerifyRequestContext:
    """Build the framework-agnostic verify context from a request object.

    Accepts any object exposing ``method`` / ``url`` / ``headers`` (Starlette/FastAPI/aiohttp/
    Sanic ``Request``). The node analog takes a WHATWG ``Request``.
    """
    from agentscore_commerce.aip.verify import VerifyRequestContext

    parts = urlsplit(str(req.url))
    headers = req.headers
    sig_input = headers.get("signature-input")
    sig = headers.get("signature")
    return VerifyRequestContext(
        method=req.method,
        authority=_derive_authority(headers, parts.netloc),
        path=parts.path,
        agent_identity_headers=_read_agent_identity_headers(headers),
        signature_input=sig_input if isinstance(sig_input, str) else None,
        signature=sig if isinstance(sig, str) else None,
    )


def has_agent_identity_header(req: RequestLike) -> bool:
    """True when the request carries an AIP credential (at least one ``Agent-Identity`` header)."""
    return len(_read_agent_identity_headers(req.headers)) > 0


def _read_mapping_header(headers: Mapping[str, str | list[str] | None], name: str) -> str | None:
    """Read a header from a plain (Node-style) header mapping.

    A value may be ``str``, ``list[str]``, or ``None``. Lookup falls back to the lowercased name
    (mirrors node's ``readNodeHeader``); list values are joined with ``", "`` like the WHATWG
    folding the node map shim emulates.
    """
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(value)
    return value


def has_agent_identity_header_parts(headers: Mapping[str, str | list[str] | None]) -> bool:
    """True when a plain header mapping carries an ``Agent-Identity`` header.

    Mirrors the reference ``hasAgentIdentityHeaderNode`` — used by adapters (Flask/Django/WSGI) that
    pass a raw header mapping rather than a request object.
    """
    raw = _read_mapping_header(headers, AGENT_IDENTITY_HEADER)
    return raw is not None and any(part.strip() for part in raw.split(","))


def build_verify_context_from_parts(parts: VerifyContextParts) -> VerifyRequestContext:
    """Build the verify context from raw request parts (header mapping + method + URL).

    For frameworks that don't expose a request object (Flask/Django/WSGI). The node analog takes
    Express/Fastify-style parts. ``parts`` carries:

    * ``method`` — the HTTP method.
    * ``url`` — full request URL, or just the origin-form target (``"/checkout?..."``); used to
      derive ``@path``.
    * ``headers`` — a Node-style header mapping (values ``str`` / ``list[str]`` / ``None``).
    * ``authority`` (optional) — authority override; falls back to the ``host`` header.
    """
    from agentscore_commerce.aip.verify import VerifyRequestContext

    method = parts["method"]
    url = parts["url"]
    headers = parts["headers"]
    authority = parts.get("authority")

    agent_identity_raw = _read_mapping_header(headers, AGENT_IDENTITY_HEADER)
    agent_identity_headers = _split_agent_identity(agent_identity_raw) if agent_identity_raw else []

    host = authority if authority is not None else (_read_mapping_header(headers, "host") or "")

    # ``url`` may be an absolute URL or an origin-form target ("/checkout?...", possibly "//x").
    # Build the URL by APPENDING the target to the origin (not resolving it as a reference) so a
    # leading "//" is treated as PATH — resolving "//x" against a base mis-reads it as a
    # protocol-relative authority and drops it, diverging from the signer's ``URL.pathname`` and
    # failing PoP. Always assigned in both branches below.
    path: str
    try:
        if _ABSOLUTE_URL_RE.match(url):
            # Absolute URL ("scheme://host/p"): take its path directly.
            path = urlsplit(url).path
        else:
            target = url if url.startswith("/") else f"/{url}"
            path = urlsplit(f"http://{host or 'localhost'}{target}").path
    except ValueError:
        q = url.find("?")
        path = url if q == -1 else url[:q]

    sig_input = _read_mapping_header(headers, "signature-input")
    sig = _read_mapping_header(headers, "signature")
    return VerifyRequestContext(
        method=method,
        authority=host,
        path=path,
        agent_identity_headers=agent_identity_headers,
        signature_input=sig_input,
        signature=sig,
    )


__all__ = [
    "HeadersLike",
    "RequestLike",
    "VerifyContextParts",
    "build_verify_context_from_parts",
    "build_verify_context_from_request",
    "has_agent_identity_header",
    "has_agent_identity_header_parts",
]
