# `agentscore-commerce` examples

Runnable, copy-pasteable example integrations covering the most common merchant scenarios. Each is a single-file FastAPI app you can adapt by swapping the relevant config.

| Example | Scenario | What it shows |
|---|---|---|
| [`identity_only.py`](./identity_only.py) | Compliance gate without payment | Minimal: wraps any endpoint with KYC + age + jurisdiction checks. Vendor handles their own payment. |
| [`api_provider.py`](./api_provider.py) | API provider (Exa-style) | Per-call billing on multiple rails: Tempo MPP + x402 (Base + Solana), all driven by `Checkout`. No identity gate. Demos `Checkout(discovery_probe=...)` for x402-crawler auto-routing, `build_merchant_index_json` + `standard_endpoint_descriptions(kind="api")` for `GET /` discovery, and `build_redemption_skill_md` with the trial-credit body shape on `GET /redemption.md`. |
| [`multi_rail_merchant.py`](./multi_rail_merchant.py) | Full agent-commerce merchant | Identity gate + Tempo MPP + x402 (Base + Solana) + Stripe SPT via `Checkout`. Demos `pricing_result` (cents → typed PricingResult), `Receipt` + `ReceiptNextSteps` + `build_success_next_steps` in `on_settled`, per-order Stripe-multichain deposit minting via `mint_recipients`, and `simulate_deposit_if_test_mode`. |
| [`stripe_multichain_merchant.py`](./stripe_multichain_merchant.py) | Stripe-anchored multi-chain | Stripe PaymentIntent with deposit_options for tempo/base/solana; crypto deposits flow through Stripe. Read `result.deposit_addresses[network]` directly. Includes testnet `simulate_crypto_deposit` helper. For low-margin endpoints (sub-dollar APIs), use `create_pay_to_address_from_stripe_pi` / `mint_multichain_recipients` with `static_recipients={"solana": "<wallet>"}` — see the `stripe_multichain` row in the main `README.md` for the full pattern and economics. |
| [`compute_first_merchant.py`](./compute_first_merchant.py) | Pay-per-result variable cost (LLM, transcode, per-token/byte) | The probe leg runs the work server-side, caches the result by body content-hash, and emits a 402 at the **exact** computed price; the retry pays that price and gets the cached result. Exact-mode rails only (x402-exact Base, plus Tempo/Solana/Stripe SPT via a `compose_mppx` callback) — deliberately scoped out of x402-upto (Permit2) and Settlement-Overrides. Uses `compute_first_checkout` + `create_quote_cache`; pairs with `rate_limit_fastapi` since the probe leg runs work pre-payment. |
| [`compliance_merchant.py`](./compliance_merchant.py) | Regulated-goods merchant (wine, cannabis, etc.) | Full compliance gate via `Checkout(gate=CheckoutGateConfig(...))` + custom `on_denied` composing commerce helpers: `verification_agent_instructions`, `is_fixable_denial`, `build_contact_support_next_steps`, `denial_reason_to_body`/`denial_reason_status`. Shows how vendors write only the business-specific denial branches and let commerce handle the rest. |
| [`per_product_policy_merchant.py`](./per_product_policy_merchant.py) | Multi-product merchant with mixed compliance needs | One product carries a hard gate (wine: KYC + 21 + US-state allowlist), another has no gate at all (anonymous merch, ships anywhere), a third uses `enforcement="soft"` (request KYC as a fraud signal but accept anonymous sales, stamping `identity_status="unverified"` on the order). Uses `PolicyBlock`, the one-call `validate_shipping_against_policy`, and `Checkout(gate=CheckoutGateConfig(per_request_policy=...))`. |
| [`signed_ucp_merchant.py`](./signed_ucp_merchant.py) | Signed UCP profile + JWKS endpoint | One-call mount via `checkout.mount_ucp_routes_fastapi(app, ...)` registers `/.well-known/ucp` + `/.well-known/jwks.json` + the OPTIONS preflights. AgentScore's `agentscore-profile+jws` is a vendor extension for trust-mode verifiers (regulated-commerce, AP2-aware) that opt into auditable profiles; UCP §6 itself does NOT mandate signing. Wires ephemeral-for-dev / env-JWK-for-prod and `bootstrap_ucp_signing_key` lifespan-hook usage. |

## How to use

1. Pick the scenario closest to yours
2. Copy the file into your project
3. Install peer deps mentioned at the top of the file (only what you actually need)
4. Set the env vars listed at the top of the file
5. Run with `uvicorn examples.<name>:app --port 3000`
6. Iterate; these are templates, not frameworks

## Patterns

All eight examples follow the same rough shape:

1. **Boot:** instantiate FastAPI, identity gate (if any), Stripe / facilitator clients (if any) via commerce factories
2. **Discovery routes:** `/openapi.json` + `/.well-known/mpp.json` + `/llms.txt` (omitted in these focused examples; see node-commerce for the discovery wiring)
3. **Per-request:** identity gate → validate body → 402 challenge (built via commerce/challenge helpers) → settle payment → return result

AgentScore Commerce keeps every step ~5–15 lines instead of ~50–150 lines. Vendors compose; the SDK wraps the protocol-correctness boilerplate.

## What stays vendor-specific

These examples are intentionally thin on domain logic. Vendors plug in their own:

- Catalog / product / pricing data
- Order storage (Postgres, durable queue, etc.)
- Customer email / fulfillment notifications
- Tax / shipping calculators
- Frontend UI (none of these examples include one; they're agent-only APIs)

AgentScore Commerce handles the agent commerce protocol layer; everything else is your business.

## Differences from node-commerce examples

Python wraps `x402[evm]` and `pympp[server,tempo,stripe]` as peer deps; `@solana/mpp` has no Python equivalent today. Two implications:

1. **`extract_payment_signer` returns EVM only.** Solana SPL Token payer recovery requires a Solana SDK (`solders` / `solana-py`) which isn't bundled. Custom adapters that wire Solana signer recovery should pass `signer={address, network}` directly to `AgentScoreCore.acheck()`; the API returns the wallet-binding + sanctions verdicts on the same response.
2. **No streaming/session-payment example.** There's no pip-installable `mppx` equivalent, so Python doesn't ship a tempo MPP session (channel + SSE + mid-stream voucher) implementation. For variable-cost billing, `compute_first_merchant.py` uses the exact-mode compute-first pattern instead (probe runs the work, 402 at the exact computed price). Vendors who need true streamed/session payments should check the [tempo session protocol docs](https://mpp.dev/guides/streamed-payments) and bind to a wallet library directly.

For Python merchants on x402 alone (Base or Solana), every helper (`create_x402_server`, `create_mppx_server`, directives, headers, dispatch, settle-overrides, signer extraction for EVM, accepted_methods, agent_instructions, how_to_pay) is fully native.
