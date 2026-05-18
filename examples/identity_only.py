"""Example: identity gate without payment

Scenario: you have an existing checkout / payment flow you don't want to change,
but you want to verify the agent is KYC'd before letting them transact. Use the
commerce/identity middleware as a thin wrapper over your existing endpoints.

Common cases:
    * Compliance-required content (age-gated, sanctioned-restricted)
    * High-value transactions where you want extra identity assurance
    * Adding agent KYC to an existing human-only Stripe checkout

This is the smallest possible commerce integration. Mount the gate, write your
route, done. No 402 logic, no payment plumbing; just identity gating.

Peer deps:
    pip install agentscore-commerce[fastapi]

Env vars:
    AGENTSCORE_API_KEY — your AgentScore API key

Run: uvicorn examples.identity_only:app --port 3000
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Request

from agentscore_commerce import CreateSessionOnMissing
from agentscore_commerce.identity.fastapi import (
    AgentScoreGate,
    capture_wallet,
    get_agentscore_data,
)
from agentscore_commerce.middleware.fastapi import RateLimitMiddleware

app = FastAPI()
app.add_middleware(RateLimitMiddleware)

API_KEY = os.environ.get("AGENTSCORE_API_KEY", "ask_test_dummy")

# ── Apply identity gate to specific routes ──────────────────────────────────
gate = AgentScoreGate(
    api_key=API_KEY,
    require_kyc=True,
    require_sanctions_clear=True,
    min_age=21,
    allowed_jurisdictions=["US"],
    # When the agent has no identity header, auto-create a verification session
    # so the 403 body carries verify_url + poll_secret + agent_instructions.
    create_session_on_missing=CreateSessionOnMissing(
        api_key=API_KEY,
        context="restricted-access",
    ),
)


@app.post("/restricted", dependencies=[Depends(gate)])
async def restricted(assess: dict[str, Any] = Depends(get_agentscore_data)) -> dict[str, Any]:
    """Gated route — only reached when the agent passes the compliance policy.

    `assess` is the raw `/v1/assess` response: ``{ decision, operator,
    kyc_verified, age_bracket, jurisdiction, ... }``. Run your own business
    logic here; buy something via your existing Stripe flow, grant access to
    gated content, write to your DB, whatever. AgentScore's job ends at "this
    agent is verified, here's their operator id."
    """
    return {"ok": True, "operator": assess.get("resolved_operator")}


# ── Optional: capture an agent's wallet after payment lands ────────────────
# (only relevant if your downstream payment flow exposes the signer wallet)
@app.post("/restricted/capture-wallet-example", dependencies=[Depends(gate)])
async def capture_wallet_example(request: Request) -> dict[str, Any]:
    body = await request.json()
    await capture_wallet(
        request,
        wallet_address=body["signer_address"],
        network="evm",
        idempotency_key=body.get("payment_intent_id"),
    )
    return {"ok": True}


# ── Public routes (no gate) ────────────────────────────────────────────────
@app.get("/public-info")
async def public_info() -> dict[str, str]:
    return {"message": "open access — no identity required"}
