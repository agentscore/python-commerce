"""Signed UCP profile example — ``/.well-known/ucp`` + ``/.well-known/jwks.json``.

AgentScore's ``agentscore-profile+jws`` is a vendor extension layered on top of
the UCP profile for trust-mode verifiers (regulated-commerce, AP2-aware) that
opt into auditable cryptographic provenance. UCP §6 itself does NOT mandate
profile-body signing; production UCP merchants commonly ship unsigned, and
vanilla UCP-aware agents read the canonical body and ignore the ``signature``
field. This example wires both routes against a persistent signing key
(env-loaded for prod, ephemeral for dev) for verifiers that DO opt into the
signed envelope.

Run::

    uv run uvicorn examples.signed_ucp_merchant:app --port 3010

Production checklist:

* Set ``UCP_SIGNING_KEY_JWK_PRIVATE`` to a JSON-encoded private JWK (mint via
  :func:`generate_ucp_signing_key` once, persist in your secret manager).
* The kid in the env JWK MUST match what verifiers will see in your published
  profile — pick a stable name like ``merchant-2026-05``.
* Configure ``Cache-Control: public, max-age=300`` (or longer) on
  ``/.well-known/jwks.json`` so verifiers don't hammer the endpoint.
* Rotate by minting a new key + new kid, publishing both in the JWKS, signing
  new profiles with the new key, then dropping the old JWK after your verifier
  cache TTL expires.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agentscore_commerce.identity import (
    AgentScoreGatePolicy,
    UCPServiceBinding,
    UCPSigningKey,
    UCPVerificationError,
    build_jwks_response,
    build_ucp_profile,
    load_ucp_signing_key_from_env,
    mpp_payment_handler,
    sign_ucp_profile,
    verify_ucp_profile,
)

logger = logging.getLogger("signed_ucp_merchant")

# Env-loader kwargs pin the production kid + alg defaults for this example.
# ``UCP_SIGNING_KEY_JWK_PRIVATE`` (env) wins when set; ``UCP_SIGNING_KEY_KID``
# and ``UCP_SIGNING_KEY_ALG`` override these defaults at runtime. The helper
# caches the loaded key across requests and serializes concurrent first-callers
# so two threads can never publish a JWKS that disagrees with the just-signed JWS.
_SIGNING_KEY_OPTS = {"default_kid": "merchant-2026-05"}


app = FastAPI()


@app.get("/.well-known/ucp")
async def well_known_ucp() -> JSONResponse:
    key = load_ucp_signing_key_from_env(**_SIGNING_KEY_OPTS)
    profile = build_ucp_profile(
        name="My Agent Service",
        services={
            "dev.ucp.shopping": [
                UCPServiceBinding(
                    version="2026-04-08",
                    spec="https://ucp.dev/2026-04-08/specification/overview",
                    transport="mcp",
                    endpoint="https://agents.example.com/api/ucp/mcp",
                    schema="https://ucp.dev/services/shopping/mcp.openrpc.json",
                ),
            ],
        },
        payment_handlers={
            **mpp_payment_handler(
                networks=[
                    {"network": "tempo-mainnet", "chain_id": 4217, "recipient": "0xfeedface"},
                ]
            ),
        },
        signing_keys=[UCPSigningKey.from_jwk(key.public_jwk)],
        # Optional: declare merchant gate policy as an `sh.agentscore.identity` capability
        # binding inside the public profile. Static policy declaration only — no per-operator
        # claims. Per-operator identity attestation flows through the AP2 risk-signal endpoint.
        agentscore_gate=AgentScoreGatePolicy(
            require_kyc=True,
            min_age=21,
            allowed_jurisdictions=["US"],
        ),
    )
    signed = sign_ucp_profile(
        profile.to_dict(),
        signing_key=key.private_key,
        kid=key.public_jwk["kid"],
        alg=key.public_jwk.get("alg", "EdDSA"),
    )
    return JSONResponse(signed, headers={"Cache-Control": "public, max-age=60"})


@app.get("/.well-known/jwks.json")
async def well_known_jwks() -> JSONResponse:
    key = load_ucp_signing_key_from_env(**_SIGNING_KEY_OPTS)
    return JSONResponse(
        build_jwks_response([key.public_jwk]),
        headers={
            "Cache-Control": "public, max-age=300",
            "Content-Type": "application/jwk-set+json",
        },
    )


@app.get("/_selftest/ucp")
async def selftest() -> JSONResponse:
    """Local round-trip: sign+serve+fetch+verify, return UCPVerificationError code on failure."""
    profile_resp = await well_known_ucp()
    jwks_resp = await well_known_jwks()
    # FastAPI's `JSONResponse.body` is typed as `bytes | memoryview[int]`; coerce to
    # plain `bytes` so `json.loads` accepts both branches without a type error.
    profile = json.loads(bytes(profile_resp.body))
    jwks = json.loads(bytes(jwks_resp.body))
    try:
        verify_ucp_profile(profile, jwks)
        return JSONResponse({"ok": True, "kid": profile["signing_keys"][0]["kid"]})
    except UCPVerificationError as exc:
        logger.exception("UCP self-test verification failed")
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": type(exc).__name__},
            status_code=500,
        )
