"""Example: API provider with per-call billing; multi-rail (Tempo MPP + x402 base + Solana MPP).

Scenario: you sell access to an HTTP API (search, scraping, RPC, etc.). Each call costs
a fixed price; agents pick whichever rail their wallet supports. No identity gate, no
compliance: purely pay-or-fail. Think Exa, QuickNode, anyone in the x402 Bazaar.

Rails advertised:
    - **Tempo MPP** (`tempo/charge` intent, carried in `Authorization: Payment`)
    - **x402 USDC on Base** (EIP-3009, carried in `x-payment` / `payment-signature`)
    - **Solana MPP** (`solana/charge` intent, carried in `Authorization: Payment`)

The 402 lists all rails neutrally; the agent picks based on what their wallet supports.

Python merchants on Solana implement MPP `solana/charge` server-side themselves;
there is no `@solana/mpp` Python equivalent today. This example only advertises the
Solana rail in the 402 directives; settle the credential via your facilitator API.

Peer deps:
    pip install agentscore-commerce[fastapi]

Env vars:
    TEMPO_RECIPIENT       your Tempo wallet for receiving USDC.e
    X402_BASE_RECIPIENT   your Base wallet for receiving USDC
    SOLANA_RECIPIENT      your Solana wallet for receiving USDC
    X402_BASE_NETWORK     CAIP-2 (default eip155:8453 = Base mainnet;
                          override to eip155:84532 for Sepolia testnet)
    SOLANA_NETWORK_CAIP2  CAIP-2 (default solana mainnet; override to
                          solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1 for devnet)

Run: uvicorn examples.api_provider:app --port 3000
"""

import json
import os
from base64 import b64encode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce.discovery import (
    NoindexNonDiscoveryMiddleware,
    X402SampleProbe,
    build_discovery_probe_response,
    is_discovery_probe_request,
)
from agentscore_commerce.payment import (
    USDC,
    networks,
    payment_directive,
    www_authenticate_header,
)

PRICE_USDC = 0.01  # per-call price in USD
REALM = "api.example.com"

# Read network selection from env so the same example serves mainnet + testnet.
X402_BASE_NETWORK = os.environ.get("X402_BASE_NETWORK", networks.base.mainnet.caip2)
SOLANA_NETWORK_CAIP2 = os.environ.get("SOLANA_NETWORK_CAIP2", networks.solana.mainnet.caip2)
_BASE_USDC = (
    USDC.base.sepolia.address if networks.base.sepolia.caip2 == X402_BASE_NETWORK else USDC.base.mainnet.address
)
_TEMPO_RAIL = "tempo-testnet" if networks.base.sepolia.caip2 == X402_BASE_NETWORK else "tempo-mainnet"

app = FastAPI()

# noindex non-discovery paths so /search doesn't end up in human-shaped SERPs.
# Defaults cover /openapi.json, /llms.txt, /.well-known/{mpp.json,agent-card.json,ucp},
# /favicon.{png,ico} — pass `custom_paths={"/sitemap.xml"}` to extend or
# `replace_paths=True` to swap the set entirely.
app.add_middleware(NoindexNonDiscoveryMiddleware)


@app.post("/search")
async def search(request: Request):
    body = await request.body()
    body_text = body.decode() if body else ""

    auth = request.headers.get("authorization")
    x402_header = request.headers.get("payment-signature") or request.headers.get("x-payment")

    # Discovery probe — empty-body POST without any payment header → return sample 402.
    if await is_discovery_probe_request(request.method, auth, body_text):
        probe = build_discovery_probe_response(
            realm=REALM,
            sample_rail=_TEMPO_RAIL,
            sample_amount_usd=PRICE_USDC,
            sample_recipient=os.environ["TEMPO_RECIPIENT"],
            # Advertise x402 support so crawlers (e.g. ``awal x402 details``)
            # can find it on an empty-body POST. Commerce synthesizes USDC
            # sample accepts from the registry per CAIP-2 network passed.
            x402_sample=X402SampleProbe(
                networks=[X402_BASE_NETWORK, SOLANA_NETWORK_CAIP2],
                resource_url=f"{REALM}/search",
            ),
        )
        return JSONResponse(json.loads(probe.body), status_code=probe.status, headers=probe.headers)

    # No payment? Return a 402 with directives for all accepted rails.
    if not (auth and auth.startswith("Payment ")) and not x402_header:
        challenge_id = f"chg_{os.urandom(8).hex()}"
        x402_base_rail = (
            "x402-base-sepolia" if networks.base.sepolia.caip2 == X402_BASE_NETWORK else "x402-base-mainnet"
        )
        solana_mpp_rail = (
            "mpp-solana-devnet" if networks.solana.devnet.caip2 == SOLANA_NETWORK_CAIP2 else "mpp-solana-mainnet"
        )
        directives = [
            payment_directive(rail=_TEMPO_RAIL, id=f"{challenge_id}_tempo", realm=REALM, request=""),
            payment_directive(rail=x402_base_rail, id=f"{challenge_id}_base", realm=REALM, request=""),
            payment_directive(rail=solana_mpp_rail, id=f"{challenge_id}_solana", realm=REALM, request=""),
        ]
        accepts = [
            {
                "scheme": "exact",
                "network": X402_BASE_NETWORK,
                "amount": str(int(PRICE_USDC * 1_000_000)),
                "asset": _BASE_USDC,
                "payTo": os.environ["X402_BASE_RECIPIENT"],
                "maxTimeoutSeconds": 300,
                # EIP-712 domain required by every x402 EVM client to sign
                # EIP-3009 TransferWithAuthorization. ``name`` MUST match the
                # on-chain USDC contract's ``name()`` — base mainnet returns
                # "USD Coin", base sepolia returns "USDC". Wrong value silently
                # breaks signature verify at the facilitator. Production code
                # should use ``build_x402_accepts_for_402(server, ...)`` which
                # derives ``extra`` from the registered scheme metadata.
                "extra": {
                    "name": "USD Coin" if X402_BASE_NETWORK.split(":")[-1] == "8453" else "USDC",
                    "version": "2",
                },
            },
        ]
        return JSONResponse(
            {"payment_required": True, "x402Version": 2, "accepts": accepts},
            status_code=402,
            headers={
                "www-authenticate": www_authenticate_header(directives),
                "PAYMENT-REQUIRED": b64encode(
                    json.dumps({"x402Version": 2, "accepts": accepts, "resource": {"url": str(request.url)}}).encode()
                ).decode(),
            },
        )

    # Payment present; branch on which header arrived:
    #   Authorization: Payment ... → MPP (tempo or solana); validate via your facilitator's MPP API
    #   payment-signature / x-payment → x402 base; validate via x402 facilitator
    # Both shapes settle through the configured facilitator HTTP API, then run your operation.

    body_json = json.loads(body_text)
    results = await run_your_search(body_json.get("query", ""))
    return {"results": results}


async def run_your_search(_query: str) -> list:
    # Vendor's actual search implementation
    return []
