"""Detect whether a request is a settle leg (carries a payment credential).

The complement is a discovery leg — no credential, expects a 402.

Used by the gate-conditional mount pattern documented in CLAUDE.md: mount
``AgentScoreGate`` on a route only when payment is being attempted, so the
discovery leg flows through unauthenticated and gets a 402 with all rails.

Three credential channels are checked:

- ``Payment-Signature`` — MPP credentials (Tempo, Solana, Stripe SPT)
- ``X-Payment`` — x402 v1 EIP-3009 credentials
- ``Authorization: Payment <jwt>`` — x402 v2 / paymentauth.org credentials

Mirrors node-commerce ``src/payment/payment_header.ts``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _read_header(headers: Any, name: str) -> str | None:
    """Read a header in a framework-agnostic way.

    Accepts:

    - ``Mapping[str, str]`` (Flask, plain dict)
    - Starlette / FastAPI ``Headers`` (has ``.get(name)``)
    - aiohttp ``CIMultiDict`` (has ``.get(name)``)
    - Django ``META`` dict (uppercase + ``HTTP_`` prefix)
    """
    if headers is None:
        return None
    # Web-style Headers (Starlette, aiohttp, Werkzeug) all expose ``.get``.
    getter = getattr(headers, "get", None)
    if callable(getter):
        val = getter(name)
        if val is None:
            val = getter(name.lower())
        if val is None:
            val = getter(name.title())
        if isinstance(val, str):
            return val
        if isinstance(val, (list, tuple)) and val and isinstance(val[0], str):
            return val[0]
    if isinstance(headers, Mapping):
        for key in (name, name.lower(), name.title()):
            if key in headers:
                val = headers[key]
                if isinstance(val, str):
                    return val
                if isinstance(val, (list, tuple)) and val and isinstance(val[0], str):
                    return val[0]
    return None


def _unwrap_headers(request_or_headers: Any) -> Any:
    inner = getattr(request_or_headers, "headers", None)
    return inner if inner is not None else request_or_headers


def has_payment_header(request_or_headers: Any) -> bool:
    """True when the request carries any recognized payment-credential header.

    Accepts a request-like object with a ``.headers`` attribute, OR a headers
    mapping directly (so callers in framework-neutral code can pass
    ``request.headers``).
    """
    headers = _unwrap_headers(request_or_headers)
    if _read_header(headers, "payment-signature"):
        return True
    if _read_header(headers, "x-payment"):
        return True
    auth = _read_header(headers, "authorization")
    return bool(isinstance(auth, str) and auth.startswith("Payment "))


def has_x402_header(request_or_headers: Any) -> bool:
    """True when the request carries an x402 payment credential.

    Matches ``X-Payment`` or ``Payment-Signature``. Use to dispatch the x402 settle path.
    """
    headers = _unwrap_headers(request_or_headers)
    return bool(
        _read_header(headers, "payment-signature") or _read_header(headers, "x-payment"),
    )


def has_mppx_header(request_or_headers: Any) -> bool:
    """True when the request carries an mppx payment credential.

    Matches ``Authorization: Payment <jwt>``. Use to dispatch the MPP settle path.
    """
    headers = _unwrap_headers(request_or_headers)
    auth = _read_header(headers, "authorization")
    return bool(isinstance(auth, str) and auth.startswith("Payment "))
