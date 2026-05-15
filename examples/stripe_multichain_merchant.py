"""Example: Stripe-anchored multichain merchant

Scenario: you want crypto payments but you're already a Stripe merchant. Use Stripe's
``deposit_options`` to issue per-PI deposit addresses on multiple chains (Tempo, Base,
Solana). Agent picks a chain and sends USDC to the matching address; Stripe auto-captures
when funds land. Net: one Stripe PI per purchase, multi-chain optionality, settlement
tracked in Stripe.

Distinct from Stripe SPT (Shared Payment Token), which is for user-approved cards via
the ``link-cli`` flow. This example is the "merchant funds via crypto rails" path.

Peer deps:
    pip install 'agentscore-commerce[fastapi,stripe]'

Env vars:
    STRIPE_SECRET_KEY — sk_live_... or sk_test_...

Run: uvicorn examples.stripe_multichain_merchant:app --port 3000
"""

import os

import stripe
from fastapi import FastAPI

from agentscore_commerce.stripe_multichain import (
    STRIPE_TEST_TX_HASH_SUCCESS,
    create_multichain_payment_intent,
    simulate_crypto_deposit,
)

stripe_client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])

app = FastAPI()


@app.post("/checkout")
async def checkout(body: dict) -> dict:
    amount_cents = round(float(body["amount_usd"]) * 100)

    # 1. Create a Stripe PI with deposit addresses on tempo + base + solana.
    result = create_multichain_payment_intent(
        stripe=stripe_client,
        amount=amount_cents,
        networks=["tempo", "base", "solana"],
        metadata={"order_id": body.get("order_id"), "merchant": "example-store"},
        idempotency_key=f"pi-{body['order_id']}-{amount_cents}" if body.get("order_id") else None,
    )

    # 2. Return per-network deposit addresses to the agent (or 402 with
    # addresses embedded — see multi_rail_merchant.py for the full 402-builder
    # pattern).
    amount_usd = body["amount_usd"]
    tempo = result.deposit_addresses.get("tempo")
    base = result.deposit_addresses.get("base")
    solana = result.deposit_addresses.get("solana")
    return {
        "payment_intent_id": result.payment_intent_id,
        "deposit_addresses": result.deposit_addresses,
        "instructions": {
            "tempo": (f"Send {amount_usd} USDC on Tempo to {tempo}" if tempo else "Tempo not available for this PI"),
            "base": (f"Send {amount_usd} USDC on Base to {base}" if base else "Base not available for this PI"),
            "solana": (
                f"Send {amount_usd} USDC on Solana to {solana}" if solana else "Solana not available for this PI"
            ),
        },
    }


# ── Testnet helper: simulate a deposit landing on a PI ──────────────────────
# Useful for end-to-end testing without real on-chain transfers. For the
# typical "fire after PI mint if sk_test_" pattern, prefer
# `simulate_deposit_if_test_mode` which gates internally — see
# multi_rail_merchant.py.
@app.post("/testnet/simulate-deposit")
async def simulate_deposit(body: dict) -> dict:
    await simulate_crypto_deposit(
        payment_intent_id=body["payment_intent_id"],
        network=body["network"],
        stripe_secret_key=os.environ["STRIPE_SECRET_KEY"],
        stripe_version="2026-03-04.preview",  # if you're on a preview API
        token_currency="usdc",
        transaction_hash=STRIPE_TEST_TX_HASH_SUCCESS,
    )
    return {"ok": True, "simulated": True}
