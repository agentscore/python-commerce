# agentscore-commerce

[![PyPI version](https://img.shields.io/pypi/v/agentscore-commerce.svg)](https://pypi.org/project/agentscore-commerce/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

The full merchant-side SDK for [AgentScore](https://agentscore.sh) in Python — agent commerce in one install. Identity middleware (FastAPI, Flask, Django, AIOHTTP, Sanic, ASGI), payment helpers, 402 challenge builders, MPP discovery, and Stripe multichain support. Mirror of [`@agent-score/commerce`](https://github.com/agentscore/node-commerce) — same surface, idiomatic Python.

## Install

```bash
pip install agentscore-commerce[fastapi]   # or [flask], [django], [aiohttp], [sanic], [stripe]
```

## What's in the package

| Submodule | What it provides |
|---|---|
| `agentscore_commerce.identity.{fastapi,flask,django,aiohttp,sanic,middleware}` | Trust gate middleware: KYC, sanctions, age, jurisdiction. `AgentScoreGate(...)` (or `agentscore_gate(app, ...)` on Flask/Sanic), `get_assess_data(...)`, `capture_wallet(...)`, `verify_wallet_signer_match(...)`. |
| `agentscore_commerce.identity` (package level) | Re-exports the denial helpers: `denial_reason_status`, `denial_reason_to_body`, `build_signer_mismatch_body`, `build_contact_support_next_steps`, `verification_agent_instructions`, `is_fixable_denial`, `FIXABLE_DENIAL_REASONS`. |
| `agentscore_commerce.payment` | `networks`, `USDC`, `rails` registries; `payment_directive`, `build_payment_directive`, `www_authenticate_header`, `payment_required_header`, `settlement_override_header`, `dispatch_settlement_by_network`, `extract_payment_signer` (returns `PaymentSigner({address, network})`), `register_x402_schemes_v1_v2`. |
| `agentscore_commerce.discovery` | `is_discovery_probe_request`, `build_discovery_probe_response`, `build_well_known_mpp`, `build_llms_txt` + `llms_txt_identity_section` + `llms_txt_payment_section` (compact + verbose modes), `agentscore_openapi_snippets`, `build_bazaar_discovery_payload`. |
| `agentscore_commerce.challenge` | `build_402_body`, `build_accepted_methods`, `build_identity_metadata`, `build_how_to_pay`, `build_agent_instructions`. |
| `agentscore_commerce.stripe_multichain` | `create_multichain_payment_intent`, `get_deposit_address`, `simulate_crypto_deposit`. Peer dep on `stripe`. |
| `agentscore_commerce.api` | `AgentScore` re-exported from `agentscore-py` SDK. |

## Quick start (FastAPI)

```python
from fastapi import Depends, FastAPI, Request
from agentscore_commerce.identity.fastapi import (
    AgentScoreGate,
    capture_wallet,
    get_assess_data,
    verify_wallet_signer_match,
)

app = FastAPI()
gate = AgentScoreGate(
    api_key="ask_...",
    require_kyc=True,
    min_age=21,
    allowed_jurisdictions=["US"],
)

@app.post("/purchase", dependencies=[Depends(gate)])
async def purchase(request: Request, assess=Depends(get_assess_data)):
    # ... settle payment ...
    # After payment, capture the signer wallet for cross-merchant attribution
    await capture_wallet(request, signer, "evm", idempotency_key=payment_intent_id)
    return {"ok": True}
```

## Payment helpers

```python
from agentscore_commerce.payment import (
    BuildPaymentDirectiveInput,
    PaymentDirectiveInput,
    build_payment_directive,
    extract_payment_signer,
    networks,
    payment_directive,
    www_authenticate_header,
)

# Build paymentauth.org directives by symbolic rail name (decimals + currency from registry)
directives = [
    build_payment_directive(BuildPaymentDirectiveInput(
        rail="tempo-mainnet", id="chg_t", realm="ex.com", recipient=TEMPO_ADDR, amount_usd=0.01,
    )),
    build_payment_directive(BuildPaymentDirectiveInput(
        rail="x402-base-mainnet", id="chg_b", realm="ex.com", recipient=BASE_ADDR, amount_usd=0.01,
    )),
]
www_auth = www_authenticate_header(directives)

# Recover the on-chain signer (EVM) from an x402 header — returns PaymentSigner | None
signer = extract_payment_signer(request.headers.get("x-payment"))
if signer:
    print(signer.address, signer.network)  # ('0x...', 'evm')
```

## Discovery + 402 builders

```python
from agentscore_commerce.discovery import (
    BuildLlmsTxtInput,
    LlmsTxtIdentitySectionInput,
    LlmsTxtPaymentSectionInput,
    LlmsTxtSection,
    PaymentMethodConfig,
    WellKnownMppInput,
    build_llms_txt,
    build_well_known_mpp,
)
from agentscore_commerce.challenge import (
    Build402BodyInput,
    BuildAcceptedMethodsInput,
    BuildAgentInstructionsInput,
    BuildHowToPayInput,
    HowToPayRails,
    PricingBlock,
    TempoConfig,
    TempoRailConfig,
    build_402_body,
    build_accepted_methods,
    build_agent_instructions,
    build_how_to_pay,
)

accepted = build_accepted_methods(BuildAcceptedMethodsInput(tempo=TempoConfig(recipient=TEMPO_ADDR)))
how_to_pay = build_how_to_pay(BuildHowToPayInput(
    url="https://my.merchant/buy", retry_body_json="{}", total_usd="10.00",
    rails=HowToPayRails(tempo=TempoRailConfig(recipient=TEMPO_ADDR)),
))
body = build_402_body(Build402BodyInput(
    accepted_methods=accepted,
    agent_instructions=build_agent_instructions(BuildAgentInstructionsInput(how_to_pay=how_to_pay)),
    pricing=build_pricing_block(subtotal_cents=1000, tax_cents=80, shipping_cents=999, tax_rate=0.08, tax_state="CA"),
    amount_usd="10.80",
    # First-encounter merchants attach the cross-merchant agent_memory hint.
    agent_memory=first_encounter_agent_memory(first_encounter=not merchant.has_seen_operator(op_token)),
))
```

`build_pricing_block` handles cents → dollar-string (with optional shipping). `first_encounter_agent_memory` returns the canonical hint or `None` based on a per-merchant first-seen flag. `OrderReceipt` is a dataclass for the post-settlement 200 response shape.

### Idempotency-key + multi-rail header bundle

```python
from agentscore_commerce.payment import (
    BuildPaymentHeadersInput,
    PaymentHeadersRail,
    build_idempotency_key,
    build_payment_headers,
)

idempotency_key = build_idempotency_key(payment_intent_id=pi_id, order_id=order_id, amount_cents=amount)

headers = build_payment_headers(BuildPaymentHeadersInput(
    order_id=order_id,
    realm="agents.merchant.example",
    rails=[
        PaymentHeadersRail(rail="tempo-mainnet", amount_usd="10.00", recipient=TEMPO_ADDR),
        PaymentHeadersRail(rail="x402-base-mainnet", amount_usd="10.00", recipient=BASE_ADDR),
        PaymentHeadersRail(rail="stripe", amount_usd="10.00", network_id=STRIPE_PROFILE_ID),
    ],
))
# headers["www_authenticate"] → set as Authorization-style WWW-Authenticate header
# headers["payment_required"] → set as PAYMENT-REQUIRED header (when x402 is present)
```

### Identity publishing (cross-vendor standards)

```python
from agentscore_commerce.identity import (
    UCPService,
    UCPSigningKey,
    UCPPaymentHandler,
    A2AAgentCardCapabilities,
    build_erc8004_attribute,
    build_a2a_agent_card,
    build_ucp_profile,
)

# On-chain ERC-8004 (Trustless Agents) — vendor signs + submits via their wallet.
attr = build_erc8004_attribute(assess_result)

# Google A2A v1.0 Signed Agent Card — publish at /.well-known/agent-card.json
card = build_a2a_agent_card(name="My Service", url=base_url, capabilities=A2AAgentCardCapabilities(...), data=assess_result)

# Google Universal Commerce Protocol — publish at /.well-known/ucp
profile = build_ucp_profile(
    name="My Service",
    services=[UCPService(type="rest", url=base_url)],
    payment_handlers=[UCPPaymentHandler(name="tempo", config={"recipient": TEMPO_ADDR})],
    signing_keys=[UCPSigningKey(kid="me", kty="EC", alg="ES256")],
    data=assess_result,
)
```

ACP (Stripe + OpenAI Agentic Commerce Protocol) is a transactional checkout protocol with no identity-publishing surface — ACP merchants integrate via the existing `build_402_body` + `build_payment_headers` + Stripe SPT rail.

## Stripe multichain

```python
import os
import stripe
from agentscore_commerce.stripe_multichain import (
    CreateMultichainPaymentIntentInput,
    create_multichain_payment_intent,
    get_deposit_address,
)

stripe_client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])
result = create_multichain_payment_intent(CreateMultichainPaymentIntentInput(
    stripe=stripe_client,
    amount=1000,
    networks=["tempo", "base", "solana"],
    metadata={"order_id": order_id},
    idempotency_key=order_id,
))
base_address = get_deposit_address(result, "base")
solana_address = get_deposit_address(result, "solana")
```

## Examples

The [examples/](./examples) directory has 6 runnable single-file FastAPI apps covering common merchant scenarios. See [examples/README.md](./examples/README.md) for the full table.

## Differences from node-commerce

Python doesn't have peer-dep equivalents for `@x402/core`, `@x402/evm`, `@x402/svm`, or `mppx` — those are TypeScript-only ecosystems today. Three implications:

1. **No `create_x402_server` / `create_mppx_server` factories.** `register_x402_schemes_v1_v2` is exposed as the protocol-helper Python merchants use; happy-path setup happens via direct HTTP calls to your facilitator.
2. **`extract_payment_signer` returns EVM only.** Solana SPL Token payer recovery requires a Solana SDK (`solders` / `solana-py`) which isn't bundled. Pass the recovered Solana payer via `signer=...` to `verify_wallet_signer_match` directly.
3. **MPP tempo session (streaming payments)** doesn't ship a working Python implementation — there's no pip-installable `mppx` equivalent. The shape is documented in `examples/variable_cost_merchant.py`; vendors using session payments today bind to a Solana wallet library directly.

For Python merchants on x402 alone or x402 + Stripe, every other helper (directives, headers, dispatch, settle-overrides, signer extraction for EVM, accepted_methods, agent_instructions, how_to_pay, well-known/mpp.json, llms.txt) is fully native.

## Stability

`agentscore-commerce@1.0.0` ships with the full merchant SDK surface stable. Helpers are protocol translations + configurable opinions — most evolution is additive (new optional params, new helpers, new networks/rails). Major bumps are reserved for genuine protocol-mapping bugs.

## Documentation

Full integration docs at [docs.agentscore.sh/integrations/python-commerce](https://docs.agentscore.sh/integrations/python-commerce).

## License

[MIT](LICENSE)
