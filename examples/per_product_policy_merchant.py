"""Example: multi-product merchant with per-product compliance policy + soft mode.

Scenario: you sell several products with different compliance needs.

* Wine: hard gate, KYC + 21+ + US-only + state allowlist (regulated alcohol)
* Tee:  no gate at all; fully anonymous, ship anywhere
* Limited print: SOFT gate; request KYC for fraud signals, but don't block the
  sale if the buyer skips it; record `identity_status="unverified"` instead.

Each product carries its own `PolicyBlock`. `Checkout(gate=CheckoutGateConfig(
per_request_policy=...))` resolves it per request:

1. `pre_validate` looks up the product row by slug and stashes the policy block
   onto `ctx.state` for downstream hooks.
2. `per_request_policy(ctx)` returns the merged policy dict (including
   `enforcement: "hard"|"soft"|None`) — the SDK gate runs hard/soft based on
   the field.
3. Soft denials are swallowed by the SDK and stamp
   `identity_status="unverified"` onto the order; hard denials propagate the
   canonical 403 envelope.

Peer deps:
    pip install 'agentscore-commerce[fastapi]'

Env vars:
    AGENTSCORE_API_KEY — your AgentScore API key

Run: uvicorn examples.per_product_policy_merchant:app --port 3000
"""

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce import (
    Checkout,
    CheckoutGateConfig,
    CheckoutValidationError,
    PolicyBlock,
    PricingResult,
    SettleOutcome,
    TempoRailSpec,
    validate_shipping_against_policy,
)
from agentscore_commerce.middleware.fastapi import RateLimitMiddleware

API_KEY = os.environ.get("AGENTSCORE_API_KEY", "ask_test_dummy")

# A merchant would normally read these from a `products` table.
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
            allowed_shipping_states=["CA", "NY", "TX", "FL", "WA"],
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


async def _validate_purchase(ctx: Any) -> dict[str, Any]:
    body = ctx.request.body if isinstance(ctx.request.body, dict) else {}
    slug = body.get("product_slug")
    shipping = body.get("shipping", {})

    product = PRODUCTS.get(slug or "")
    if product is None:
        raise CheckoutValidationError(code="product_not_found", message=f"No product with slug {slug!r}.")

    policy = product["policy"]
    validate_shipping_against_policy(
        country=shipping.get("country", ""),
        state=shipping.get("state", ""),
        policy=policy,
        product_name=product["name"],
    )
    return {"product": product, "policy": policy}


async def _compute_pricing(ctx: Any) -> PricingResult:
    product = ctx.state["product"]
    return PricingResult(amount_usd=float(product["price_usd"]))


def _per_request_policy(ctx: Any) -> dict[str, Any] | None:
    policy = ctx.state.get("policy")
    if policy is None:
        return None  # Skip the gate entirely for no-policy products (anonymous).
    # The SDK gate reads `enforcement` to switch hard/soft mode. `PolicyBlock`
    # already carries the field; spread it through verbatim.
    return dict(policy)


async def _on_settled(ctx: Any, outcome: SettleOutcome) -> dict[str, Any]:
    product = ctx.state["product"]
    return {
        "order": {"product": product["name"], "total_usd": product["price_usd"]},
        "identity_status": ctx.identity_status,
        "tx_hash": outcome.tx_hash,
    }


checkout = Checkout(
    # Minimal rails so the 402 emit path has something to advertise; vendor
    # swaps in their real rails (multi-rail, Stripe-anchored, etc.).
    rails={"tempo": TempoRailSpec(recipient=os.environ.get("TEMPO_RECIPIENT", "0xfeedface"))},
    url="https://api.example.com/purchase",
    pre_validate=_validate_purchase,
    compute_pricing=_compute_pricing,
    on_settled=_on_settled,
    gate=CheckoutGateConfig(
        api_key=API_KEY,
        merchant_name="Multi-Product Co.",
        per_request_policy=_per_request_policy,
    ),
)


app = FastAPI()
app.add_middleware(RateLimitMiddleware)


@app.post("/purchase")
async def purchase(request: Request) -> JSONResponse:
    return await checkout.handle_fastapi(request)
