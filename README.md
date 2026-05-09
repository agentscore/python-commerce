# agentscore-commerce

[![PyPI version](https://img.shields.io/pypi/v/agentscore-commerce.svg)](https://pypi.org/project/agentscore-commerce/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

The full merchant-side SDK for [AgentScore](https://agentscore.sh) in Python — agent commerce in one install. Identity middleware (FastAPI, Flask, Django, AIOHTTP, Sanic, ASGI), payment helpers, 402 challenge builders, MPP discovery, and Stripe multichain support.

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
| `agentscore_commerce.identity.{fastapi,flask,django,aiohttp,sanic,middleware}` | Trust gate middleware: KYC, sanctions, age, jurisdiction. `AgentScoreGate(...)` (or `agentscore_gate(app, ...)` on Flask/Sanic), `get_assess_data(...)`, `capture_wallet(...)`, `verify_wallet_signer_match(...)`. |
| `agentscore_commerce.identity` (package level) | Re-exports the denial helpers: `denial_reason_status`, `denial_reason_to_body`, `build_signer_mismatch_body`, `build_contact_support_next_steps`, `verification_agent_instructions`, `is_fixable_denial`, `FIXABLE_DENIAL_REASONS`. Also re-exports the per-product policy helpers: `PolicyBlock`, `GateResult`, `EnforcementMode`, `IdentityStatus`, `build_gate_from_policy`, `run_gate_with_enforcement`, `shipping_country_allowed`, `shipping_state_allowed` — for multi-product merchants where each product carries its own compliance config (hard gate vs soft vs none, per-product shipping allowlists). |
| `agentscore_commerce.payment` | `networks`, `USDC`, `rails` registries; `payment_directive`, `build_payment_directive`, `www_authenticate_header`, `payment_required_header`, `alias_amount_fields` (v1↔v2 amount field shim — emits both `amount` and `maxAmountRequired` so v1-only x402 parsers like Coinbase awal can read v2 bodies), `settlement_override_header`, `dispatch_settlement_by_network`, `extract_payment_signer` (returns `PaymentSigner({address, network})`), `register_x402_schemes_v1_v2`; drop-in x402 helpers: `validate_x402_network_config` (boot-time guard), `verify_x402_request` (parse + validate inbound X-Payment), `process_x402_settle` (verify-then-settle with one call), `classify_x402_settle_result` (maps the tagged settle result to a recommended HTTP status / code / next_steps so merchants get a controlled envelope without coupling to facilitator-specific error text). |
| `agentscore_commerce.discovery` | `is_discovery_probe_request`, `build_discovery_probe_response` (with optional `x402_sample` for x402-aware crawlers — `awal x402 details` etc.), `sample_x402_accept_for_network` (USDC sample-accept builder for known CAIP-2 networks), `build_well_known_mpp`, `build_llms_txt` + `llms_txt_identity_section` + `llms_txt_payment_section` (compact + verbose modes), `build_skill_md` (Claude-Skill-compatible `/skill.md` agent-discovery manifest — strictly agent-facing data only, no internal posture), `agentscore_openapi_snippets`, `build_bazaar_discovery_payload`, `NoindexNonDiscoveryMiddleware` (ASGI middleware that emits `X-Robots-Tag: noindex` on every path except the agent-discovery surfaces — defaults cover `/openapi.json`, `/llms.txt`, `/skill.md`, `/.well-known/{mpp.json,agent-card.json,ucp,jwks.json}`, `/favicon.{png,ico}`; pure helpers `is_discovery_path` + `DEFAULT_DISCOVERY_PATHS` for non-ASGI frameworks). |
| `agentscore_commerce.challenge` | `build_402_body`, `build_accepted_methods`, `build_identity_metadata`, `build_how_to_pay`, `build_agent_instructions` (auto-emits per-rail `compatible_clients` — smoke-verified CLIs the agent should use; vendor override supported), `build_pricing_block` (cents → dollar-string with optional shipping/tax), `first_encounter_agent_memory` (cross-merchant hint, returns the canonical block or `None` based on a per-merchant first-seen flag), `OrderReceipt` (dataclass for the post-settlement 200 response shape); `respond_402` — drop-in 402 emit that preserves pympp's `WWW-Authenticate` and layers x402's `PAYMENT-REQUIRED`. `build_validation_error` — structured 4xx body builder (`{error: {code, message}, required_fields?, example_body?, next_steps?, ...extra}`) so vendors compose body shapes by name instead of inlining at every validation site. |
| `agentscore_commerce.stripe_multichain` | `create_multichain_payment_intent`, `get_deposit_address`, `simulate_crypto_deposit`; `create_pi_cache` (TTL'd PI / deposit-address cache, Redis-backed when `redis_url` set, in-memory otherwise), `simulate_deposit_if_test_mode` (gates on `sk_test_` and looks up the PI for you), `STRIPE_TEST_TX_HASH_SUCCESS` / `STRIPE_TEST_TX_HASH_FAILED` constants. Peer dep on `stripe`. |
| `agentscore_commerce.api` | Everything from `agentscore-py` re-exported in one place: `AgentScore` + `AgentScoreError`, `AGENTSCORE_TEST_ADDRESSES` + `is_agentscore_test_address`. **Don't add `agentscore-py` as a separate dep** — the two can drift versions and cause subtle type mismatches. |

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
_gate = AgentScoreGate(
    api_key="as_live_...",
    require_kyc=True,
    min_age=21,
    allowed_jurisdictions=["US"],
)


# Run the gate CONDITIONALLY — only when a payment credential is already attached.
# Anonymous discovery (no payment header) flows through to the handler so any spec-
# compliant x402 wallet can read the 402 challenge with rails + pricing without first
# proving identity. Identity is verified at settle time on the retry leg.
async def gate_on_settle(request: Request) -> None:
    has_payment_header = bool(
        request.headers.get("payment-signature")
        or request.headers.get("x-payment")
        or (request.headers.get("authorization") or "").startswith("Payment ")
    )
    if not has_payment_header:
        return None
    return await _gate(request)


@app.post("/purchase", dependencies=[Depends(gate_on_settle)])
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
    build_a2a_agent_card,
    build_ucp_profile,
)

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

UCP §6 trust-mode requires profiles to carry a JWS signature backed by a JWKS at `/.well-known/jwks.json`. Sign + verify via the optional `joserfc` extra (tested against joserfc v1.x; pin `joserfc>=1.0.0,<2`):

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
    services=[...],
    payment_handlers=[...],
    signing_keys=[UCPSigningKey.from_jwk(key.public_jwk)],
)
signed = sign_ucp_profile(profile.to_dict(), signing_key=key.private_key, kid=key.public_jwk["kid"], alg="EdDSA")
jwks = build_jwks_response([key.public_jwk])
```

`verify_ucp_profile` enforces the JWS protected header `typ='ucp-profile+jws'`, restricts `alg` to `EdDSA`/`ES256`, requires a `kid`, rejects duplicate kids in the JWKS, and compares the canonical body bytes against the JWS payload to catch swap-after-sign tampering. Failures raise `UCPVerificationError` (a `ValueError` subclass) with a discriminated `code` attribute (`no_signature`/`missing_kid`/`kid_not_found`/`duplicate_kid`/`unsupported_alg`/`wrong_typ`/`signature_invalid`/`body_mismatch`/`malformed_jws`/`malformed_jwks`/`unusable_key`/`unrecognized_critical_header`).

`sign_ucp_profile` rejects profiles containing `float` values: cross-language float canonicalization is not stable, so use decimal strings (e.g. `"9.99"`) for any monetary or fractional fields you put in `extras`.

**Persisting the private JWK.** Mint once via `generate_ucp_signing_key()`, serialize via `key.private_key.as_dict(private=True)`, store in your secret manager. On each container start, read the secret, `OKPKey.import_key(jwk_dict)` (or `ECKey.import_key` for ES256) to re-hydrate. Remote-signer flows (KMS-backed asymmetric keys) require subclassing the joserfc Key to delegate the sign hook; `OKPKey`/`ECKey` themselves only carry local key material.

**Key rotation.** Mint a new key with a new `kid`, add the public JWK to your JWKS endpoint alongside the old one, then sign new profiles with the new key. Drop the old JWK after your verifier-side cache TTL has elapsed.

**Inline JWK in the profile vs separate JWKS endpoint.** UCP §6 mandates the separate `/.well-known/jwks.json` endpoint as the canonical trust source. The profile's `signing_keys[]` is informational; verifiers MUST resolve the kid against the JWKS to prevent a swap-after-sign attack.

ACP (Stripe + OpenAI Agentic Commerce Protocol) is a transactional checkout protocol with no identity-publishing surface — ACP merchants integrate via the existing `build_402_body` + `build_payment_headers` + Stripe SPT rail.

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
    get_deposit_address,
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
base_address = get_deposit_address(result, "base")
solana_address = get_deposit_address(result, "solana")

# PI / deposit-address cache. Redis-backed when REDIS_URL is set, in-memory otherwise —
# multi-instance deployments need Redis so a deposit lands on whichever instance settles it.
pi_cache = create_pi_cache(PiCacheOptions(redis_url=os.environ.get("REDIS_URL")))
for addr in result.deposit_addresses.values():
    await pi_cache.cache_address(addr)
    pi_cache.cache_payment_intent(addr, result.payment_intent_id)
pi_cache.cache_network_addresses(result.payment_intent_id, result.deposit_addresses)

# Testnet helper — gates on sk_test_ and looks up the PI for you. No-op on live keys.
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

# Boot-time guard — raises if a configured network isn't supported.
validate_x402_network_config(ValidateX402NetworkConfigInput(base_network=X402_BASE))

@app.post("/purchase")
async def purchase(request: Request):
    # Path A — agent presented an x402 X-Payment header
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

    # Path B — cold call (or Authorization: Payment for pympp). After pympp.compose() returns 402,
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
        # Compliance was NOT enforced this request — log/alert/refund-async/etc.
        logger.warning("gate degraded: %s", state["infra_reason"])
    # ...rest of handler
```

When `fail_open=True` AND the failure is infra-shape, the gate state carries `degraded=True` + `infra_reason="quota_exceeded" | "api_error" | "network_timeout"` so merchants can log/alert without parsing console output. **Compliance denials (sanctions, age, jurisdiction, signer-mismatch) still deny regardless of `fail_open`** — `fail_open` only covers "AgentScore couldn't tell us," never "AgentScore said no."

For regulated commerce (alcohol, age-gated, sanctioned-jurisdiction-relevant) keep the default `fail_open=False` — outage is the correct posture; bypassing compliance on infra failure is a compliance gap. For low-stakes commerce or high-uptime SLAs, opt in and use the `degraded` flag as the audit trail.

The `get_gate_degraded_state` helper is exported by every framework adapter (FastAPI, Flask, Django, AIOHTTP, Sanic, ASGI middleware) and reads from the framework-appropriate request state. The signature takes a request argument everywhere except Flask, which reads from `g` and takes no arguments.

## Examples

The [examples/](./examples) directory has 7 runnable single-file FastAPI apps covering common merchant scenarios. See [examples/README.md](./examples/README.md) for the full table.

## Stability

`agentscore-commerce@1.0.0` ships with the full merchant SDK surface stable. Helpers are protocol translations + configurable opinions — most evolution is additive (new optional params, new helpers, new networks/rails). Major bumps are reserved for genuine protocol-mapping bugs.

## Documentation

Full integration docs at [docs.agentscore.sh/integrations/python-commerce](https://docs.agentscore.sh/integrations/python-commerce).

## License

[MIT](LICENSE)
