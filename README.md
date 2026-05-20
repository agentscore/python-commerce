# agentscore-commerce

[![PyPI version](https://img.shields.io/pypi/v/agentscore-commerce.svg)](https://pypi.org/project/agentscore-commerce/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

The full merchant-side SDK for [AgentScore](https://agentscore.sh) in Python: agent commerce in one install. Identity middleware (FastAPI, Flask, Django, AIOHTTP, Sanic, ASGI), payment helpers, 402 challenge builders, MPP discovery, and Stripe multichain support.

## Install

```bash
pip install agentscore-commerce[fastapi]   # or [flask], [django], [aiohttp], [sanic], [stripe]
```

For x402 + Coinbase facilitator support (mints per-endpoint CDP JWTs via `cdp-sdk`):

```bash
pip install 'agentscore-commerce[fastapi,x402,coinbase]'
# Set CDP_API_KEY_ID and CDP_API_KEY_SECRET in the environment.
```

`[mppx]` adds Tempo MPP + Stripe SPT helpers via `pympp[server,tempo,stripe]`.

## What's in the package

| Submodule | What it provides |
|---|---|
| `agentscore_commerce` (top-level) | `Checkout` orchestrator + `CheckoutContext` + `CheckoutGateConfig` + `CheckoutValidationError` + `DiscoveryProbeConfig` + `SettleOutcome` + `MppxComposeOutcome` + `PricingResult` (the 2.0 high-level surface: one config object, hooks for pre_validate/compute_pricing/on_settled/mint_recipients/compose_mppx, auto-derived x402+mppx servers, per-framework adapters `handle_fastapi`/`handle_flask`/`handle_django`/`handle_aiohttp`/`handle_sanic`, signed UCP routes via `mount_ucp_routes_{fastapi,flask,django,aiohttp,sanic}`); `pricing_result` (factory: cents-denominated → typed `PricingResult` with embedded `PricingBlock`); `validation_response_{fastapi,flask,django,aiohttp,sanic}` (per-framework 4xx envelope wrappers); `make_mppx_compose_hook` (canonical pympp compose adapter). |
| `agentscore_commerce.identity.{fastapi,flask,django,aiohttp,sanic,middleware}` | Trust gate middleware: KYC, sanctions (account name + signer wallet), age, jurisdiction. `AgentScoreGate(...)` (or `agentscore_gate(app, ...)` on Flask/Sanic), `get_agentscore_data(...)`, `capture_wallet(...)`, `get_signer_verdict(...)`. The gate extracts the payment signer pre-evaluate and passes it to `/v1/assess`, so the API composes both wallet-binding (`signer_match`) and OFAC SDN wallet-address (`signer_sanctions`) verdicts on one round trip. |
| `agentscore_commerce.identity` (package level) | Re-exports the denial helpers: `denial_reason_status`, `denial_reason_to_body`, `build_signer_mismatch_body`, `build_contact_support_next_steps`, `verification_agent_instructions`, `is_fixable_denial`, `FIXABLE_DENIAL_REASONS`. The per-framework adapter modules also expose `get_gate_quota_info(request)` for surfacing X-RateLimit info from gate state. Also re-exports the per-product policy helpers: `PolicyBlock`, `GateResult`, `EnforcementMode`, `IdentityStatus`, `build_gate_from_policy`, `run_gate_with_enforcement`, `shipping_country_allowed`, `shipping_state_allowed`, `validate_shipping_against_policy` (one-call country+state validator that raises `CheckoutValidationError` with the canonical envelope on miss) — for multi-product merchants where each product carries its own compliance config: hard gate vs soft vs none, per-product shipping allowlists. Key + token helpers: `load_ucp_signing_key_from_env` (cached env-driven loader for the UCP signing key — reads `UCP_SIGNING_KEY_JWK_PRIVATE` JSON JWK, detects alg from shape, falls back to ephemeral when unset, sanitizes errors so key bytes never reach logs, concurrent-safe via `threading.Lock`; env-var names and `default_kid` / `default_alg` are overridable as kwargs); `hash_operator_token` (sha256 hex of plaintext `opc_...` — for merchants persisting `operator_token_id` to their own DB without ever storing the plaintext); `extract_owner_scope(headers) -> OwnerScope` (canonical owner-identity extractor for caller-scoped resource queries — reads `X-Wallet-Address` / `X-Operator-Token`, hashes the token so plaintext never leaves the request); `has_payment_header` / `has_x402_header` / `has_mppx_header` (request discriminators — any-credential vs x402 vs MPP); `default_read_only_on_denied(reason)` (canonical `on_denied` for read-only resource gates: 401 + `Cache-Control: no-store` while still spreading `denial_reason_to_body`. Returns a `DefaultOnDeniedResult(body, status, headers)`; FastAPI / Flask / aiohttp / Sanic `on_denied` callbacks accept an optional 3-tuple `(body, status, headers)` to carry headers through; wrap with `lambda req, reason: (lambda r: (r.body, r.status, r.headers or {}))(default_read_only_on_denied(reason))`). |
| `agentscore_commerce.payment` | `networks`, `USDC`, `rails` registries; `payment_directive`, `build_payment_directive`, `www_authenticate_header`, `payment_required_header`, `alias_amount_fields` (v1↔v2 amount field shim that emits both `amount` and `maxAmountRequired` so v1-only x402 parsers like Coinbase awal can read v2 bodies), `settlement_override_header`, `dispatch_settlement_by_network`, `extract_payment_signer` (accepts positional `x402_payment_header` AND/OR `authorization_header=` kwarg; recovers signer from x402 EIP-3009 `payload.authorization.from` OR MPP `Authorization: Payment <base64>` `did:pkh:eip155:<chain>:<addr>` / `did:pkh:solana:<genesis>:<addr>` source DID), `detect_rail_from_headers` (returns `"x402"` / `"mpp"` / `None` from inbound headers), `register_x402_schemes_v1_v2`; drop-in x402 helpers: `validate_x402_network_config` (boot-time guard), `verify_x402_request` (parse + validate inbound X-Payment), `process_x402_settle` (verify-then-settle with one call), `classify_x402_settle_result` (maps the tagged settle result to a recommended HTTP status / code / next_steps so merchants get a controlled envelope without coupling to facilitator-specific error text), `classify_orchestration_error` (same `ClassifiedX402Error` shape but for uncaught exceptions thrown elsewhere in the orchestration; returns `None` for unknown errors so merchants rethrow instead of swallowing); `zero_amount_carve_out` (skip CDP / pympp upstream verify+settle for $0 settles where the upstream rejects value=0 payloads; parses the credential, lifts signer + network, returns a `ZeroSettleResult` shaped identically to the success path so callers branch on rail, not on result shape); `usd_to_atomic` (Decimal-based USD → atomic int, ROUND_HALF_UP — for Tempo / Solana / Base USDC amount construction). |
| `agentscore_commerce.discovery` | `is_discovery_probe_request`, `build_discovery_probe_response` (with optional `x402_sample` for x402-aware crawlers like `awal x402 details`), `sample_x402_accept_for_network` (USDC sample-accept builder for known CAIP-2 networks), `build_well_known_mpp`, `build_llms_txt` + `llms_txt_identity_section` + `llms_txt_payment_section` (compact + verbose modes), `build_skill_md` (Claude-Skill-compatible `/skill.md` agent-discovery manifest; strictly agent-facing data only, no internal posture), `build_redemption_skill_md` (delivery-neutral redemption-code template — printed mailers, emailed codes, API trial credits all covered; `endpoint_path`/`delivery_intro`/`body_shape`/`body_rules`/`extra_recovery_rows` overrides for non-goods shapes), `build_merchant_index_json` (canonical `/` discovery body), `standard_endpoint_descriptions(kind=)` (canonical method+path → description map for goods vs api merchants; optional `include_order_status_route` for goods), `build_success_next_steps` (universal Passport-active success block), `build_agentscore_onboarding_steps` (canonical skill.md onboarding for goods or API merchants), `agentscore_openapi_snippets`, `build_bazaar_discovery_payload`, `NoindexNonDiscoveryMiddleware` (ASGI middleware emitting `X-Robots-Tag: noindex` on every path except the agent-discovery surfaces; pure helpers `is_discovery_path` + `DEFAULT_DISCOVERY_PATHS` for non-ASGI frameworks). Plus the UCP/JWKS publish surface: `build_signed_ucp_response`, `build_signed_jwks_response`, `well_known_preflight_response`, `default_a2a_services`, `bootstrap_ucp_signing_key`, framework-neutral `SignedDiscoveryResponse` + per-framework wrappers `signed_response_{fastapi,flask,django,aiohttp,sanic}`. |
| `agentscore_commerce.challenge` | `build_402_body`, `build_accepted_methods`, `build_identity_metadata` (auto-attached by `Checkout` when an inbound `X-Wallet-Address` header is present), `build_how_to_pay`, `build_agent_instructions` (auto-emits per-rail `compatible_clients`: smoke-verified CLIs the agent should use; vendor override supported; pure helper `compatible_clients_by_rails(rails)` returns the same map for vendors building custom 402s), `build_pricing_block` (cents to dollar-string with optional shipping/tax), `first_encounter_agent_memory` (cross-merchant hint, returns the canonical block or `None` based on a per-merchant first-seen flag), `Receipt` + `ReceiptNextSteps` + `ProductInfo` + `ShippingAddress` (canonical 200-receipt dataclasses — universal across goods + API merchants); `respond_402`, a drop-in 402 emit that preserves pympp's `WWW-Authenticate` and layers x402's `PAYMENT-REQUIRED`. `build_validation_error`: structured 4xx body builder (`{error: {code, message}, required_fields?, example_body?, next_steps?, ...extra}`) so vendors compose body shapes by name instead of inlining at every validation site. |
| `agentscore_commerce.stripe_multichain` | `create_multichain_payment_intent` (returns `MultichainPaymentIntentResult(payment_intent_id, deposit_addresses)`; read `result.deposit_addresses[network]` directly), `create_pay_to_address_from_stripe_pi(authorization_header=, amount_cents=, stripe=, pi_cache=, networks=, metadata=, order_id=, preferred_network=)` — per-order payTo resolver: on the settle leg, reuses the buyer's signed-against payTo from the MPP credential (after `pi_cache.has_address` check); on the discovery leg, mints a fresh PI and caches it. `simulate_crypto_deposit`; `create_pi_cache` (TTL'd PI / deposit-address cache, Redis-backed when `redis_url` set, in-memory otherwise), `simulate_deposit_if_test_mode` (gates on `sk_test_` and looks up the PI for you), `STRIPE_TEST_TX_HASH_SUCCESS` / `STRIPE_TEST_TX_HASH_FAILED` constants. Peer dep on `stripe`. |
| `agentscore_commerce.api` | Everything from `agentscore-py` re-exported in one place: `AgentScore` + `AgentScoreError`, `AGENTSCORE_TEST_ADDRESSES` + `is_agentscore_test_address`. **Don't add `agentscore-py` as a separate dep**: the two can drift versions and cause subtle type mismatches. |
| `agentscore_commerce.middleware.{fastapi,flask,django,aiohttp,sanic,asgi}` | Framework-specific rate-limit middleware. FastAPI: `rate_limit_fastapi(...)` (FastAPI dependency) plus the ASGI `RateLimitMiddleware` re-export. Flask: `rate_limit_flask(app, ...)` installer. Django: class-based async `RateLimitMiddleware` configured via `settings.AGENTSCORE_RATE_LIMIT`. aiohttp: `rate_limit_aiohttp(...)` middleware factory. Sanic: `rate_limit_sanic(app, ...)` installer. `asgi.RateLimitMiddleware` works with any starlette-compatible app. Shared options: `window_seconds` (default 60), `max_requests` (default 60), `key_resolver` (default first hop of `x-forwarded-for`), `redis_url` (lazy-imports `redis.asyncio` when set, in-memory `dict` fallback otherwise), `key_prefix`. `redis` is an optional peer dep (install via the `redis` extra). |

## Quick start (FastAPI)

### Rate limiting

Mount globally before any payment route so probe and settle legs share the same bucket. Defaults: 60 req / 60 s / IP. Redis when `REDIS_URL` is set, in-memory fallback otherwise.

```python
from fastapi import FastAPI
from agentscore_commerce.middleware.asgi import RateLimitMiddleware

app = FastAPI()
app.add_middleware(RateLimitMiddleware, max_requests=60, window_seconds=60)
```

Same factory shape per framework: `rate_limit_flask(app, ...)`, `rate_limit_aiohttp(...)`, `rate_limit_sanic(app, ...)`, Django's `RateLimitMiddleware` class in `MIDDLEWARE`, and `rate_limit_fastapi(...)` for a `Depends`-able per-route variant. Override `max_requests` / `window_seconds` / `key_resolver` / `redis_url` / `key_prefix` as needed.

### Identity gate

```python
from fastapi import Depends, FastAPI, Request
from agentscore_commerce.identity.fastapi import (
    AgentScoreGate,
    capture_wallet,
    get_agentscore_data,
    get_signer_verdict,
)

app = FastAPI()
_gate = AgentScoreGate(
    api_key="as_live_...",
    require_kyc=True,
    min_age=21,
    allowed_jurisdictions=["US"],
)


# Run the gate CONDITIONALLY: only when a payment credential is already attached.
# Anonymous discovery (no payment header) flows through to the handler so any spec-
# compliant x402 wallet can read the 402 challenge with rails + pricing without first
# proving identity. Identity is verified at settle time on the retry leg.
from agentscore_commerce.payment import has_payment_header

async def gate_on_settle(request: Request) -> None:
    if not has_payment_header(request):
        return None
    return await _gate(request)


@app.post("/purchase", dependencies=[Depends(gate_on_settle)])
async def purchase(request: Request, assess=Depends(get_agentscore_data)):
    # ... settle payment ...
    # After payment, capture the signer wallet for cross-merchant attribution
    await capture_wallet(request, signer, "evm", idempotency_key=payment_intent_id)
    return {"ok": True}
```

## Checkout orchestrator (the 2.0 high-level surface)

`Checkout` is the canonical merchant surface: one config object, hooks for the merchant-specific pieces, and the SDK handles 402 emit, identity gating, x402 verify+settle, mppx compose, $0 carve-out, and the per-framework adapter. Most merchants reach for `Checkout` first and drop to lower-level helpers only when they need custom flows.

```python
from fastapi import FastAPI, Request
from agentscore_commerce import (
    Checkout, CheckoutGateConfig, DiscoveryProbeConfig, PricingResult, pricing_result,
    SolanaMppRailSpec, StripeRailSpec, TempoRailSpec, X402BaseRailSpec,
    validate_shipping_against_policy,
)
from agentscore_commerce.discovery import default_a2a_services

app = FastAPI()

async def _pre_validate(ctx):
    body = ctx.request.body or {}
    product = await lookup_product(body.get("product_slug"))
    validate_shipping_against_policy(
        country=body.get("shipping", {}).get("country", ""),
        state=body.get("shipping", {}).get("state", ""),
        policy=product,
        product_name=product["name"],
    )
    return {"product": product}

async def _compute_pricing(ctx) -> PricingResult:
    return pricing_result(
        subtotal_cents=ctx.state["product"]["price_cents"],
        tax_cents=ctx.state["product"]["tax_cents"],
        tax_rate=ctx.state["product"]["tax_rate"],
        tax_state=ctx.state["product"]["tax_state"],
    )

async def _on_settled(ctx, outcome):
    return {"ok": True, "order_id": ctx.reference_id, "tx_hash": outcome.tx_hash}

checkout = Checkout(
    rails={
        "tempo":     TempoRailSpec(recipient=os.environ["TEMPO_RECIPIENT"]),
        "x402_base": X402BaseRailSpec(recipient=os.environ["X402_BASE_RECIPIENT"], network="eip155:8453"),
        "solana_mpp":SolanaMppRailSpec(recipient=os.environ["SOLANA_RECIPIENT"], network="solana:mainnet"),
        "stripe":    StripeRailSpec(profile_id=os.environ["STRIPE_PROFILE_ID"]),
    },
    url="https://merchant.example/purchase",
    pre_validate=_pre_validate,
    compute_pricing=_compute_pricing,
    on_settled=_on_settled,
    cdp_api_key_id=os.environ.get("CDP_API_KEY_ID"),
    cdp_api_key_secret=os.environ.get("CDP_API_KEY_SECRET"),
    mppx_secret_key=os.environ.get("MPP_SECRET_KEY"),
    gate=CheckoutGateConfig(
        api_key=os.environ["AGENTSCORE_API_KEY"],
        merchant_name="Merchant",
        require_kyc=True, require_sanctions_clear=True, min_age=21, allowed_jurisdictions=["US"],
    ),
    # Optional: empty-body POSTs without a payment header auto-route to a sample 402
    # so x402 crawlers (awal x402 details, x402-proxy, ...) can discover the surface.
    discovery_probe=DiscoveryProbeConfig(
        realm="merchant.example",
        sample_rail="tempo-mainnet",
        sample_amount_usd=1.0,
        sample_recipient=os.environ["TEMPO_RECIPIENT"],
    ),
)

# Mount signed UCP profile + JWKS + OPTIONS preflights in one call.
checkout.mount_ucp_routes_fastapi(
    app,
    name="Merchant",
    well_known_ucp_url="https://merchant.example/.well-known/ucp",
    services=default_a2a_services(agent_card_url="https://merchant.example/.well-known/agent-card.json"),
    signing_kid="merchant-2026-05",
)

@app.post("/purchase")
async def purchase(request: Request):
    return await checkout.handle_fastapi(request)
```

The 402 body Checkout emits auto-attaches `identity_mode` + `required_signer` + `signer_constraint` (and `linked_wallets` when the gate populated them) when an inbound `X-Wallet-Address` header is present — so agents self-correct at discovery instead of at the 403 retry.

For **variable-cost pay-per-result** endpoints (per-result search, per-token LLM, per-byte transcoding), reach for `compute_first_checkout` — same config shape, but the probe leg runs the work, caches by body content-hash, and emits a 402 with the EXACT computed price. The retry pays that exact amount and receives the cached body. Scope is exact-mode rails only (x402-exact Base, tempo/charge, solana/charge, Stripe SPT); does NOT use x402-upto (Permit2) or Settlement-Overrides — variable cost is captured by running the work pre-settle. Tradeoff: the work runs on the unpaid probe leg, so mount `rate_limit_fastapi` (from `agentscore_commerce.middleware.fastapi`) globally — it's load-bearing. See `examples/compute_first_merchant.py`.

For the `on_denied` hook on Checkout's gate config, `create_default_on_denied(merchant_name=, support_email=, ...)` returns the canonical denial callback that handles `wallet_signer_mismatch` / `wallet_not_trusted` unfixable fallback / `payment_required` / `token_expired` / `invalid_credential` / `api_error`. Merchants override `wallet_not_trusted_message` / `payment_required_message` / `support_context` for vendor-specific copy and keep their own merchant-specific branches (e.g. wine merchants add a fixable-denial-with-session branch on top).

`build_default_checkout_rails(tempo=, x402_base=, solana_mpp=, stripe=)` builds the canonical four-rail `rails` dict so merchants pass per-rail overrides instead of redeclaring the recipient sentinel + network/chain_id/token boilerplate. Flipping `network` alone is enough: Base Sepolia derives Sepolia USDC + chain_id 84532, Solana devnet derives the devnet USDC mint. Solana's `network` field accepts both CAIP-2 (`solana:5eykt4UsFv8…` / `solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1`) and the raw `@solana/mpp` form (`mainnet-beta` / `devnet` / `localnet`). `build_mppx_compose_rails(amount_usd=, tempo_recipient=, solana_recipient=, ...)` builds the per-call mppx intent list. The helper auto-drops `stripe/charge` (with a one-time `logging.warning`) when `amount_usd < 0.50` since Stripe's fixed ~$0.30 fee makes sub-50-cent charges unprofitable; sub-50-cent APIs pass `include_stripe=False` explicitly to silence the warning. `simulate_deposit_for_outcome(outcome=, deposit_address=, get_payment_intent_id=, stripe_secret_key=)` dispatches the Stripe testnet simulator from `on_settled` based on the rail family (no per-merchant rail switch needed).

## Payment helpers

```python
from agentscore_commerce import extract_payment_signer
from agentscore_commerce.payment import (
    BuildPaymentDirectiveInput,
    PaymentDirectiveInput,
    build_payment_directive,
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

# Recover the on-chain signer (EVM) from an x402 header. Returns PaymentSigner | None.
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
    build_pricing_block,
    first_encounter_agent_memory,
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

`build_pricing_block` handles cents → dollar-string (with optional shipping). Pass `discount_cents` for redemption codes / coupons: `subtotal` stays the list price, the block surfaces `discount` as a dollar-string, and `total` becomes `subtotal + tax + shipping - discount` (floored at 0). `pricing_result` accepts the same `discount_cents` and propagates it to `block.discount` so agents reading the 402 see the savings line. Pass `decimals: N` (default `2`) on either helper for sub-cent unit pricing — e.g. `decimals=4` advertises `$0.0005`-precision instead of rounding to two decimals. Set `decimals` on `PricingResult` and the SDK threads it through `build_how_to_pay`, `build_pricing_block`, and the x402 settle `price` string automatically; the cents inputs accept floats under that mode (per-token / per-byte unit pricing). `first_encounter_agent_memory` returns the canonical hint or `None` based on a per-merchant first-seen flag. `Receipt` (plus `ReceiptNextSteps`, `ProductInfo`, `ShippingAddress`) is a universal dataclass for the post-settlement 200 response shape — goods merchants populate the shipping/fulfillment/tracking slots, API merchants fill only the universal fields (id, created_at, pricing, payment_status, next_steps).

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
    AgentScoreGatePolicy,
    UCPServiceBinding,
    UCPSigningKey,
    UCPPaymentHandlerBinding,
    A2AAgentSkill,
    build_a2a_agent_card,
    build_ucp_profile,
    ucp_a2a_extension,
)

# Google A2A v1.0 Signed Agent Card. Publish at /.well-known/agent-card.json.
# Per UCP §A2A binding the card MUST declare the canonical UCP extension URI in
# `capabilities.extensions[]`; pass `ucp_a2a_extension()` with empty capabilities
# until you bind formal UCP capabilities (dev.ucp.shopping.checkout, etc.).
# Skills are top-level AgentSkill objects; identity claims live in a separate
# AgentCardSignature (RFC 7515 JWS) wrapping the serialized card.
card = build_a2a_agent_card(
    name="My Service",
    description="Buy products via agent payments.",
    url=base_url,
    version="1.0.0",
    skills=[
        A2AAgentSkill(
            id="purchase",
            name="Purchase",
            description="Buy products via agent payments.",
            tags=["commerce", "payment"],
        ),
    ],
    extensions=[ucp_a2a_extension()],
)

# Google Universal Commerce Protocol. Publish at /.well-known/ucp.
# Output shape: {"ucp": {"version", "services", "capabilities",
# "payment_handlers", "name?", "supported_versions?"}, "signing_keys": [...]}
# — services / capabilities / payment_handlers are MAPS keyed by reverse-DNS
# service / capability / handler name (UCP spec §3 + §6).
profile = build_ucp_profile(
    name="My Service",
    services={
        "dev.ucp.shopping": [
            UCPServiceBinding(
                version="2026-04-08",
                spec="https://ucp.dev/2026-04-08/specification/overview",
                transport="mcp",
                endpoint=f"{base_url}/api/ucp/mcp",
                schema="https://ucp.dev/services/shopping/mcp.openrpc.json",
            ),
        ],
    },
    payment_handlers={
        **mpp_payment_handler(networks=[{"network": "tempo-mainnet", "chain_id": 4217, "recipient": TEMPO_ADDR}]),
        **x402_payment_handler(networks=[{"network": "base-8453", "recipient": BASE_ADDR}]),
        **stripe_spt_payment_handler(profile_id="profile_5xKvNqM9BaH"),
    },
    signing_keys=[UCPSigningKey(kid="me", kty="EC", alg="ES256")],
    # Optional: declare merchant gate policy as an `sh.agentscore.identity` capability
    # binding inside the public profile. Static policy declaration only — no per-operator
    # claims. Per-operator identity attestation flows through the AP2 risk-signal endpoint.
    agentscore_gate=AgentScoreGatePolicy(
        require_kyc=True, min_age=21, allowed_jurisdictions=["US"],
    ),
)
```

UCP §6 doesn't mandate profile-body JWS signing; production UCP merchants commonly ship unsigned. AgentScore's `agentscore-profile+jws` is a vendor extension for trust-mode verifiers (regulated-commerce, AP2-aware) that opt into auditable profiles. Sign + verify via the optional `joserfc` extra (tested against joserfc v1.x; pin `joserfc>=1.0.0,<2`):

```bash
pip install agentscore-commerce[ucp]
```

```python
from agentscore_commerce.identity import (
    UCPSigningKey,
    UCPVerificationError,
    build_jwks_response,
    build_ucp_profile,
    generate_ucp_signing_key,
    sign_ucp_profile,
    verify_ucp_profile,
)

key = generate_ucp_signing_key(kid="merchant-2026-05")
profile = build_ucp_profile(
    name="My Service",
    services={...},
    payment_handlers={...},
    signing_keys=[UCPSigningKey.from_jwk(key.public_jwk)],
)
signed = sign_ucp_profile(profile.to_dict(), signing_key=key.private_key, kid=key.public_jwk["kid"], alg="EdDSA")
jwks = build_jwks_response([key.public_jwk])
```

`verify_ucp_profile` enforces the JWS protected header `typ='agentscore-profile+jws'` (vendor-namespaced; UCP §6 does not define a profile-as-JWS typ), restricts `alg` to `EdDSA`/`ES256`, requires a `kid`, rejects duplicate kids in the JWKS, and compares the canonical body bytes against the JWS payload to catch swap-after-sign tampering. Failures raise `UCPVerificationError` (a `ValueError` subclass) with a discriminated `code` attribute (`no_signature`/`missing_kid`/`kid_not_found`/`duplicate_kid`/`unsupported_alg`/`wrong_typ`/`signature_invalid`/`body_mismatch`/`malformed_jws`/`malformed_jwks`/`unusable_key`/`unrecognized_critical_header`).

`sign_ucp_profile` rejects profiles containing `float` values and `int` values whose magnitude exceeds `Number.MAX_SAFE_INTEGER` (2^53 - 1): cross-language float canonicalization is not stable, and Python's arbitrary-width ints lose precision when JS verifiers reparse the canonical body. Use decimal strings (e.g. `"9.99"`) for monetary or fractional fields and for any integer that may exceed the safe range.

**Persisting the private JWK.** Mint once via `generate_ucp_signing_key()`, serialize via `key.private_key.as_dict(private=True)`, store in your secret manager. On each container start, read the secret, `OKPKey.import_key(jwk_dict)` (or `ECKey.import_key` for ES256) to re-hydrate. Remote-signer flows (KMS-backed asymmetric keys) require subclassing the joserfc Key to delegate the sign hook; `OKPKey`/`ECKey` themselves only carry local key material.

**Key rotation.** Mint a new key with a new `kid`, add the public JWK to your JWKS endpoint alongside the old one, then sign new profiles with the new key. Set `Cache-Control: public, max-age=300` on `/.well-known/jwks.json` and wait at least that long after publishing the new key before removing the old JWK.

**Inline JWK in the profile vs separate JWKS endpoint.** UCP §6 mandates the separate `/.well-known/jwks.json` endpoint as the canonical trust source. The profile's `signing_keys[]` is informational; verifiers MUST resolve the kid against the JWKS to prevent a swap-after-sign attack.

ACP (Stripe + OpenAI Agentic Commerce Protocol) is a transactional checkout protocol with no identity-publishing surface; ACP merchants integrate via the existing `build_402_body` + `build_payment_headers` + Stripe SPT rail.

## Stripe multichain

```python
import os
import stripe
from agentscore_commerce.stripe_multichain import (
    CreateMultichainPaymentIntentInput,
    PiCacheOptions,
    SimulateDepositIfTestModeInput,
    create_multichain_payment_intent,
    create_pi_cache,
    simulate_deposit_if_test_mode,
)

stripe_client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])
result = create_multichain_payment_intent(CreateMultichainPaymentIntentInput(
    stripe=stripe_client,
    amount=1000,
    networks=["tempo", "base", "solana"],
    metadata={"order_id": order_id},
    idempotency_key=order_id,
))
base_address = result.deposit_addresses.get("base")
solana_address = result.deposit_addresses.get("solana")

# PI / deposit-address cache. Redis-backed when REDIS_URL is set, in-memory otherwise.
# Multi-instance deployments need Redis so a deposit lands on whichever instance settles it.
pi_cache = create_pi_cache(PiCacheOptions(redis_url=os.environ.get("REDIS_URL")))
for addr in result.deposit_addresses.values():
    await pi_cache.cache_address(addr)
    pi_cache.cache_payment_intent(addr, result.payment_intent_id)
pi_cache.cache_network_addresses(result.payment_intent_id, result.deposit_addresses)

# Testnet helper. Gates on sk_test_ and looks up the PI for you. No-op on live keys.
await simulate_deposit_if_test_mode(SimulateDepositIfTestModeInput(
    get_payment_intent_id=pi_cache.get_payment_intent_id,
    deposit_address=base_address,
    network="base",
    stripe_secret_key=os.environ["STRIPE_SECRET_KEY"],
))
```

## Build the x402 accepts entry for the 402 challenge

```python
from agentscore_commerce.payment import build_x402_accepts_for_402

x402_accepts = build_x402_accepts_for_402(
    x402_server,
    network=X402_BASE,
    price=f"${total_usd}",
    pay_to=os.environ["TREASURY_BASE_RECIPIENT"],
    max_timeout_seconds=300,
)
```

Returns a list of plain dicts ready for the 402 body's `accepts[]`. `extra.name` is derived from the registered scheme metadata so the EIP-712 domain matches the on-chain USDC contract.

## Drop-in 402 + settle (x402)

```python
from agentscore_commerce.challenge import Build402BodyInput, Respond402Input, respond_402
from agentscore_commerce.payment import (
    PaymentRequiredHeaderInput,
    ProcessX402SettleInput,
    ValidateX402NetworkConfigInput,
    VerifyX402RequestInput,
    classify_x402_settle_result,
    process_x402_settle,
    validate_x402_network_config,
    verify_x402_request,
)

# Boot-time guard. Raises if a configured network isn't supported.
validate_x402_network_config(ValidateX402NetworkConfigInput(base_network=X402_BASE))

@app.post("/purchase")
async def purchase(request: Request):
    # Path A: agent presented an x402 X-Payment header
    if request.headers.get("payment-signature") or request.headers.get("x-payment"):
        verified = await verify_x402_request(VerifyX402RequestInput(
            headers=dict(request.headers),
            is_cached_address=pi_cache.has_address,
            accepted_network=X402_BASE,
        ))
        if not verified.ok:
            return JSONResponse(verified.body, status_code=verified.status)

        settle = await process_x402_settle(ProcessX402SettleInput(
            x402_server=x402_server,
            payload=verified.payload,
            resource_config={"scheme": "exact", "network": verified.signed_network, "price": f"${total}", "payTo": verified.signed_pay_to, "maxTimeoutSeconds": 300},
            resource_meta={"url": str(request.url), "mimeType": "application/json"},
        ))
        classified = classify_x402_settle_result(settle)
        if classified is not None:
            # Log raw `settle` server-side; return controlled phase-based response to the agent.
            logger.error("x402-settle failed phase=%s raw=%r", settle.phase, settle)
            return JSONResponse(
                {"error": {"code": classified.code, "message": classified.message}, "next_steps": classified.next_steps},
                status_code=classified.status,
            )

        headers = {"payment-response": settle.payment_response_header} if settle.payment_response_header else {}
        return JSONResponse({"ok": True}, headers=headers)

    # Path B: cold call (or Authorization: Payment for pympp). After pympp.compose() returns 402,
    # respond_402 PRESERVES pympp's WWW-Authenticate and ADDS x402's PAYMENT-REQUIRED.
    result = respond_402(Respond402Input(
        mppx_challenge_headers=pympp_challenge_headers,
        body=Build402BodyInput(accepted_methods=accepted, agent_instructions=instructions, pricing=pricing, amount_usd=total, retry_body=body),
        x402=PaymentRequiredHeaderInput(x402_version=2, accepts=x402_accepts, resource={"url": str(request.url), "mimeType": "application/json"}),
    ))
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)
```

## Fail-open behavior

By default AgentScore Gate fails closed: any AgentScore-side infrastructure failure (HTTP 429, 5xx, network timeout) returns 503 to the buyer. Set `fail_open=True` on `AgentScoreGate(...)` to opt in to graceful degradation:

```python
from fastapi import Depends, FastAPI, Request
from agentscore_commerce.identity.fastapi import AgentScoreGate, get_gate_degraded_state

app = FastAPI()
gate = AgentScoreGate(api_key=os.environ["AGENTSCORE_API_KEY"], fail_open=True)

@app.post("/purchase", dependencies=[Depends(gate)])
async def purchase(request: Request):
    state = get_gate_degraded_state(request)
    if state["degraded"]:
        # Compliance was NOT enforced this request: log/alert/refund-async/etc.
        logger.warning("gate degraded: %s", state["infra_reason"])
    # ...rest of handler
```

When `fail_open=True` AND the failure is infra-shape, the gate state carries `degraded=True` + `infra_reason="quota_exceeded" | "api_error" | "network_timeout"` so merchants can log/alert without parsing console output. **Compliance denials (sanctions, age, jurisdiction, signer-mismatch) still deny regardless of `fail_open`**; `fail_open` only covers "AgentScore couldn't tell us," never "AgentScore said no."

For regulated commerce (alcohol, age-gated, sanctioned-jurisdiction-relevant) keep the default `fail_open=False`; outage is the correct posture, and bypassing compliance on infra failure is a compliance gap. For low-stakes commerce or high-uptime SLAs, opt in and use the `degraded` flag as the audit trail.

The `get_gate_degraded_state` helper is exported by every framework adapter (FastAPI, Flask, Django, AIOHTTP, Sanic, ASGI middleware) and reads from the framework-appropriate request state. The signature takes a request argument everywhere except Flask, which reads from `g` and takes no arguments.

## Examples

The [examples/](./examples) directory has 8 runnable single-file FastAPI apps covering common merchant scenarios. See [examples/README.md](./examples/README.md) for the full table.

## Stability

`agentscore-commerce` ships with the full merchant SDK surface stable. Helpers are protocol translations + configurable opinions; most evolution is additive (new optional params, new helpers, new networks/rails). Major bumps are reserved for genuine protocol-mapping bugs.

## Documentation

Full integration docs at [docs.agentscore.sh/integrations/python-commerce](https://docs.agentscore.sh/integrations/python-commerce).

## License

[MIT](LICENSE)
