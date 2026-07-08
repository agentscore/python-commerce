"""Detect whether a request is a settle leg (carries a payment credential).

The complement is a discovery leg — no credential, expects a 402.

Used by the gate-conditional mount pattern: mount ``AgentScoreGate`` on a route
only when payment is being attempted, so the discovery leg flows through
unauthenticated and gets a 402 with all rails.

Three credential channels are checked:

- ``Payment-Signature`` — MPP credentials (Tempo, Solana, Stripe SPT)
- ``X-Payment`` — x402 v1 EIP-3009 credentials
- ``Authorization: Payment <jwt>`` — x402 v2 / paymentauth.org credentials
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
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


_JWT_SHAPE_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def _decodes_to_json_object(token: str) -> bool:
    try:
        padded = token + "=" * (-len(token) % 4)
        try:
            raw = base64.b64decode(padded, validate=False)
        except (ValueError, binascii.Error):
            raw = base64.urlsafe_b64decode(padded)
        return isinstance(json.loads(raw.decode("utf-8")), dict)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False


@dataclass(frozen=True)
class MalformedPaymentCredential:
    """Which credential channel carried the malformed value, and why."""

    channel: str  # "x402" | "mpp"
    message: str


def malformed_payment_credential(request_or_headers: Any) -> MalformedPaymentCredential | None:
    """Wire-shape gate for payment credentials, cheap enough to run before any merchant hook.

    A request whose payment header cannot possibly be a credential (not
    base64/base64url JSON, not a JWT-shaped token) is rejected up front, so junk
    headers never trigger per-request hooks (``pre_validate``, pricing,
    recipient minting) or the identity-gate API call.

    This is deliberately a SHAPE check only. Signature verification, payTo
    binding, and challenge validation stay where they are (the x402 validator
    and the MPP settle path) — those need per-request state the hooks produce.
    A well-formed-but-invalid credential still reaches the real validators and
    fails there.

    Returns ``None`` when every present credential channel is plausibly shaped
    (or no payment header is present).
    """
    headers = _unwrap_headers(request_or_headers)
    x402_token = _read_header(headers, "payment-signature") or _read_header(headers, "x-payment")
    if x402_token:
        if not _decodes_to_json_object(x402_token):
            return MalformedPaymentCredential(
                channel="x402",
                message="X-Payment header is not decodable base64 JSON.",
            )
        return None
    auth = _read_header(headers, "authorization")
    if isinstance(auth, str) and auth.startswith("Payment "):
        token = auth[len("Payment ") :].strip()
        if not token or (not _decodes_to_json_object(token) and not _JWT_SHAPE_RE.match(token)):
            return MalformedPaymentCredential(
                channel="mpp",
                message=("Authorization: Payment credential is neither base64-encoded JSON nor a token-shaped value."),
            )
    return None
