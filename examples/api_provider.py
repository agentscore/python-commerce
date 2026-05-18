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

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from agentscore_commerce import (
    Checkout,
    DiscoveryProbeConfig,
    PricingResult,
    SettleOutcome,
    SolanaMppRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
)
from agentscore_commerce.discovery import (
    NoindexNonDiscoveryMiddleware,
    X402SampleProbe,
    build_merchant_index_json,
    build_redemption_skill_md,
    standard_endpoint_descriptions,
)
from agentscore_commerce.middleware.fastapi import RateLimitMiddleware
from agentscore_commerce.payment import networks

PRICE_USDC = 0.01  # per-call price in USD
REALM = "api.example.com"

X402_BASE_NETWORK = os.environ.get("X402_BASE_NETWORK", networks.base.mainnet.caip2)
SOLANA_NETWORK_CAIP2 = os.environ.get("SOLANA_NETWORK_CAIP2", networks.solana.mainnet.caip2)
_TEMPO_RAIL_NAME = "tempo-testnet" if networks.base.sepolia.caip2 == X402_BASE_NETWORK else "tempo-mainnet"

app = FastAPI()
app.add_middleware(RateLimitMiddleware)

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
    # Auto-route empty-body POSTs without a payment header to a sample 402 so
    # crawlers (`awal x402 details`, x402-proxy, ...) can find this surface
    # without committing to a real charge. The probe advertises SAMPLE accepts;
    # real rails fire only when the agent retries with a credential.
    discovery_probe=DiscoveryProbeConfig(
        realm=REALM,
        sample_rail=_TEMPO_RAIL_NAME,
        sample_amount_usd=PRICE_USDC,
        sample_recipient=os.environ["TEMPO_RECIPIENT"],
        x402_sample=X402SampleProbe(
            networks=[X402_BASE_NETWORK, SOLANA_NETWORK_CAIP2],
            resource_url=f"https://{REALM}/search",
        ),
    ),
)


@app.post("/search")
async def search(request: Request) -> JSONResponse:
    return await checkout.handle_fastapi(request)


@app.get("/")
async def root() -> JSONResponse:
    """Discovery root for API merchants. Mirror of the goods-merchant `/` pattern.

    Lists endpoints, supported rails, docs, and per-call pricing so agents can
    discover this merchant from a Bazaar listing or a llms.txt cross-link.
    """
    return JSONResponse(
        build_merchant_index_json(
            name="Example Search API",
            description=(
                "Agent-native search API. Per-call billing on Tempo, x402 Base, and "
                "Solana. Trial credit codes (single-use) settle a fixed number of free "
                "calls before the wallet starts paying."
            ),
            docs={
                "redemption": f"https://{REALM}/redemption.md",
            },
            endpoints=standard_endpoint_descriptions(kind="api"),
            supported_rails=["tempo", "x402-base", "solana-mpp"],
            extra={
                "pricing": {
                    "per_call_usd": f"{PRICE_USDC:.2f}",
                    "trial_credit_codes": "single-use; settle one paid call for free",
                },
            },
        )
    )


@app.get("/redemption.md", response_class=PlainTextResponse)
async def redemption_md() -> str:
    """Agent-facing skill.md for trial-credit codes.

    The pattern is delivery-neutral; whether codes are emailed in a developer
    onboarding email, surfaced in a dashboard, or distributed via partner
    promotions, the redemption flow is the same: submit the code in the body
    next to the regular call shape, the server burns it single-use, and the
    402 either skips entirely ($0 settle) or charges the discounted amount.
    """
    return build_redemption_skill_md(
        merchant_name="Example Search API",
        app_url=f"https://{REALM}",
        endpoint_path="/search",
        sku_intro=(
            "The code unlocks one free `POST /search` call. After that, the "
            "endpoint reverts to standard per-call billing."
        ),
        delivery_intro=(
            "You're reading this because the developer you're working for received "
            "a single-use trial credit code from Example Search API (typically via "
            "the developer onboarding email or dashboard). This page tells you, the "
            "agent, exactly how to turn that code into a successful call."
        ),
        body_shape="""{
     "query": "<search query>",
     "redemption_code": "<code>"
   }""",
        # API endpoint takes only query + redemption_code; no shipping rules apply.
        body_rules="",
    )
