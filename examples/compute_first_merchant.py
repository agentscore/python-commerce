"""Example: variable-cost merchant via compute-first + exact-x402.

Scenario: you bill per unit of work (per result, per token, per byte). The
total can't be known until the work runs, but every payment rail in the
ecosystem signs an EXACT amount up front. The compute-first pattern flips the
order: probe runs the work server-side, caches the result, and emits a 402
with the EXACT computed price. The retry pays that price; the merchant
serves the cached result.

Why this exists (vs x402 upto / Permit2):

* upto's facilitator support is still limited (Coinbase CDP testnet rejects
  upto-mode settles today; only mainnet claims support).
* Permit2 is Ethereum-only — no Solana, no Tempo non-EIP-3009, no Stripe.
* Compute-first works on every exact-mode rail in the ecosystem with no
  buyer setup and no facilitator extensions.

The tradeoff: work runs on the unpaid probe leg, so rate-limiting is
load-bearing. Mount the SDK's rate-limit middleware globally and tune
``max_requests`` per your compute budget.

This example wires the x402-exact rail on Base only. To add MPP rails
(Tempo, Solana, Stripe SPT), pass a ``compose_mppx`` callback that builds
mppx intents at the exact cached price — see ``multi_rail_merchant.py``
for the fixed-price MPP compose pattern; the compute-first variant is
structurally identical except the helper passes the cached price +
recipients into your callback.

Peer deps::

    pip install 'agentscore-commerce[fastapi,x402,coinbase]'

Env vars::

    APP_URL              public URL of your service
    X402_BASE_RECIPIENT  Base wallet (USDC)
    X402_BASE_NETWORK    CAIP-2 (default eip155:8453)

Run: ``uvicorn examples.compute_first_merchant:app --port 3000``
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request

from agentscore_commerce import (
    ComputeFirstRails,
    ComputeFirstWorkContext,
    WorkOutcome,
    compute_first_checkout,
)
from agentscore_commerce.middleware.fastapi import rate_limit_fastapi
from agentscore_commerce.payment import X402BaseRailSpec, create_x402_server

APP_URL = os.environ.get("APP_URL", "https://api.example.com")
X402_BASE_NETWORK = os.environ.get("X402_BASE_NETWORK", "eip155:8453")
X402_BASE_RECIPIENT = os.environ.get("X402_BASE_RECIPIENT", "0xbase")


# Vendor's actual per-result work. Swap with a real search / enrichment / LLM
# call. The result_count drives pricing; the body is what the buyer receives.
async def _run_search(body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    query = str(body.get("query", ""))
    limit = int(body.get("limit", 5))
    matches = [
        {"id": f"result_{i}", "score": 0.9 - i * 0.05, "snippet": f"{query} hit {i}"} for i in range(min(limit, 8))
    ]
    return WorkOutcome(result_count=len(matches), body={"matches": matches, "total": 8492})


x402_server = create_x402_server(
    facilitator="coinbase",
    rails=["x402-base-sepolia" if X402_BASE_NETWORK == "eip155:84532" else "x402-base-mainnet"],
)

search_handler = compute_first_checkout(
    name="search",
    url=f"{APP_URL}/search",
    # $0.01 per result. Use 0.0001 for sub-cent / per-token pricing — the
    # helper auto-derives decimal precision from the unit price.
    unit_price_cents=1,
    rails=ComputeFirstRails(
        x402_base=X402BaseRailSpec(recipient=X402_BASE_RECIPIENT, network=X402_BASE_NETWORK),
    ),
    x402_server=x402_server,
    run_work=_run_search,
)

app = FastAPI()
# Rate-limit is load-bearing here: the probe leg runs the work without
# payment. Without it, an attacker can drain compute budget for free.
app.middleware("http")(rate_limit_fastapi(max_requests=60, window_seconds=60))


@app.post("/search")
async def search(request: Request) -> Any:
    return await search_handler.handle_fastapi(request)
