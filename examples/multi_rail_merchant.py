"""Example: full agent-commerce merchant (Martin-Estate-style stripped down)

Scenario: you sell a regulated good. Identity gate (KYC + age + jurisdiction + sanctions),
plus 402 payment challenge advertising multiple rails so agents can pay with whatever they
have — Tempo USDC (MPP), x402 USDC on Base + Solana, Stripe SPT.

Python doesn't have `@x402/core` / `mppx` peer deps, so payment verification + settlement
runs against the facilitator HTTP API. Commerce helpers build the protocol-correct 402 body +
headers; your route does the post-payment settlement against the facilitator.

Peer deps:
    pip install agentscore-commerce[fastapi]

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
    PricingBlock,
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
)
from agentscore_commerce.identity.fastapi import AgentScoreGate, get_assess_data
from agentscore_commerce.payment import (
    PaymentDirectiveInput,
    payment_directive,
    www_authenticate_header,
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
    pricing = PricingBlock(
        subtotal=f"{subtotal_cents / 100:.2f}",
        tax=f"{tax_cents / 100:.2f}",
        tax_rate=0.08,
        tax_state=body.get("shipping", {}).get("state", "CA"),
        total=total_usd,
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
    directives = [
        payment_directive(
            PaymentDirectiveInput(rail="tempo-mainnet", id="chg_tempo", realm="my-merchant.example.com", request="")
        ),
        payment_directive(
            PaymentDirectiveInput(rail="x402-base-mainnet", id="chg_base", realm="my-merchant.example.com", request="")
        ),
    ]
    return JSONResponse(
        build_402_body(
            Build402BodyInput(
                accepted_methods=accepted,
                agent_instructions=build_agent_instructions(
                    BuildAgentInstructionsInput(how_to_pay=how_to_pay, recommended="tempo")
                ),
                pricing=pricing,
                amount_usd=total_usd,
            )
        ),
        status_code=402,
        headers={"www-authenticate": www_authenticate_header(directives)},
    )
