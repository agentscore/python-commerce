# `agentscore-commerce` examples

Runnable, copy-pasteable example integrations covering the most common merchant scenarios. Each is a single-file FastAPI app you can adapt by swapping the relevant config.

| Example | Scenario | What it shows |
|---|---|---|
| [`identity_only.py`](./identity_only.py) | Compliance gate without payment | Minimal: wraps any endpoint with KYC + age + jurisdiction checks. Vendor handles their own payment. |
| [`api_provider.py`](./api_provider.py) | API provider (Exa-style) | Per-call billing on multiple rails: Tempo MPP + x402 (Base + Solana). Discovery probe responder + multi-rail 402 challenge. No identity gate. |
| [`multi_rail_merchant.py`](./multi_rail_merchant.py) | Full agent-commerce merchant | Identity gate + Tempo MPP + x402 (Base + Solana) + Stripe SPT, all rails accepted, full 402 builder using `build_402_body` + `build_accepted_methods` + `build_how_to_pay` + `build_agent_instructions`. |
| [`stripe_multichain_merchant.py`](./stripe_multichain_merchant.py) | Stripe-anchored multi-chain | Stripe PaymentIntent with deposit_options for tempo/base/solana; crypto deposits flow through Stripe. Includes testnet `simulate_crypto_deposit` helper. |
| [`variable_cost_merchant.py`](./variable_cost_merchant.py) | Pay-per-actual-usage (LLM, transcode, etc.) | Same use case on **two protocols**: x402 upto (Permit2 authorize-max → `Settlement-Overrides` settle-actual) AND MPP tempo session (channel + SSE + mid-stream vouchers). Vendor offers both. |
| [`compliance_merchant.py`](./compliance_merchant.py) | Regulated-goods merchant (wine, cannabis, etc.) | Full compliance gate + custom `on_denied` composing commerce helpers: `verification_agent_instructions`, `is_fixable_denial`, `build_contact_support_next_steps`, `denial_reason_to_body`/`denial_reason_status`, `build_signer_mismatch_body`. Shows how vendors write only the business-specific branches and let commerce handle the rest. |
| [`per_product_policy_merchant.py`](./per_product_policy_merchant.py) | Multi-product merchant with mixed compliance needs | One product carries a hard gate (wine: KYC + 21 + US-state allowlist), another has no gate at all (anonymous merch, ships anywhere), a third uses `enforcement="soft"` (request KYC as a fraud signal but accept anonymous sales, stamping `identity_status="unverified"` on the order). Uses `PolicyBlock`, `build_gate_from_policy`, `run_gate_with_enforcement`, `shipping_country_allowed`, `shipping_state_allowed`. |

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
2. **Streaming session payments (variable_cost_merchant.py)** sketches the protocol but doesn't ship a working tempo session implementation; there's no pip-installable `mppx` equivalent. The example shows the response shape; vendors using session payments today should check the [tempo session protocol docs](https://mpp.dev/guides/streamed-payments) and bind to a Solana wallet library directly.

For Python merchants on x402 alone (Base or Solana), every helper (`create_x402_server`, `create_mppx_server`, directives, headers, dispatch, settle-overrides, signer extraction for EVM, accepted_methods, agent_instructions, how_to_pay) is fully native.
