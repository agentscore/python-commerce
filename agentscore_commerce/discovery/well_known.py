"""Spec-rooted helpers for ``/.well-known/{ucp,jwks.json}`` discovery surfaces.

What this module collapses for every UCP-publishing merchant:

* Loading + caching the signing key via :func:`load_ucp_signing_key_from_env`.
* Composing the ``payment_handlers`` map from the merchant's :class:`Checkout`
  rails (TempoRailSpec → mpp_payment_handler; X402BaseRailSpec → x402_payment_handler;
  StripeRailSpec → stripe_spt_payment_handler).
* Building the unsigned profile + signing it.
* Cache-Control + CORS + X-Request-ID echo per UCP §6.
* RFC 7517 §8.5 ``application/jwk-set+json`` media type on JWKS.
* The 503 ``ucp_misconfigured`` fallback envelope when no handlers can be
  derived (empty rails dict OR all rails have empty recipients).

Each helper returns a framework-neutral :class:`SignedDiscoveryResponse` that
merchants wrap in their framework's Response builder (FastAPI ``Response``,
aiohttp ``web.Response``, Flask ``Response``, Django ``HttpResponse``, etc.).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentscore_commerce.identity.ucp import (
    AgentScoreGatePolicy,
    UCPServiceBinding,
    UCPSigningKey,
    build_ucp_profile,
    mpp_payment_handler,
    stripe_spt_payment_handler,
    x402_payment_handler,
)
from agentscore_commerce.identity.ucp_jwks import (
    build_jwks_response,
    load_ucp_signing_key_from_env,
    sign_ucp_profile,
)
from agentscore_commerce.payment.rail_spec import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    TempoSessionRailSpec,
    X402BaseRailSpec,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentscore_commerce.checkout import Checkout

_UCP_CACHE_SECONDS = 60
_JWKS_CACHE_SECONDS = 300


@dataclass
class SignedDiscoveryResponse:
    """Framework-neutral response shape for discovery endpoints.

    Wrap in your framework's response builder. The body is already JSON-encoded
    bytes; do not re-serialize.
    """

    content: bytes
    media_type: str
    headers: dict[str, str] = field(default_factory=dict)
    status: int = 200


def _request_id(request_headers: Mapping[str, str] | None) -> str | None:
    if request_headers is None:
        return None
    for key, value in request_headers.items():
        if key.lower() == "x-request-id":
            return value
    return None


def _attach_request_id(headers: dict[str, str], request_headers: Mapping[str, str] | None) -> None:
    rid = _request_id(request_headers)
    if rid is not None:
        headers["X-Request-ID"] = rid


def _compose_handlers(checkout: Checkout) -> dict[str, list[Any]]:
    """Map rails on the Checkout to a UCP ``payment_handlers`` block.

    Includes rails with empty-string-sentinel recipients (per-order-mint
    pattern) — the static UCP profile drops the recipient field from those
    entries, and the authoritative per-order recipient ships in the 402 body
    at request time. Only rails missing the ``recipient`` attribute entirely
    are excluded.
    """
    handlers: dict[str, list[Any]] = {}
    mpp: list[TempoRailSpec | SolanaMppRailSpec | TempoSessionRailSpec] = []
    x402: list[X402BaseRailSpec] = []
    stripe: list[StripeRailSpec] = []
    for spec in checkout.rails.values():
        if isinstance(spec, (TempoRailSpec, TempoSessionRailSpec, SolanaMppRailSpec)):
            if hasattr(spec, "recipient"):
                mpp.append(spec)
        elif isinstance(spec, X402BaseRailSpec):
            if hasattr(spec, "recipient"):
                x402.append(spec)
        elif isinstance(spec, StripeRailSpec):
            stripe.append(spec)
    if mpp:
        handlers.update(mpp_payment_handler(networks=mpp))
    if x402:
        handlers.update(x402_payment_handler(networks=x402))
    for spec in stripe:
        handlers.update(stripe_spt_payment_handler(spec=spec))
    return handlers


def _misconfigured_response(request_headers: Mapping[str, str] | None) -> SignedDiscoveryResponse:
    body = {
        "error": {
            "code": "ucp_misconfigured",
            "message": "Merchant has no configured payment handlers.",
        },
        "next_steps": {
            "action": "contact_merchant",
            "user_message": "This merchant is temporarily unable to accept agent payments.",
        },
        "agent_instructions": {
            "action": "contact_merchant",
            "steps": [
                "Surface a transient error to the user.",
                "Retry later; the merchant operator will repair the configuration.",
            ],
            "user_message": "Merchant temporarily offline for agent payments.",
        },
    }
    # UCP §6 forbids `no-store` on profile responses. 60s is the minimum cache age;
    # short enough that recovery is fast once the merchant restores config.
    headers: dict[str, str] = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": f"public, max-age={_UCP_CACHE_SECONDS}",
    }
    _attach_request_id(headers, request_headers)
    return SignedDiscoveryResponse(
        content=json.dumps(body).encode(),
        media_type="application/json",
        headers=headers,
        status=503,
    )


def build_signed_ucp_response(
    *,
    checkout: Checkout,
    name: str,
    well_known_ucp_url: str,
    services: dict[str, list[UCPServiceBinding]],
    request_headers: Mapping[str, str] | None = None,
    signing_kid: str = "merchant-default",
    agentscore_gate: AgentScoreGatePolicy | None = None,
) -> SignedDiscoveryResponse:
    """Build the signed UCP profile response for ``/.well-known/ucp``.

    Composes payment handlers from the Checkout's rails dict, builds the
    profile via :func:`build_ucp_profile`, signs via :func:`sign_ucp_profile`,
    and attaches the UCP §6-prescribed Cache-Control + CORS + X-Request-ID
    headers.

    Returns a 503 ``ucp_misconfigured`` envelope (still with the §6-compliant
    Cache-Control) when no payment handlers can be derived from rails.

    ``services`` is the spec-compliant services map (keyed by reverse-DNS
    service name). ``well_known_ucp_url`` is the canonical URL of this profile,
    surfaced as the value in ``supported_versions``.
    """
    handlers = _compose_handlers(checkout)
    if not handlers:
        return _misconfigured_response(request_headers)

    key = load_ucp_signing_key_from_env(default_kid=signing_kid)
    signing_key_entry = UCPSigningKey.from_jwk(key.public_jwk)

    profile = build_ucp_profile(
        name=name,
        supported_versions={"2026-04-08": well_known_ucp_url},
        agentscore_gate=agentscore_gate,
        services=services,
        payment_handlers=handlers,
        signing_keys=[signing_key_entry],
    )
    signed = sign_ucp_profile(
        profile.to_dict(),
        signing_key=key.private_key,
        kid=key.public_jwk["kid"],
        alg=key.public_jwk.get("alg", "EdDSA"),
    )
    headers: dict[str, str] = {
        "Cache-Control": f"public, max-age={_UCP_CACHE_SECONDS}",
        "Access-Control-Allow-Origin": "*",
    }
    _attach_request_id(headers, request_headers)
    return SignedDiscoveryResponse(
        content=json.dumps(signed).encode(),
        media_type="application/json",
        headers=headers,
    )


def build_signed_jwks_response(
    *,
    request_headers: Mapping[str, str] | None = None,
    signing_kid: str = "merchant-default",
) -> SignedDiscoveryResponse:
    """Build the JWKS response for ``/.well-known/jwks.json``.

    RFC 7517 §8.5 prescribes ``application/jwk-set+json``. Five-minute
    Cache-Control balances verifier-side cache hit rate against rotation
    propagation latency.
    """
    key = load_ucp_signing_key_from_env(default_kid=signing_kid)
    jwks = build_jwks_response([key.public_jwk])
    headers: dict[str, str] = {
        "Cache-Control": f"public, max-age={_JWKS_CACHE_SECONDS}",
        "Access-Control-Allow-Origin": "*",
    }
    _attach_request_id(headers, request_headers)
    return SignedDiscoveryResponse(
        content=json.dumps(jwks).encode(),
        media_type="application/jwk-set+json",
        headers=headers,
    )


def well_known_cors_preflight_headers(
    request_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """CORS preflight headers for ``/.well-known/*`` endpoints.

    Echoes ``Access-Control-Request-Headers`` verbatim when present rather
    than advertising ``*`` (which browsers reject with credentials in scope).
    Returns a 204 on the corresponding response via the merchant's framework.
    """
    headers: dict[str, str] = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Max-Age": "86400",
        "Vary": "Access-Control-Request-Headers",
    }
    if request_headers is not None:
        for key, value in request_headers.items():
            if key.lower() == "access-control-request-headers":
                headers["Access-Control-Allow-Headers"] = value
                break
    return headers


@dataclass
class WellKnownPreflightResponse:
    """Framework-neutral 204 preflight result.

    Merchants wrap into their framework's response shape (FastAPI ``Response``,
    Flask ``Response``, etc.).
    """

    status: int
    headers: dict[str, str]
    content: bytes = b""


def well_known_preflight_response(
    request_headers: Mapping[str, str] | None = None,
) -> WellKnownPreflightResponse:
    """Build a 204 CORS preflight response for ``/.well-known/*`` endpoints.

    Wraps :func:`well_known_cors_preflight_headers`. Universal across every
    UCP-publishing merchant; saves the 3-line ``Response(status_code=204,
    headers=...)`` wrapper every consumer otherwise hand-rolls.
    """
    return WellKnownPreflightResponse(
        status=204,
        headers=well_known_cors_preflight_headers(request_headers),
    )


_UCP_SHOPPING_SPEC_2026_04_08 = "https://ucp.dev/2026-04-08/specification/overview"


def default_a2a_services(*, agent_card_url: str) -> dict[str, list[UCPServiceBinding]]:
    """Canonical UCP §services map for a merchant publishing an A2A agent card.

    Returns ``{"dev.ucp.shopping": [UCPServiceBinding(version="2026-04-08",
    spec="<UCP shopping spec>", transport="a2a", endpoint=agent_card_url)]}`` ;
    the binding every UCP-publishing merchant declares when their primary agent
    surface is the A2A v1.0 ``/.well-known/agent-card.json`` (versus a UCP MCP
    or REST endpoint).

    Merchants who additionally expose a UCP MCP or REST transport append further
    bindings to the same ``dev.ucp.shopping`` list.
    """
    return {
        "dev.ucp.shopping": [
            UCPServiceBinding(
                version="2026-04-08",
                spec=_UCP_SHOPPING_SPEC_2026_04_08,
                transport="a2a",
                endpoint=agent_card_url,
            ),
        ],
    }


def bootstrap_ucp_signing_key(*, default_kid: str = "merchant-default") -> None:
    """Eager-load the UCP signing key at startup.

    A malformed ``UCP_SIGNING_KEY_JWK_PRIVATE`` env value otherwise surfaces
    on the first ``/.well-known/ucp`` hit after deploy, masquerading as a
    runtime 500. Calling this in the framework's startup / lifespan hook
    fails the deploy fast.

    Wraps :func:`load_ucp_signing_key_from_env`; raises ``ValueError`` (per
    that helper's contract) on a malformed JWK so the orchestrator marks the
    task unhealthy.
    """
    load_ucp_signing_key_from_env(default_kid=default_kid)


__all__ = [
    "SignedDiscoveryResponse",
    "WellKnownPreflightResponse",
    "bootstrap_ucp_signing_key",
    "build_signed_jwks_response",
    "build_signed_ucp_response",
    "default_a2a_services",
    "well_known_cors_preflight_headers",
    "well_known_preflight_response",
]
