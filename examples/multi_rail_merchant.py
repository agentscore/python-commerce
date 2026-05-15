"""Example: full regulated-commerce merchant.

Scenario: you sell a regulated good. Identity gate (KYC + age + jurisdiction +
sanctions), plus a 402 payment challenge advertising multiple rails so agents
pay with whatever they have: Tempo USDC (MPP `tempo/charge`), x402 USDC on
Base, Solana USDC (MPP `solana/charge`), Stripe SPT.

`Checkout(...)` orchestrates the flow:

1. Identity gate runs only on the settle leg (a payment header is attached);
   the discovery leg flows through anonymously and gets a 402 with all rails.
2. `mint_recipients` hook calls into Stripe to mint per-PI deposit addresses
   for tempo/base/solana before the 402 emits, so the body advertises the
   right addresses.
3. `compute_pricing` returns the subtotal + tax block for the current cart.
4. x402-base header → Checkout dispatches to `process_x402_settle` internally.
5. `Authorization: Payment` header → Checkout dispatches to the auto-derived
   `compose_mppx` hook (built from `mppx_secret_key`).
6. `on_settled` persists the order + fires `simulate_deposit_if_test_mode`
   for Stripe testnet round-trip on base settles.

Peer deps::

    pip install 'agentscore-commerce[fastapi,x402,mppx,coinbase,stripe]'

Env vars:
    AGENTSCORE_API_KEY    your AgentScore API key
    APP_URL               public URL of your service
    STRIPE_SECRET_KEY     sk_test_... or sk_live_...
    STRIPE_PROFILE_ID     your Stripe Connect profile id (for SPT)
    X402_BASE_NETWORK     CAIP-2 (default eip155:8453)
    SOLANA_NETWORK_CAIP2  CAIP-2 (default solana mainnet)
    MPP_SECRET_KEY        secret_key for the auto-derived mppx server
    CDP_API_KEY_ID        Coinbase CDP key id (auto-promotes x402 facilitator)
    CDP_API_KEY_SECRET    Coinbase CDP key secret
    REDIS_URL             optional; in-memory PI cache otherwise

Run: uvicorn examples.multi_rail_merchant:app --port 3000
"""

import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce import (
    Checkout,
    CheckoutGateConfig,
    CheckoutValidationError,
    PricingResult,
    SettleOutcome,
    pricing_result,
)
from agentscore_commerce.challenge import ProductInfo, Receipt, ReceiptNextSteps
from agentscore_commerce.discovery import build_success_next_steps
from agentscore_commerce.payment import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
    networks,
    validate_x402_network_config,
)
from agentscore_commerce.stripe_multichain import (
    create_multichain_payment_intent,
    create_pi_cache,
    simulate_deposit_if_test_mode,
)

APP_URL = os.environ["APP_URL"]
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
X402_BASE_NETWORK = os.environ.get("X402_BASE_NETWORK", networks.base.mainnet.caip2)
SOLANA_NETWORK_CAIP2 = os.environ.get("SOLANA_NETWORK_CAIP2", networks.solana.mainnet.caip2)
validate_x402_network_config(base_network=X402_BASE_NETWORK)

# Singleton Stripe client + PI / deposit-address cache. Redis-backed when
# REDIS_URL is set (multi-task deployments need this so a deposit lands on
# whichever task settles it).
import stripe  # noqa: E402  optional peer dep installed by the example user

stripe_client = stripe.StripeClient(STRIPE_SECRET_KEY)
pi_cache = create_pi_cache(redis_url=os.environ.get("REDIS_URL"))

app = FastAPI()


async def _validate_purchase(ctx: Any) -> dict[str, Any]:
    """preValidate hook: shape-check the request body before pricing/gate runs."""
    body = ctx.request.body if isinstance(ctx.request.body, dict) else {}
    if "shipping" not in body:
        raise CheckoutValidationError(code="missing_shipping", message="`shipping` is required.")
    return {"shipping_state": body["shipping"].get("state", "CA")}


async def _compute_pricing(ctx: Any) -> PricingResult:
    return pricing_result(
        subtotal_cents=25000,  # $250.00; vendor pricing logic goes here.
        tax_cents=2000,
        tax_rate=0.08,
        tax_state=ctx.state.get("shipping_state", "CA"),
    )


async def _mint_recipients(ctx: Any) -> dict[str, str]:
    """Per-order recipient mint: Stripe multichain PI → per-network deposit addresses."""
    total_cents = round(ctx.pricing.amount_usd * 100)
    result = create_multichain_payment_intent(
        stripe=stripe_client,
        amount=total_cents,
        networks=["tempo", "base", "solana"],
    )
    for addr in result.deposit_addresses.values():
        await pi_cache.cache_address(addr)
        pi_cache.cache_payment_intent(addr, result.payment_intent_id)
    pi_cache.cache_network_addresses(result.payment_intent_id, result.deposit_addresses)
    return {
        "tempo": result.deposit_addresses["tempo"],
        "x402_base": result.deposit_addresses["base"],
        "solana_mpp": result.deposit_addresses["solana"],
    }


async def _on_settled(ctx: Any, outcome: SettleOutcome) -> dict[str, Any]:
    # Fire Stripe testnet deposit simulation on real on-chain base settles
    # (no-op on live keys). Gate on `tx_hash` so $0 zero-settle carve-outs
    # (which have signer_address but no tx_hash) don't trigger a PI sim.
    if outcome.rail == "x402" and outcome.tx_hash is not None:
        await simulate_deposit_if_test_mode(
            get_payment_intent_id=pi_cache.get_payment_intent_id,
            deposit_address=ctx.recipients.get("x402_base", ""),
            network="base",
            stripe_secret_key=STRIPE_SECRET_KEY,
        )
    # Compose the canonical Receipt shape returned on 200. Goods merchants
    # populate the goods-only slots (shipping, fulfillment_status, tracking)
    # at fulfillment time; this example wires the universal fields.
    receipt = Receipt(
        id=ctx.reference_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        pricing=ctx.pricing.block,
        product=ProductInfo(name="Regulated Goods Cart"),
        payment_status="completed",
        next_steps=ReceiptNextSteps(
            **build_success_next_steps(
                order_status_url=f"{APP_URL}/orders/{ctx.reference_id}",
            ),
        ),
        extras={
            "tx_hash": outcome.tx_hash,
            "identity_status": ctx.identity_status,
        },
    )
    return asdict(receipt)


checkout = Checkout(
    rails={
        # Per-order-mint pattern: empty-string `recipient` declares the rail
        # in discovery; `mint_recipients` resolves the real per-PI address.
        "tempo": TempoRailSpec(recipient=""),
        "x402_base": X402BaseRailSpec(recipient="", network=X402_BASE_NETWORK),
        "solana_mpp": SolanaMppRailSpec(recipient="", network=SOLANA_NETWORK_CAIP2),
        "stripe": StripeRailSpec(profile_id=os.environ["STRIPE_PROFILE_ID"]),
    },
    url=f"{APP_URL}/purchase",
    pre_validate=_validate_purchase,
    compute_pricing=_compute_pricing,
    mint_recipients=_mint_recipients,
    on_settled=_on_settled,
    is_cached_address=pi_cache.has_address,
    cdp_api_key_id=os.environ.get("CDP_API_KEY_ID"),
    cdp_api_key_secret=os.environ.get("CDP_API_KEY_SECRET"),
    mppx_secret_key=os.environ.get("MPP_SECRET_KEY"),
    gate=CheckoutGateConfig(
        api_key=os.environ["AGENTSCORE_API_KEY"],
        merchant_name="Regulated Goods Co.",
        require_kyc=True,
        require_sanctions_clear=True,
        min_age=21,
        allowed_jurisdictions=["US"],
    ),
)


@app.post("/purchase")
async def purchase(request: Request) -> JSONResponse:
    return await checkout.handle_fastapi(request)
