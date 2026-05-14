"""Example: full regulated-commerce merchant.

Scenario: you sell a regulated good. Identity gate (KYC + age + jurisdiction + sanctions),
plus 402 payment challenge advertising multiple rails so agents can pay with whatever they
have: Tempo USDC (MPP `tempo/charge`), x402 USDC on Base, Solana USDC (MPP `solana/charge`),
Stripe SPT.

The flow on each /purchase POST:
    1. Identity gate (AgentScoreGate): KYC + age + jurisdiction + sanctions
    2. If ``X-Payment`` header present (x402 client paying base) → ``verify_x402_request`` →
       ``process_x402_settle`` → return 200 with ``payment-response`` header
    3. Else mint a Stripe multichain PI (deposit addresses for tempo/base/solana)
       and run pympp's compose() to validate any ``Authorization: Payment`` header
       (covers tempo/charge AND solana/charge directives)
    4. If pympp returns 402 → ``respond_402`` (preserves pympp's WWW-Auth + adds x402's
       PAYMENT-REQUIRED) with the rich body
    5. If pympp returns 200 → also fire ``simulate_deposit_if_test_mode`` for testnet

Peer deps::

    pip install agentscore-commerce[fastapi,x402,pympp]

Env vars:
    AGENTSCORE_API_KEY    — your AgentScore API key
    APP_URL               — public URL of your service
    STRIPE_SECRET_KEY     — sk_test_... or sk_live_...
    STRIPE_PROFILE_ID     — your Stripe Connect profile id (for SPT)
    TEMPO_USDC_ADDRESS    — USDC token address on Tempo (mainnet or testnet)
    X402_BASE_NETWORK     — CAIP-2
    SOLANA_NETWORK_CAIP2      — CAIP-2
    REDIS_URL             — optional; in-memory PI cache otherwise

Run: uvicorn examples.multi_rail_merchant:app --port 3000
"""

import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce.challenge import (
    build_402_body,
    build_accepted_methods,
    build_agent_instructions,
    build_how_to_pay,
    build_pricing_block,
    build_validation_error,
    first_encounter_agent_memory,
    respond_402,
)
from agentscore_commerce.identity.fastapi import AgentScoreGate, get_agentscore_data
from agentscore_commerce.payment import (
    USDC,
    build_x402_accepts_for_402,
    networks,
    process_x402_settle,
    validate_x402_network_config,
    verify_x402_request,
)
from agentscore_commerce.stripe_multichain import (
    create_pi_cache,
    simulate_deposit_if_test_mode,
)

APP_URL = os.environ["APP_URL"]
X402_BASE_NETWORK = os.environ.get("X402_BASE_NETWORK", networks.base.mainnet.caip2)
SOLANA_NETWORK_CAIP2 = os.environ.get("SOLANA_NETWORK_CAIP2", networks.solana.mainnet.caip2)

# Boot-time guard: validate the configured x402 networks are in the supported set.
# Raises on misconfigured deploys before the first request.
validate_x402_network_config(base_network=X402_BASE_NETWORK)

# Singleton Stripe PI / deposit-address cache. Backed by Redis when REDIS_URL is set
# (multi-instance deployments need this so a deposit lands on whichever instance
# settles it); falls back to in-process dict for single-instance dev.
pi_cache = create_pi_cache(redis_url=os.environ.get("REDIS_URL"))

app = FastAPI()
_gate = AgentScoreGate(
    api_key=os.environ["AGENTSCORE_API_KEY"],
    require_kyc=True,
    require_sanctions_clear=True,
    min_age=21,
    allowed_jurisdictions=["US"],
)


# Conditional gate: fires only when a payment credential is already attached. Anonymous
# requests (no payment header) fall through to the handler unauthenticated and receive
# a clean 402 with all rails advertised — so any spec-compliant x402 wallet (Coinbase
# awal, Phantom, Solflare, etc.) can discover prices before AgentScore identity exists.
# Identity is verified at settle time (when X-Payment / Authorization: Payment arrives),
# and `create_session_on_missing` then auto-mints a verification session.
async def gate_on_settle(request: Request) -> None:
    has_payment_header = bool(
        request.headers.get("payment-signature")
        or request.headers.get("x-payment")
        or (request.headers.get("authorization") or "").startswith("Payment ")
    )
    if not has_payment_header:
        return None
    return await _gate(request)


# Vendor-instantiated x402 server + pympp server are stubs in this example —
# replace with your `create_x402_server(...)` + `create_mppx_server(...)` setup.
x402_server: object = ...  # type: ignore[assignment]


@app.post("/purchase", dependencies=[Depends(gate_on_settle)])
async def purchase(request: Request, assess: dict = Depends(get_agentscore_data)):
    body = await request.json()

    # Compute pricing (vendor-specific — wine tax by state, dynamic SKU pricing, etc.)
    subtotal_cents = 25000  # $250.00
    tax_cents = 2000
    total_cents = subtotal_cents + tax_cents
    total_usd = f"{total_cents / 100:.2f}"
    pricing = build_pricing_block(
        subtotal_cents=subtotal_cents,
        tax_cents=tax_cents,
        tax_rate=0.08,
        tax_state=body.get("shipping", {}).get("state", "CA"),
        currency="USD",
    )

    # ──────────────────────────────────────────────────────────────────────────
    # Path A: x402 X-Payment header present → verify + settle on chain
    # ──────────────────────────────────────────────────────────────────────────
    if request.headers.get("payment-signature") or request.headers.get("x-payment"):
        verified = await verify_x402_request(
            headers=dict(request.headers),
            is_cached_address=pi_cache.has_address,
            accepted_network=X402_BASE_NETWORK,
        )
        if not verified.ok:
            return JSONResponse(verified.body, status_code=verified.status)

        settle = await process_x402_settle(
            x402_server=x402_server,
            payload=verified.payload,
            resource_config={
                "scheme": "exact",
                "network": verified.signed_network,
                "price": f"${total_usd}",
                "payTo": verified.signed_pay_to,
                "maxTimeoutSeconds": 300,
            },
            resource_meta={
                "url": str(request.url),
                "description": "Agent purchase via x402",
                "mimeType": "application/json",
            },
        )
        if not settle.success:
            return JSONResponse(
                build_validation_error(
                    code="payment_proof_invalid",
                    message=f"Payment failed during settlement (phase: {settle.phase or 'unknown'}).",
                    next_steps={"action": "regenerate_payment_credential"},
                    extra={"phase": settle.phase},
                ),
                status_code=400,
            )

        # Fire Stripe testnet sim; no-ops on live keys. x402 settle only ever
        # lands on base in 1.4+ (Solana moved to MPP `solana/charge`).
        await simulate_deposit_if_test_mode(
            get_payment_intent_id=pi_cache.get_payment_intent_id,
            deposit_address=verified.signed_pay_to,
            network="base",
            stripe_secret_key=os.environ["STRIPE_SECRET_KEY"],
        )

        headers: dict[str, str] = {}
        if settle.payment_response_header:
            headers["payment-response"] = settle.payment_response_header
        return JSONResponse({"ok": True, "operator": assess.get("resolved_operator")}, headers=headers)

    # ──────────────────────────────────────────────────────────────────────────
    # Path B: cold call OR Authorization: Payment (pympp) — mint PI + compose pympp
    # ──────────────────────────────────────────────────────────────────────────
    # ... your createMultichainPaymentIntent + cache writeback here ...
    # ... your pympp.compose() to validate Authorization: Payment header ...
    # If pympp returns 402, build the rich 402 with respond_402 (preserves pympp's
    # WWW-Auth + adds x402's PAYMENT-REQUIRED):
    pympx_challenge_headers = {"www-authenticate": 'Payment id="..."'}  # from pympp.compose
    deposit_addresses = {"tempo": "0x...", "base": "0x...", "solana": "..."}  # from create_multichain_payment_intent
    accepted = build_accepted_methods(
        tempo={"recipient": deposit_addresses["tempo"]},
        x402_base={"recipient": deposit_addresses["base"]},
        solana_mpp={"recipient": deposit_addresses["solana"]},
        stripe={"profile_id": os.environ["STRIPE_PROFILE_ID"]},
    )
    how_to_pay = build_how_to_pay(
        url=APP_URL,
        retry_body_json=str(body),
        total_usd=total_usd,
        rails={
            "tempo": {"recipient": deposit_addresses["tempo"]},
            "x402_base": {"recipient": deposit_addresses["base"]},
            "solana_mpp": {"recipient": deposit_addresses["solana"]},
            "stripe": {"profile_id": os.environ["STRIPE_PROFILE_ID"]},
        },
    )

    result = respond_402(
        mppx_challenge_headers=pympx_challenge_headers,
        body=build_402_body(
            accepted_methods=accepted,
            agent_instructions=build_agent_instructions(how_to_pay=how_to_pay),
            pricing=pricing,
            amount_usd=total_usd,
            retry_body=body,
            # Production merchants track first-encounter state in their own DB;
            # for demo purposes we always emit the cross-merchant pattern hint.
            agent_memory=first_encounter_agent_memory(first_encounter=True),
        ),
        x402={
            "x402_version": 2,
            # Base accept comes from the registered x402 scheme — `extra` (incl. the
            # network-correct USDC `name`) is filled in automatically. Solana goes
            # through MPP `solana/charge` not x402's exact scheme, so it stays inline.
            "accepts": [
                *build_x402_accepts_for_402(
                    x402_server,
                    network=X402_BASE_NETWORK,
                    price=f"${total_usd}",
                    pay_to=deposit_addresses["base"],
                    max_timeout_seconds=300,
                ),
                {
                    "scheme": "exact",
                    "network": SOLANA_NETWORK_CAIP2,
                    "amount": str(round(float(total_usd) * 1_000_000)),
                    "asset": (
                        USDC.solana.devnet.mint
                        if networks.solana.devnet.caip2 == SOLANA_NETWORK_CAIP2
                        else USDC.solana.mainnet.mint
                    ),
                    "payTo": deposit_addresses["solana"],
                    "maxTimeoutSeconds": 300,
                    # SVM transactions require feePayer in extra. Default to
                    # the recipient (round-trip safe for dev). Production
                    # merchants typically point at the Coinbase facilitator's
                    # payer address.
                    "extra": {"feePayer": deposit_addresses["solana"]},
                },
            ],
            "resource": {"url": str(request.url), "mimeType": "application/json"},
        },
    )
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)
