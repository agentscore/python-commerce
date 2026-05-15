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

These flows are too custom to fit the one-shot `Checkout(...)` model:
`compute_pricing` returns a single amount, but variable-cost discovers the
amount AFTER the request runs (upto) or grows it cumulatively (session). The
example keeps the 402-emit body custom (the warnings + dynamic `max_usd` block
aren't in the canonical 402 schema) and the settle path manual; vendors
compose `create_x402_server` + Permit2 extensions or `create_mppx_server`
(TempoSessionRailSpec) at the vendor layer.

Peer deps:
    pip install 'agentscore-commerce[fastapi,x402,mppx,coinbase]'

Env vars:
    X402_BASE_RECIPIENT — your Base wallet (USDC payouts for upto rail)
    TEMPO_RECIPIENT     — your Tempo wallet
    TEMPO_ESCROW        — your deployed escrow contract for channel deposits

Run: uvicorn examples.variable_cost_merchant:app --port 3000
"""

import json
from base64 import b64encode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce.payment import (
    payment_directive,
    settlement_override_header,
    www_authenticate_header,
)

REALM = "llm.example.com"
MAX_USDC = 0.5  # upper bound advertised; actual bill <= this.

app = FastAPI()


def _build_402_body(url: str) -> tuple[dict, dict]:
    directives = [
        payment_directive(rail="x402-base-mainnet-upto", id="chg_upto", realm=REALM, request=""),
        payment_directive(rail="tempo-mainnet", id="chg_session", realm=REALM, intent="session", request=""),
    ]
    body = {
        "payment_required": True,
        "x402Version": 2,
        "product_name": "LLM completion",
        "pricing": {"max_usd": MAX_USDC, "billing": "pay-per-token"},
        "warnings": [
            "Cost is variable; final amount depends on output length.",
            "For one-shot completions use x402 upto. For long streams use tempo session.",
        ],
    }
    headers = {
        "www-authenticate": www_authenticate_header(directives),
        # `PAYMENT-REQUIRED` (x402 wire) is the base64-encoded body. Spec-strict
        # clients (Coinbase awal, purl) parse this header first; the JSON body
        # is the fallback for clients that don't.
        "PAYMENT-REQUIRED": b64encode(json.dumps({"x402Version": 2, "resource": {"url": url}}).encode()).decode(),
    }
    return body, headers


async def _run_your_llm(_prompt: str) -> tuple[str, int]:
    return "completion text here", 1234


@app.post("/llm/complete")
async def complete(request: Request):
    """x402 upto path: single JSON response with Settlement-Overrides."""
    # x402 carries the credential in either `x-payment` or `payment-signature`
    # depending on client (purl uses payment-signature; awal uses x-payment).
    if not (request.headers.get("x-payment") or request.headers.get("payment-signature")):
        body, headers = _build_402_body(str(request.url))
        return JSONResponse(body, status_code=402, headers=headers)

    body = await request.json()
    text, tokens_used = await _run_your_llm(body.get("prompt", ""))

    # Calculate actual cost based on tokens consumed.
    actual_usd = tokens_used * 0.000_002  # $2 per 1M tokens
    actual_atomic = str(int(actual_usd * 1_000_000))  # USDC atomic units

    # Tell the facilitator to settle for `actual_atomic` instead of the authorized max.
    name, value = settlement_override_header(amount=actual_atomic)
    return JSONResponse(
        {"text": text, "tokens_used": tokens_used, "charged_usd": actual_usd},
        headers={name: value},
    )


@app.post("/llm/stream")
async def stream(request: Request):
    """MPP tempo session path: agent opens channel, server streams SSE with mid-stream vouchers.

    Production wiring: ``mpp = await create_mppx_server(secret_key=MPP_SECRET, rails={
    "tempo_session": TempoSessionRailSpec(recipient=TEMPO_RECIPIENT,
    escrow_contract=TEMPO_ESCROW, store=YourChannelStore())})``; parse channel
    state from ``Authorization: Payment``, emit SSE chunks, request fresh voucher
    signatures as cumulative cost grows, close channel on completion.
    """
    if not request.headers.get("authorization"):
        body, headers = _build_402_body(str(request.url))
        return JSONResponse(body, status_code=402, headers=headers)
    return JSONResponse({"error": "stream-not-implemented"}, status_code=501)
