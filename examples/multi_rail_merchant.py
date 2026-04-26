"""Example: full agent-commerce merchant (Martin-Estate-style stripped down)

Scenario: you sell a regulated good. Identity gate (KYC + age + jurisdiction + sanctions),
plus 402 payment challenge advertising multiple rails so agents can pay with whatever they
have — Tempo USDC (MPP), x402 USDC on Base + Solana, Stripe SPT.

Peer deps:
    pip install agentscore-commerce[fastapi,x402,mppx]

Env vars:
    AGENTSCORE_API_KEY    — your AgentScore API key
    TEMPO_RECIPIENT       — Tempo wallet for receiving USDC.e
    X402_BASE_RECIPIENT   — Base wallet for receiving USDC
    X402_SOLANA_RECIPIENT — Solana wallet for receiving USDC
    STRIPE_PROFILE_ID     — your Stripe profile id (for SPT)

Run: uvicorn examples.multi_rail_merchant:app --port 3000
"""

import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce.challenge import (
    Build402BodyInput,
    BuildAcceptedMethodsInput,
    BuildAgentInstructionsInput,
    BuildHowToPayInput,
    HowToPayRails,
    StripeConfig,
    StripeRailConfig,
    TempoConfig,
    TempoRailConfig,
    X402BaseConfig,
    X402BaseRailConfig,
    X402SolanaConfig,
    X402SolanaRailConfig,
    build_402_body,
    build_accepted_methods,
    build_agent_instructions,
    build_how_to_pay,
    build_pricing_block,
    first_encounter_agent_memory,
)
from agentscore_commerce.identity.fastapi import AgentScoreGate, get_assess_data
from agentscore_commerce.payment import (
    BuildPaymentHeadersInput,
    PaymentHeadersRail,
    build_payment_headers,
)

APP_URL = "https://my-merchant.example.com/purchase"

app = FastAPI()
gate = AgentScoreGate(
    api_key=os.environ["AGENTSCORE_API_KEY"],
    require_kyc=True,
    require_sanctions_clear=True,
    min_age=21,
    allowed_jurisdictions=["US"],
)


@app.post("/purchase", dependencies=[Depends(gate)])
async def purchase(request: Request, assess: dict = Depends(get_assess_data)):
    body = await request.json()

    # Compute pricing (vendor-specific — wine tax by state, dynamic SKU pricing, etc.)
    subtotal_cents = 25000  # $250.00
    tax_cents = 2000
    total_cents = subtotal_cents + tax_cents
    total_usd = f"{total_cents / 100:.2f}"
    pricing = build_pricing_block(
        subtotal_cents=subtotal_cents,
        tax_cents=tax_cents,
        # Pass shipping_cents=0 for digital goods if you want the field present;
        # omit entirely (default) if your merchant has no shipping concept at all.
        tax_rate=0.08,
        tax_state=body.get("shipping", {}).get("state", "CA"),
        currency="USD",
    )

    # Payment present? Validate + settle against facilitator HTTP, run the order, return 200.
    if request.headers.get("x-payment") or (request.headers.get("authorization", "").startswith("Payment ")):
        # ... your facilitator-validate + settle + insert order ...
        return {"status": "completed", "operator": assess.get("resolved_operator")}

    # No payment yet — return 402 with multi-rail challenge.
    accepted = build_accepted_methods(
        BuildAcceptedMethodsInput(
            tempo=TempoConfig(recipient=os.environ["TEMPO_RECIPIENT"]),
            x402_base=X402BaseConfig(recipient=os.environ["X402_BASE_RECIPIENT"]),
            x402_solana=X402SolanaConfig(recipient=os.environ["X402_SOLANA_RECIPIENT"]),
            stripe=StripeConfig(profile_id=os.environ["STRIPE_PROFILE_ID"]),
        )
    )
    how_to_pay = build_how_to_pay(
        BuildHowToPayInput(
            url=APP_URL,
            retry_body_json=str(body),
            total_usd=total_usd,
            rails=HowToPayRails(
                tempo=TempoRailConfig(recipient=os.environ["TEMPO_RECIPIENT"]),
                x402_base=X402BaseRailConfig(recipient=os.environ["X402_BASE_RECIPIENT"]),
                x402_solana=X402SolanaRailConfig(recipient=os.environ["X402_SOLANA_RECIPIENT"]),
                stripe=StripeRailConfig(profile_id=os.environ["STRIPE_PROFILE_ID"]),
            ),
        )
    )
    # One-call header bundle: composes WWW-Authenticate + PAYMENT-REQUIRED from a
    # single rails declaration. Replaces ~10 lines of inline directive construction.
    headers = build_payment_headers(
        BuildPaymentHeadersInput(
            order_id="chg",
            realm="my-merchant.example.com",
            rails=[
                PaymentHeadersRail(
                    rail="tempo-mainnet",
                    amount_usd=total_usd,
                    recipient=os.environ["TEMPO_RECIPIENT"],
                    method="tempo",
                ),
                PaymentHeadersRail(
                    rail="x402-base-mainnet",
                    amount_usd=total_usd,
                    recipient=os.environ["X402_BASE_RECIPIENT"],
                ),
                PaymentHeadersRail(
                    rail="x402-solana-mainnet",
                    amount_usd=total_usd,
                    recipient=os.environ["X402_SOLANA_RECIPIENT"],
                ),
                PaymentHeadersRail(
                    rail="stripe",
                    amount_usd=total_usd,
                    network_id=os.environ["STRIPE_PROFILE_ID"],
                    method="stripe",
                ),
            ],
        ),
    )
    return JSONResponse(
        build_402_body(
            Build402BodyInput(
                accepted_methods=accepted,
                agent_instructions=build_agent_instructions(
                    BuildAgentInstructionsInput(how_to_pay=how_to_pay),
                ),
                pricing=pricing,
                amount_usd=total_usd,
                # Production merchants track first-encounter state in their own DB;
                # for demo purposes we always emit the cross-merchant pattern hint.
                agent_memory=first_encounter_agent_memory(first_encounter=True),
            ),
        ),
        status_code=402,
        headers={"www-authenticate": headers["www_authenticate"]},
    )
