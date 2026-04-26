"""Example: Stripe-anchored multichain merchant

Scenario: you want to accept agent payments but settle through Stripe so all your existing
billing/refund/dashboard infrastructure keeps working. Stripe issues a single PaymentIntent
with deposit_options for tempo/base/solana — the agent picks any chain to send USDC, and
Stripe auto-captures the PI when the deposit lands.

Distinct from the Stripe SPT (Shared Payment Token) flow — this is the "agent sends crypto,
Stripe handles settlement on your behalf" path.

Peer deps:
    pip install agentscore-commerce[fastapi,stripe]

Env vars:
    STRIPE_SECRET_KEY — your sk_... secret key (sk_test_ for testnet)

Run: uvicorn examples.stripe_multichain_merchant:app --port 3000
"""

import os

import stripe
from fastapi import FastAPI

from agentscore_commerce.stripe_multichain import (
    CreateMultichainPaymentIntentInput,
    SimulateCryptoDepositInput,
    create_multichain_payment_intent,
    get_deposit_address,
    simulate_crypto_deposit,
)

stripe_client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])

app = FastAPI()


@app.post("/buy")
async def buy(body: dict):
    # Create a multichain PaymentIntent — Stripe issues deposit addresses for each requested chain.
    result = create_multichain_payment_intent(
        CreateMultichainPaymentIntentInput(
            stripe=stripe_client,
            amount=body.get("amount_cents", 25000),
            networks=["tempo", "base", "solana"],
            metadata={"order_id": body.get("order_id", "ord_demo"), "merchant": "example"},
            idempotency_key=body.get("order_id"),
        )
    )

    base_addr = get_deposit_address(result, "base")
    tempo_addr = get_deposit_address(result, "tempo")

    # On testnet you can simulate a deposit so end-to-end tests don't need real on-chain transfers.
    if os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test_"):
        await simulate_crypto_deposit(
            SimulateCryptoDepositInput(
                payment_intent_id=result.payment_intent_id,
                network="base",
                stripe_secret_key=os.environ["STRIPE_SECRET_KEY"],
                token_currency="usdc",
            )
        )

    return {
        "payment_intent_id": result.payment_intent_id,
        "deposit_addresses": result.deposit_addresses,
        "pay_to": {"base": base_addr, "tempo": tempo_addr},
    }
