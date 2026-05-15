"""Example: variable-cost merchant supporting BOTH x402 upto AND MPP tempo session.

Scenario: you sell something where the cost depends on output (LLM completions,
transcription, video transcode, etc.). You don't know the final price until the
work is done. Two protocols solve this; both are advertised on the 402 so
agents can pick whichever they support.

x402 upto (one-shot)
    * Agent signs Permit2 authorizing up to a max amount.
    * Vendor does the work, knows actual cost after.
    * Response sets ``Settlement-Overrides: {"amount":"<actual>"}``.
    * Facilitator settles for actual; difference auto-refunds.

MPP tempo session (streaming)
    * Agent opens a channel with on-chain deposit.
    * Vendor streams output as SSE.
    * Cumulative cost grows; vendor emits voucher requests.
    * Agent signs each voucher mid-stream.
    * Final settle on close reclaims unspent deposit.

These flows are too custom to fit the one-shot ``Checkout(...)`` model:
``compute_pricing`` returns a single amount, but variable-cost discovers the
amount AFTER the request runs (upto) or grows it cumulatively (session). The
example keeps the 402-emit path custom (using ``build_402_body`` +
``build_accepted_methods`` + ``build_how_to_pay``) and the settle path manual;
vendors compose ``create_x402_server`` + Permit2 extensions or
``create_mppx_server`` (TempoSessionRailSpec) at the vendor layer.

Peer deps:
    pip install 'agentscore-commerce[fastapi,x402,mppx,coinbase]'

Env vars:
    APP_URL               public URL of your service
    MPP_SECRET_KEY        random base64
    TEMPO_RECIPIENT       your Tempo wallet
    TEMPO_ESCROW          your deployed escrow contract for channel deposits
    X402_BASE_RECIPIENT   your Base wallet (USDC payouts for upto rail)

Run: uvicorn examples.variable_cost_merchant:app --port 3000
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce.challenge import (
    build_402_body,
    build_accepted_methods,
    build_agent_instructions,
    build_how_to_pay,
    build_pricing_block,
)
from agentscore_commerce.payment import (
    TempoRailSpec,
    X402BaseRailSpec,
    create_mppx_server,
    create_x402_server,
    payment_directive,
    payment_required_header,
    settlement_override_header,
    www_authenticate_header,
)

APP_URL = os.environ.get("APP_URL", "http://localhost:3000")
TEMPO_RECIPIENT = os.environ.get("TEMPO_RECIPIENT", "0xfeedface")
X402_BASE_RECIPIENT = os.environ.get("X402_BASE_RECIPIENT", "0xfeedface")
MPP_SECRET_KEY = os.environ.get("MPP_SECRET_KEY", "")
TEMPO_ESCROW = os.environ.get("TEMPO_ESCROW", "")

REALM = urlparse(APP_URL).hostname or "llm.example.com"
MAX_USDC = 0.5  # upper bound vendor advertises; actual bill <= this.
MAX_USDC_CENTS = round(MAX_USDC * 100)

app = FastAPI()


# Boot the x402 server for the Permit2 (upto) rail. The MPP server boot
# parallel is sketched below — pympp doesn't yet ship a Python-native session
# implementation, so the SSE handler returns 501 with the wire-shape sketched.
async def _boot_x402_server() -> Any:
    return await create_x402_server(facilitator="http", rails=["x402-base-mainnet-upto"])


async def _build_402_body(url: str) -> tuple[dict[str, Any], dict[str, str]]:
    challenge_id = f"chg_{int(asyncio.get_event_loop().time() * 1000)}"
    directives = [
        payment_directive(rail="x402-base-mainnet-upto", id=f"{challenge_id}_upto", realm=REALM, request=""),
        payment_directive(
            rail="tempo-mainnet",
            id=f"{challenge_id}_session",
            realm=REALM,
            intent="session",
            request="",
        ),
    ]

    x402_spec = X402BaseRailSpec(recipient=X402_BASE_RECIPIENT)
    tempo_spec = TempoRailSpec(recipient=TEMPO_RECIPIENT)
    accepted = await build_accepted_methods(x402_base=x402_spec, tempo=tempo_spec)
    how_to_pay = await build_how_to_pay(
        url=url,
        retry_body_json='{"prompt":"<your prompt>"}',
        total_usd=f"{MAX_USDC:.2f}",
        rails={"x402_base": x402_spec, "tempo": tempo_spec},
        max_spend=MAX_USDC,
    )
    instructions = build_agent_instructions(
        how_to_pay=how_to_pay,
        warnings=[
            "Cost is variable; final amount depends on output length.",
            "For one-shot completions use x402 upto. For long streams use tempo session.",
        ],
    )

    # For variable-cost work, advertise the upper bound as `subtotal` and let
    # the vendor charge <= that. The actual amount lands via
    # Settlement-Overrides (x402 upto) or the highest voucher signed mid-stream
    # (tempo session).
    body = build_402_body(
        product={"id": "llm-completion", "name": "LLM completion"},
        accepted_methods=accepted,
        pricing=build_pricing_block(subtotal_cents=MAX_USDC_CENTS, currency="USD"),
        agent_instructions=instructions,
        amount_usd=f"{MAX_USDC:.2f}",
        currency="USD",
        retry_body={"prompt": "<your prompt>"},
    )
    headers = {
        "www-authenticate": www_authenticate_header(directives),
        # x402 wire requires the body to also appear as base64 in this header;
        # spec-strict clients (Coinbase awal, purl) parse it before falling
        # back to the JSON body.
        "PAYMENT-REQUIRED": payment_required_header(x402_version=2, accepts=[], resource={"url": url}),
    }
    return body, headers


async def _run_your_llm(_prompt: str) -> tuple[str, int]:
    return "completion text here", 1234


@app.post("/llm/complete")
async def complete(request: Request) -> JSONResponse:
    """x402 upto path: single JSON response with Settlement-Overrides."""
    # x402 carries the credential in either `x-payment` or `payment-signature`
    # depending on client (purl uses payment-signature; awal uses x-payment).
    if not (request.headers.get("x-payment") or request.headers.get("payment-signature")):
        body, headers = await _build_402_body(str(request.url))
        return JSONResponse(body, status_code=402, headers=headers)

    body = await request.json()
    text, tokens_used = await _run_your_llm(body.get("prompt", ""))

    actual_usd = tokens_used * 0.000_002  # $2 per 1M tokens
    actual_atomic = str(int(actual_usd * 1_000_000))  # USDC atomic units

    # Tell the facilitator to settle for `actual_atomic` instead of the
    # authorized max. The Permit2 layer auto-refunds the difference.
    name, value = settlement_override_header(amount=actual_atomic)
    return JSONResponse(
        {"text": text, "tokens_used": tokens_used, "charged_usd": actual_usd},
        headers={name: value},
    )


@app.post("/llm/stream")
async def stream(request: Request) -> JSONResponse:
    """MPP tempo session path: agent opens channel, server streams SSE with mid-stream vouchers.

    Production wiring: ``mpp = await create_mppx_server(secret_key=MPP_SECRET_KEY, rails={
    "tempo_session": TempoSessionRailSpec(recipient=TEMPO_RECIPIENT,
    escrow_contract=TEMPO_ESCROW, store=YourChannelStore())})``; parse channel state
    from ``Authorization: Payment``, emit SSE chunks, request fresh voucher signatures
    as cumulative cost grows, close channel on completion.
    """
    if not request.headers.get("authorization"):
        body, headers = await _build_402_body(str(request.url))
        return JSONResponse(body, status_code=402, headers=headers)
    return JSONResponse({"error": "stream-not-implemented"}, status_code=501)


# Hold a reference to the boot coro so the linter doesn't drop the import.
_X402_SERVER_BOOT = _boot_x402_server
_ = create_mppx_server  # exported for vendors wiring session rails
