"""Example: multi-product merchant with per-product compliance policy + soft mode

Scenario: you sell several products with different compliance needs.
- Wine: hard gate, KYC + 21+ + US-only + state allowlist (regulated alcohol)
- Tee:  no gate at all — fully anonymous, ship anywhere
- Limited print: SOFT gate — request KYC for fraud signals, but don't block sale
                 if the buyer skips it; record identity_status="unverified" instead

Each product carries its own policy block (in this example, a Python dict the
merchant looks up from a database row). The route uses three helpers from
``agentscore_commerce.identity.policy``:

    - build_gate_from_policy(policy, *, api_key) → AgentScoreGate | None
        Returns None when the policy has no enforcement (no gate fires).
    - run_gate_with_enforcement(request, gate, *, enforcement) → GateResult
        Runs the gate, swallows soft denials, returns a structured result.
    - shipping_country_allowed / shipping_state_allowed
        Per-product shipping allowlists (NULL = ship anywhere).

Peer deps:
    pip install agentscore-commerce[fastapi]

Env vars:
    AGENTSCORE_API_KEY — your AgentScore API key

Run: uvicorn examples.per_product_policy_merchant:app --port 3000
"""

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce.identity.policy import (
    PolicyBlock,
    build_gate_from_policy,
    run_gate_with_enforcement,
    shipping_country_allowed,
    shipping_state_allowed,
)

API_KEY = os.environ.get("AGENTSCORE_API_KEY", "ask_test_dummy")

# A merchant would normally read these from a `products` table. Each row carries
# its own compliance config; the keys match `PolicyBlock`.
PRODUCTS: dict[str, dict[str, Any]] = {
    "wine-cabernet": {
        "name": "Reserve Cabernet",
        "price_usd": 75.00,
        "policy": PolicyBlock(
            enforcement="hard",
            require_kyc=True,
            require_sanctions_clear=True,
            min_age=21,
            allowed_jurisdictions=["US"],
            allowed_shipping_countries=["US"],
            allowed_shipping_states=["CA", "NY", "TX", "FL", "WA"],  # abridged
        ),
    },
    "tee": {
        "name": "Cotton Tee",
        "price_usd": 30.00,
        "policy": None,  # No gate; ship anywhere; identity_status="anonymous"
    },
    "limited-print": {
        "name": "Limited Edition Print (200/500)",
        "price_usd": 200.00,
        # Soft gate: request KYC as a fraud signal, but accept anonymous sales.
        # On miss, identity_status="unverified" stamps the order so ops can flag it.
        "policy": PolicyBlock(enforcement="soft", require_kyc=True),
    },
}


app = FastAPI()


@app.post("/purchase")
async def purchase(request: Request) -> JSONResponse:
    body = await request.json()
    slug = body.get("product_slug")
    shipping = body.get("shipping", {})

    product = PRODUCTS.get(slug)
    if product is None:
        return JSONResponse({"error": {"code": "product_not_found"}}, status_code=400)

    policy = product["policy"]

    # Per-product shipping allowlists. NULL policy → ship anywhere.
    if not shipping_country_allowed(shipping.get("country", ""), policy):
        return JSONResponse(
            {"error": {"code": "unsupported_jurisdiction", "message": f"Cannot ship to {shipping.get('country')}."}},
            status_code=400,
        )
    if not shipping_state_allowed(shipping.get("state", ""), shipping.get("country", ""), policy):
        return JSONResponse(
            {"error": {"code": "unsupported_jurisdiction", "message": f"Cannot ship to {shipping.get('state')}."}},
            status_code=400,
        )

    # Per-product identity gate.
    enforcement = policy["enforcement"] if policy and "enforcement" in policy else None
    gate = build_gate_from_policy(policy, api_key=API_KEY)
    gate_result = await run_gate_with_enforcement(request, gate, enforcement=enforcement)

    if gate_result.status == "denied":
        # Hard mode: propagate the gate's structured 403 verbatim.
        return JSONResponse(content=gate_result.denial_body, status_code=gate_result.denial_status or 403)

    # gate_result.status is one of: "verified" (gate ran + passed),
    # "unverified" (soft mode swallowed a denial), "anonymous" (no gate fired).
    # Persist this on the order row so ops can distinguish soft passes from hard
    # passes and from no-gate-product orders. For the limited print, an
    # "unverified" status is a real fraud signal worth flagging in ops.
    identity_status = gate_result.status

    # ... settle payment, create order with `identity_status` column, return 200 ...
    return JSONResponse(
        {
            "order": {"product": product["name"], "total_usd": product["price_usd"]},
            "identity_status": identity_status,
        }
    )
