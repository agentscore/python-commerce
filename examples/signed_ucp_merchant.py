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

import asyncio
import json
import logging
import os
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agentscore_commerce.identity import (
    UCPPaymentHandlerBinding,
    UCPServiceBinding,
    UCPSigningKey,
    UCPVerificationError,
    build_jwks_response,
    build_ucp_profile,
    generate_ucp_signing_key,
    sign_ucp_profile,
    verify_ucp_profile,
)
from agentscore_commerce.identity.ucp_jwks import GeneratedUCPKey

logger = logging.getLogger("signed_ucp_merchant")

KID = os.environ.get("UCP_SIGNING_KEY_KID", "merchant-2026-05")
ALG: Literal["EdDSA", "ES256"] = "ES256" if os.environ.get("UCP_SIGNING_KEY_ALG") == "ES256" else "EdDSA"

# Asyncio lock + cached Future so concurrent first-callers don't generate
# different keys (race condition fix).
_lock = asyncio.Lock()
_cached: GeneratedUCPKey | None = None


async def load_signing_key() -> GeneratedUCPKey:
    global _cached
    async with _lock:
        if _cached is not None:
            return _cached
        env_jwk = os.environ.get("UCP_SIGNING_KEY_JWK_PRIVATE")
        if env_jwk:
            from joserfc.jwk import ECKey, OKPKey  # type: ignore[import-not-found]

            try:
                jwk_dict = json.loads(env_jwk)
            except json.JSONDecodeError as exc:
                msg = f"UCP_SIGNING_KEY_JWK_PRIVATE is not valid JSON: {exc}"
                raise ValueError(msg) from exc
            # Detect alg from JWK shape; ignore env if it conflicts.
            kty = jwk_dict.get("kty")
            crv = jwk_dict.get("crv")
            if kty == "OKP" and crv == "Ed25519":
                priv = OKPKey.import_key(jwk_dict)
                effective_alg: Literal["EdDSA", "ES256"] = "EdDSA"
            elif kty == "EC" and crv == "P-256":
                priv = ECKey.import_key(jwk_dict)
                effective_alg = "ES256"
            else:
                msg = f"Unsupported env JWK: kty={kty} crv={crv}"
                raise ValueError(msg)
            public_jwk: dict[str, Any] = priv.as_dict(private=False)
            public_jwk.setdefault("kid", jwk_dict.get("kid", KID))
            public_jwk["alg"] = effective_alg
            public_jwk["use"] = "sig"
            _cached = GeneratedUCPKey(private_key=priv, public_jwk=public_jwk)
            return _cached
        logger.warning(
            "UCP_SIGNING_KEY_JWK_PRIVATE not set — generating ephemeral key. "
            "Verifier caches will break across restarts."
        )
        _cached = generate_ucp_signing_key(kid=KID, alg=ALG)
        return _cached


app = FastAPI()


@app.get("/.well-known/ucp")
async def well_known_ucp() -> JSONResponse:
    key = await load_signing_key()
    profile = build_ucp_profile(
        name="My Agent Service",
        services={
            "dev.ucp.shopping": [
                UCPServiceBinding(
                    version="2026-04-08",
                    spec="https://ucp.dev/2026-04-08/specification/overview",
                    transport="mcp",
                    endpoint="https://agents.example.com/api/ucp/mcp",
                    schema="https://ucp.dev/services/shopping/openrpc.json",
                ),
            ],
        },
        payment_handlers={
            "sh.agentscore.payment.tempo": [
                UCPPaymentHandlerBinding(
                    id="tempo",
                    version="2026-04-08",
                    spec="https://agentscore.sh/specification/payment-handlers/tempo",
                    schema="https://agentscore.sh/schemas/payment-handlers/tempo.json",
                    config={"recipient": "0xfeedface"},
                ),
            ],
        },
        signing_keys=[UCPSigningKey.from_jwk(key.public_jwk)],
    )
    signed = sign_ucp_profile(
        profile.to_dict(),
        signing_key=key.private_key,
        kid=key.public_jwk["kid"],
        alg=key.public_jwk.get("alg", ALG),
    )
    return JSONResponse(signed, headers={"Cache-Control": "public, max-age=60"})


@app.get("/.well-known/jwks.json")
async def well_known_jwks() -> JSONResponse:
    key = await load_signing_key()
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
    profile = json.loads(profile_resp.body.decode())
    jwks = json.loads(jwks_resp.body.decode())
    try:
        verify_ucp_profile(profile, jwks)
        return JSONResponse({"ok": True, "kid": profile["signing_keys"][0]["kid"]})
    except UCPVerificationError as exc:
        logger.exception("UCP self-test verification failed")
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": type(exc).__name__},
            status_code=500,
        )
