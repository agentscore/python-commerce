"""Signed UCP profile example: ``/.well-known/ucp`` + ``/.well-known/jwks.json``.

AgentScore's ``agentscore-profile+jws`` is a vendor extension on top of UCP for
trust-mode verifiers (regulated-commerce, AP2-aware) that opt into auditable
cryptographic provenance. UCP §6 itself does NOT mandate profile-body signing;
production UCP merchants commonly ship unsigned, and vanilla UCP-aware agents
read the canonical body and ignore the ``signature`` field.

The 2.0 SDK ships :meth:`Checkout.mount_ucp_routes_fastapi` (and one for each
framework adapter) which folds loading + signing + Cache-Control + CORS + the
3-route registration block (GET ucp + GET jwks + OPTIONS preflight) into one
call. Pass the merchant's :class:`Checkout` and the helpers compose the
``payment_handlers`` block from the configured rails automatically.

Run::

    uv run uvicorn examples.signed_ucp_merchant:app --port 3010

Production checklist:

* Set ``UCP_SIGNING_KEY_JWK_PRIVATE`` to a JSON-encoded private JWK (mint via
  :func:`generate_ucp_signing_key` once, persist in your secret manager).
* The kid in the env JWK MUST match what verifiers will see in your published
  profile; pick a stable name like ``merchant-2026-05``.
* Rotate by minting a new key + new kid, publishing both in the JWKS, signing
  new profiles with the new key, then dropping the old JWK after your verifier
  cache TTL expires.

Call :func:`bootstrap_ucp_signing_key` in your lifespan handler so a malformed
``UCP_SIGNING_KEY_JWK_PRIVATE`` env value fails the deploy fast instead of
surfacing on the first ``/.well-known/ucp`` hit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce import AgentScoreGatePolicy, Checkout, PricingResult, TempoRailSpec
from agentscore_commerce.discovery import bootstrap_ucp_signing_key, default_a2a_services
from agentscore_commerce.middleware.fastapi import RateLimitMiddleware

SIGNING_KID = "merchant-2026-05"


async def _compute_pricing(_ctx: Any) -> PricingResult:
    return PricingResult(amount_usd=1.0)


checkout = Checkout(
    rails={"tempo": TempoRailSpec(recipient="0xfeedface")},
    url="https://agents.example.com/purchase",
    compute_pricing=_compute_pricing,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bootstrap_ucp_signing_key(default_kid=SIGNING_KID)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)

checkout.mount_ucp_routes_fastapi(
    app,
    name="My Agent Service",
    well_known_ucp_url="https://agents.example.com/.well-known/ucp",
    services=default_a2a_services(agent_card_url="https://agents.example.com/.well-known/agent-card.json"),
    signing_kid=SIGNING_KID,
    agentscore_gate=AgentScoreGatePolicy(
        require_kyc=True,
        min_age=21,
        allowed_jurisdictions=["US"],
    ),
)


@app.get("/_selftest/ucp")
async def selftest(request: Request) -> JSONResponse:
    """Local round-trip: sign+serve+fetch+verify, return UCPVerificationError code on failure."""
    import json

    from starlette.testclient import TestClient

    from agentscore_commerce import UCPVerificationError, verify_ucp_profile

    client = TestClient(app)
    profile = client.get("/.well-known/ucp").json()
    jwks = json.loads(client.get("/.well-known/jwks.json").content)
    try:
        verify_ucp_profile(profile, jwks)
        return JSONResponse({"ok": True, "kid": profile["signing_keys"][0]["kid"]})
    except UCPVerificationError as exc:
        return JSONResponse({"ok": False, "code": exc.code}, status_code=500)
