# agentscore-commerce

Agent commerce SDK for Python. The full merchant-side toolkit: identity gating + payment helpers + 402 builders + discovery + Stripe multichain. One install, submodule imports per concern.

Every helper is extracted from a real consumer, not speculated.

## Submodules

| Submodule | What it is |
|---|---|
| `agentscore_commerce` (top-level) | `Checkout` orchestrator (the 2.0 high-level surface): one config object + hooks (pre_validate, compute_pricing, mint_recipients, compose_mppx, on_settled, gate), auto-derived x402+pympp servers, per-framework adapters `handle_fastapi`/`handle_flask`/`handle_django`/`handle_aiohttp`/`handle_sanic`, signed UCP routes via `mount_ucp_routes_{fastapi,flask,django,aiohttp,sanic}`, optional `discovery_probe` config for x402-crawler auto-routing. Plus factories: `pricing_result` (cents → typed `PricingResult`), `validation_response_{fastapi,flask,django,aiohttp,sanic}` (4xx envelope per framework), `Receipt`/`ReceiptNextSteps`/`ProductInfo`/`ShippingAddress` (canonical 200-receipt dataclasses, universal across goods + API merchants) |
| `agentscore_commerce.identity.{fastapi,flask,django,aiohttp,sanic,middleware}` | Trust gate middleware (KYC, age, sanctions on both account name and signer wallet, jurisdiction) |
| `agentscore_commerce.identity.policy` | Per-product compliance helpers: `PolicyBlock`, `build_gate_from_policy`, `run_gate_with_enforcement`, `shipping_country_allowed`, `shipping_state_allowed`, `validate_shipping_against_policy` (one-call country+state validator that raises `CheckoutValidationError` with the canonical envelope on miss) |
| `agentscore_commerce.payment` | Networks/USDC/rails registries, paymentauth.org directive builders, `create_x402_server` (wraps `x402[evm]>=2.9` + `cdp-sdk` for `facilitator="coinbase"`; install via the `coinbase` extra), `build_x402_accepts_for_402` (build the 402's `accepts[]` from the registered scheme; derives the right `extra.name` per network), `process_x402_settle` (verify+settle in one call), `create_mppx_server` (wraps `pympp[server,tempo,stripe]>=0.6`), dispatch-by-network, signer extraction, WWW-Authenticate header, Settlement-Overrides header |
| `agentscore_commerce.discovery` | Discovery probe (`is_discovery_probe_request`, `build_discovery_probe_response`), Bazaar wrapper, `/.well-known/mpp.json`, `llms.txt` builder, `skill.md` builder (Claude-Skill-compatible agent-discovery manifest), `build_redemption_skill_md` (delivery-neutral; printed/emailed/API-trial codes all covered via `delivery_intro`/`body_shape`/`body_rules`/`extra_recovery_rows` overrides), `build_merchant_index_json` + `standard_endpoint_descriptions(kind=)` (canonical `/` discovery body for goods or API merchants), `build_success_next_steps` (universal Passport-active success block), `build_agentscore_onboarding_steps`, OpenAPI snippets, `NoindexNonDiscoveryMiddleware` ASGI middleware. Plus the UCP/JWKS publish surface: `build_signed_ucp_response`, `build_signed_jwks_response`, `well_known_preflight_response`, `default_a2a_services`, `bootstrap_ucp_signing_key`, framework-neutral `SignedDiscoveryResponse` + per-framework wrappers `signed_response_{fastapi,flask,django,aiohttp,sanic}` |
| `agentscore_commerce.challenge` | 402-body builders: accepted_methods, identity_metadata (auto-attached by `Checkout` when wallet header present), how_to_pay, agent_instructions, build_402_body, pricing, agent_memory, `build_validation_error` (4xx body builder), `Receipt`/`ReceiptNextSteps`/`ProductInfo`/`ShippingAddress` (canonical 200-receipt dataclasses) |
| `agentscore_commerce.stripe_multichain` | Multichain PaymentIntent helper (`create_multichain_payment_intent` returns `MultichainPaymentIntentResult`; read `result.deposit_addresses[network]` directly), testnet simulator (`simulate_crypto_deposit`, `simulate_deposit_if_test_mode`), `create_pi_cache`, `create_mppx_stripe` |
| `agentscore_commerce.api` | Re-exports `AgentScore` from `agentscore` SDK |

## Architecture

Single Python package, hatchling-built, published to PyPI as `agentscore-commerce`. Per-framework identity adapters expose the same surface (`AgentScoreGate`, or `agentscore_gate(app, ...)` for Flask/Sanic; `capture_wallet`, `get_signer_verdict`, `get_agentscore_data`, `get_gate_degraded_state`, `get_gate_quota_info`) with network-aware address normalization (EVM lowercased, Solana base58 preserved verbatim). The gate middleware extracts the inbound payment signer pre-evaluate (`extract_payment_signer(x402_header)`) and passes it to `/v1/assess` via the SDK's `signer` kwarg, so the API composes both wallet-binding (`signer_match`) and OFAC SDN wallet-address (`signer_sanctions`) verdicts on one round trip; merchants read both back synchronously via `get_signer_verdict(request)` off the gate's cache.

| Directory | Contents |
|---|---|
| `agentscore_commerce/identity/` | Per-framework gate adapters (fastapi, flask, django, aiohttp, sanic, middleware/ASGI), shared client, sessions, address normalization, signer extraction, response marshalling, types, cache |
| `agentscore_commerce/payment/` | Payment-protocol helpers |
| `agentscore_commerce/discovery/` | Discovery probe + machine-readable docs |
| `agentscore_commerce/challenge/` | 402-body builders |
| `agentscore_commerce/stripe_multichain/` | Stripe multichain PaymentIntent helpers |
| `agentscore_commerce/api/` | `AgentScore` re-export |
| `examples/` | Runnable single-file FastAPI apps for each common scenario |
| `tests/` | pytest, one file per surface |

Peer-dep pattern: payment/x402/mppx/stripe modules import lazily at runtime; vendors install only what they use via extras (`pip install agentscore-commerce[fastapi,stripe]` etc.). Underlying packages: `x402[evm]`, `pympp[server,tempo,stripe]`, `stripe`, `cdp-sdk` (the `coinbase` extra; only needed when `facilitator="coinbase"`). Missing peer dep raises a guiding `ImportError` with the install command.

## Examples

`examples/` contains full single-file FastAPI apps for the most common merchant scenarios; copy-paste templates, not frameworks:

| Example | Scenario |
|---|---|
| `api_provider.py` | Per-call API billing on multiple rails: Tempo MPP + x402 Base + Solana MPP; no compliance gate |
| `identity_only.py` | Compliance gate without payment (vendor handles their own) |
| `multi_rail_merchant.py` | Full agent-commerce: identity + Tempo MPP + x402 + Stripe SPT |
| `stripe_multichain_merchant.py` | Stripe-anchored multichain (PaymentIntent → tempo/base/solana deposit addresses) |
| `variable_cost_merchant.py` | Pay-per-actual-usage on **two protocols**: x402 upto (Permit2 + Settlement-Overrides) AND MPP tempo session (channel + SSE + mid-stream vouchers) |
| `compliance_merchant.py` | Regulated-goods merchant: full compliance gate + custom `on_denied` composing the denial helpers (`verification_agent_instructions`, `is_fixable_denial`, `build_signer_mismatch_body`, `build_contact_support_next_steps`, `denial_reason_to_body`/`denial_reason_status`) |
| `per_product_policy_merchant.py` | Multi-product merchant where each row carries its own compliance policy. One product hard-gates KYC + age + state; another is anonymous; a third uses `enforcement="soft"` (request KYC but don't block sale). Demonstrates `PolicyBlock`, `build_gate_from_policy`, `run_gate_with_enforcement`, `shipping_country_allowed`, `shipping_state_allowed`. |
| `signed_ucp_merchant.py` | Signed UCP profile (`/.well-known/ucp`) + JWKS endpoint (`/.well-known/jwks.json`). AgentScore's `agentscore-profile+jws` is a vendor extension on top of UCP for trust-mode verifiers (regulated-commerce, AP2-aware) that opt into auditable cryptographic provenance — UCP §6 itself does NOT mandate signing; production UCP merchants commonly ship unsigned. Wires ephemeral-for-dev / env-JWK-for-prod signing, kid rotation, and `Cache-Control` posture. Uses `generate_ucp_signing_key`, `sign_ucp_profile`, `build_jwks_response`, `UCPSigningKey.from_jwk`, `UCPVerificationError`. Demonstrates the payment-handler builders (`mpp_payment_handler`, `x402_payment_handler`, `stripe_spt_payment_handler` — see "Payment-handler builders" below). |

## Payment-handler builders

The SDK ships protocol-rooted builders for the AgentScore-published payment handlers — vendors compose UCP `payment_handlers` blocks by spreading these helpers instead of hand-writing the verbose binding wrapper:

```python
from agentscore_commerce.identity import (
    build_ucp_profile,
    mpp_payment_handler,
    x402_payment_handler,
    stripe_spt_payment_handler,
)

build_ucp_profile(
    ...,
    payment_handlers={
        **mpp_payment_handler(networks=[{"network": "tempo-mainnet", "chain_id": 4217, "recipient": "0x..."}]),
        **x402_payment_handler(networks=[{"network": "base-8453", "recipient": "0x..."}]),
        **stripe_spt_payment_handler(profile_id="profile_..."),
    },
)
```

Each helper returns `{ <reverse-DNS-key>: [binding] }` so spreading composes the parent map. The handler `version`, spec URL, and schema URL are owned by the helpers (`agentscore_commerce/identity/ucp.py`) — bumping a handler spec version is a one-line change there. `mpp` + `x402` share the same `networks: [{network, recipient?, ...extras}]` config shape so consumers parse both identically. `recipient` is optional — omit when the merchant uses per-order recipients (e.g. Stripe-derived deposit addresses); the authoritative recipient still ships in the 402 body.

## Identity model

Two identity types: wallet (`X-Wallet-Address`) and operator-token (`X-Operator-Token`). Default checks operator-token first, then wallet. Address normalization is network-aware via `agentscore_commerce/identity/address.py`: EVM lowercased, Solana base58 preserved verbatim. Used for cache keys, wallet→operator resolves, and signer-match comparisons.

`DenialReason` codes (`missing_identity`, `identity_verification_required`, `token_expired`, `invalid_credential`, `wallet_signer_mismatch`, `wallet_auth_requires_wallet_signing`, `wallet_not_trusted`, `api_error`, `payment_required`) each carry a structured `agent_instructions` JSON block describing concrete recovery actions. See `agentscore_commerce/identity/_response.py` for the canned action copy.

`create_session_on_missing` auto-mints a verification session when no identity is present and returns 403 with `verify_url` + poll instructions. `get_signer_verdict(request)` (per-adapter) returns the cached `signer_match` + `signer_sanctions` verdicts the gate composed on its primary `/v1/assess` call (single round trip; merchants build a 403 with `build_signer_mismatch_body(result=verdict.signer_match)` when `kind != "pass"`).

Captured wallets: `capture_wallet(...)` is fire-and-forget. Reads `operator_token` stashed during gating and POSTs to `/v1/credentials/wallets`. No-ops for wallet-authenticated requests.

Wallet-signer-match + signer-sanctions: the gate adapter calls `extract_payment_signer(x402_header)` pre-evaluate and passes `signer={address, network}` to the SDK's `assess`. The API returns both `signer_match` (wallet-binding) and `signer_sanctions` (OFAC SDN wallet-address) on the same response; commerce caches the raw body alongside the projected verdicts so `get_signer_verdict` is a pure cache read. Under `policy.require_sanctions_clear`, an OFAC SDN signer hit already flips `decision -> deny` before the handler runs.

### Fail-open (opt-in)

`fail_open=True` on `AgentScoreGate(...)` (or `agentscore_gate(app, ...)`) flips infra-failure handling: 429 / 5xx / network-timeout pass through to the handler with the gate state stamped `degraded=True` + `infra_reason="quota_exceeded" | "api_error" | "network_timeout"`. `get_gate_degraded_state(request)` (Flask: `get_gate_degraded_state()`, reads from `g`) returns `{"degraded": bool, "infra_reason"?: str}` for merchant logging/alerting. Default stays `fail_open=False`; regulated commerce should keep it. Compliance denials (sanctions, age, jurisdiction, signer-mismatch) still deny regardless of the flag. The gate's `try` wraps only the AgentScore call, never the downstream user handler.

### Mount posture: gate-first vs gate-conditional

`AgentScoreGate(...)` (or `agentscore_gate(app, ...)` on Flask/Sanic) is mounted directly when the route is AgentScore-only; every request runs identity + policy. To support **anonymous discovery by any spec-compliant x402 wallet** (Coinbase awal, Phantom, Solflare, ...), wrap the gate so it fires only when a payment credential is attached:

```python
_gate = AgentScoreGate(api_key=..., require_kyc=True, ...)

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
async def purchase(...): ...
```

Anonymous POST flows through to the handler unauthenticated and gets a 402 with all rails + per-order pricing. Identity is verified at settle time on the retry leg (when the agent submits `X-Payment` / `Authorization: Payment`); `create_session_on_missing` still auto-mints a verification session there. The same wrap pattern works identically across all 6 framework adapters (fastapi, flask, django, aiohttp, sanic, middleware/ASGI). See `examples/multi_rail_merchant.py` and `examples/compliance_merchant.py`.

### `compatible_clients` field on emitted 402s

`build_agent_instructions` emits a `compatible_clients` field in the 402 body, derived automatically from `how_to_pay`: per-rail list of CLIs the AgentScore team has smoke-verified end-to-end. Vendors override with `BuildAgentInstructionsInput(compatible_clients={...})` to add their own tested clients. Set to an empty dict `{}` to suppress the default. Same data is published as `core/docs/integrations/x402-clients.mdx` for human-side rationale + per-rail commands.

## Tooling

- **uv**: package manager.
- **ruff**: linting + formatting.
- **ty**: type checker (Astral).
- **vulture**: dead code detection.
- **pytest**: tests.
- **Lefthook**: pre-commit ruff, pre-push ty + vulture (parallel).

```bash
uv sync --all-extras
uv run lefthook install   # one-time per clone; wires pre-commit + pre-push
uv run ruff check .
uv run ruff format .
uv run ty check agentscore_commerce/
uv run pytest tests/
```

## Workflow

1. Create a branch
2. Make changes
3. Lefthook runs ruff on commit, ty + vulture on push
4. Open a PR (CI runs automatically)
5. Merge (squash)

## Rules

- **No silent refactors**
- **Never commit .env files or secrets**
- **Use PRs**: never push directly to main
- **Helpers are protocol translations + configurable opinions, not opinionated frameworks**
- **Cross-language API parity**: keep the surface area identical between the node and python flavors so vendors switching languages have the same mental model

## Releasing

1. Update `version` in `pyproject.toml`
2. Commit: `git commit -am "chore: bump to vX.Y.Z"`
3. Tag: `git tag vX.Y.Z`
4. Push: `git push && git push origin vX.Y.Z`

The publish workflow runs on `ubuntu-latest` (required for PyPI trusted publishing), builds, publishes to PyPI via OIDC, and creates a GitHub Release.
