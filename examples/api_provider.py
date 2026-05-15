"""Example: API provider with per-call billing; multi-rail (Tempo MPP + x402 base + Solana MPP).

Scenario: you sell access to an HTTP API (search, scraping, RPC, etc.). Each call costs
a fixed price; agents pick whichever rail their wallet supports. No identity gate, no
compliance: purely pay-or-fail. Think Exa, QuickNode, anyone in the x402 Bazaar.

Rails advertised:
    - **Tempo MPP** (`tempo/charge` intent, carried in `Authorization: Payment`)
    - **x402 USDC on Base** (EIP-3009, carried in `x-payment` / `payment-signature`)
    - **Solana MPP** (`solana/charge` intent, carried in `Authorization: Payment`)

The 402 lists all rails neutrally; the agent picks based on what their wallet supports.

`Checkout(...)` collapses the ~150 lines of hand-rolled 402 envelope + header
parsing + rail dispatch in pre-2.0 examples to a single `compute_pricing` +
`on_settled` configuration. Discovery probes are still handled inline because
they advertise SAMPLE rails for crawlers (not the merchant's real rails).

Peer deps:
    pip install 'agentscore-commerce[fastapi,x402,mppx]'

Env vars:
    TEMPO_RECIPIENT       your Tempo wallet for receiving USDC.e
    X402_BASE_RECIPIENT   your Base wallet for receiving USDC
    SOLANA_RECIPIENT      your Solana wallet for receiving USDC
    X402_BASE_NETWORK     CAIP-2 (default eip155:8453 = Base mainnet;
                          override to eip155:84532 for Sepolia testnet)
    SOLANA_NETWORK_CAIP2  CAIP-2 (default solana mainnet; override to
                          solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1 for devnet)
    MPP_SECRET_KEY        secret_key for create_mppx_server (auto-wired)
    CDP_API_KEY_ID        Coinbase CDP key id; when set, the x402 facilitator
                          auto-promotes from public x402.org to Coinbase
    CDP_API_KEY_SECRET    Coinbase CDP key secret

Run: uvicorn examples.api_provider:app --port 3000
"""

import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce import Checkout, PricingResult, SettleOutcome
from agentscore_commerce.discovery import (
    NoindexNonDiscoveryMiddleware,
    X402SampleProbe,
    build_discovery_probe_response,
    is_discovery_probe_request,
)
from agentscore_commerce.payment import (
    SolanaMppRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
    networks,
)

PRICE_USDC = 0.01  # per-call price in USD
REALM = "api.example.com"

X402_BASE_NETWORK = os.environ.get("X402_BASE_NETWORK", networks.base.mainnet.caip2)
SOLANA_NETWORK_CAIP2 = os.environ.get("SOLANA_NETWORK_CAIP2", networks.solana.mainnet.caip2)
_TEMPO_RAIL_NAME = "tempo-testnet" if networks.base.sepolia.caip2 == X402_BASE_NETWORK else "tempo-mainnet"

app = FastAPI()

# noindex non-discovery paths so /search doesn't end up in human-shaped SERPs.
app.add_middleware(NoindexNonDiscoveryMiddleware)


async def _run_your_search(_query: str) -> list[Any]:
    """Vendor's actual search implementation."""
    return []


async def _compute_pricing(_ctx: Any) -> PricingResult:
    return PricingResult(amount_usd=PRICE_USDC)


async def _on_settled(ctx: Any, _outcome: SettleOutcome) -> dict[str, Any]:
    body = ctx.request.body if isinstance(ctx.request.body, dict) else {}
    results = await _run_your_search(body.get("query", ""))
    return {"results": results}


checkout = Checkout(
    rails={
        # Static treasury recipients; fail-fast at import time on missing env so
        # misconfigured deploys never reach the 402 emit path with empty rails.
        "tempo": TempoRailSpec(
            recipient=os.environ["TEMPO_RECIPIENT"],
            testnet=networks.base.sepolia.caip2 == X402_BASE_NETWORK,
        ),
        "x402_base": X402BaseRailSpec(
            recipient=os.environ["X402_BASE_RECIPIENT"],
            network=X402_BASE_NETWORK,
        ),
        "solana": SolanaMppRailSpec(
            recipient=os.environ["SOLANA_RECIPIENT"],
            network=SOLANA_NETWORK_CAIP2,
        ),
    },
    url=f"https://{REALM}/search",
    compute_pricing=_compute_pricing,
    on_settled=_on_settled,
    cdp_api_key_id=os.environ.get("CDP_API_KEY_ID"),
    cdp_api_key_secret=os.environ.get("CDP_API_KEY_SECRET"),
    mppx_secret_key=os.environ.get("MPP_SECRET_KEY"),
)


@app.post("/search")
async def search(request: Request) -> JSONResponse:
    body_bytes = await request.body()
    body_text = body_bytes.decode() if body_bytes else ""
    auth = request.headers.get("authorization")

    # Discovery probe: empty-body POST without any payment header. Return sample
    # 402 so crawlers (`awal x402 details`, x402-proxy, ...) can find this surface
    # without committing to a real charge. Handle inline because the probe
    # advertises SAMPLE accepts (not the merchant's real settle rails).
    if await is_discovery_probe_request(request.method, auth, body_text):
        probe = build_discovery_probe_response(
            realm=REALM,
            sample_rail=_TEMPO_RAIL_NAME,
            sample_amount_usd=PRICE_USDC,
            sample_recipient=os.environ["TEMPO_RECIPIENT"],
            x402_sample=X402SampleProbe(
                networks=[X402_BASE_NETWORK, SOLANA_NETWORK_CAIP2],
                resource_url=f"https://{REALM}/search",
            ),
        )
        return JSONResponse(json.loads(probe.body), status_code=probe.status, headers=probe.headers)

    return await checkout.handle_fastapi(request)
