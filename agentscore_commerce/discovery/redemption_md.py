"""Standard ``/redemption.md`` template for merchants offering printed-mailer redemption codes.

Renders the canonical cold-start bootstrap section + TL;DR + recovery table +
body/code rules. Merchants supply only the merchant-specific bits (name, URL,
SKU intro, peer-merchant pointer) and the rest comes from this template.

Mirrors the prose every AgentScore merchant otherwise hand-writes so agents
encounter the same shape of redemption flow at any merchant.
"""

# ruff: noqa: E501

from __future__ import annotations


def build_redemption_skill_md(
    *,
    merchant_name: str,
    app_url: str,
    sku_intro: str | None = None,
    peer_merchant_pointer: str | None = None,
) -> str:
    """Render the canonical ``redemption.md`` for an AgentScore merchant.

    ``sku_intro`` is one paragraph describing what the code unlocks at this
    merchant (e.g. "a wine SKU you'll find in /catalog with purchase_mode =
    redemption_only"). Defaults to a generic placeholder.

    ``peer_merchant_pointer`` is the optional "Don't have a code?" cross-link
    at the bottom; a URL or short markdown line pointing at the merchant-name's
    sister non-code-only store. Omit to drop the section.
    """
    sku_text = sku_intro or (
        "The code redeems a product at this merchant which you'll find in "
        "/catalog with `purchase_mode = redemption_only`."
    )

    peer_section = ""
    if peer_merchant_pointer:
        peer_section = (
            "\n## Don't have a code?\n\n"
            "This page is the redemption flow for printed-mailer codes. "
            f"If you're looking to buy without a code, see: {peer_merchant_pointer}\n"
        )

    return f"""# Redeeming an AgentScore mailer code at {merchant_name}

You're reading this because the human you're working for received a printed
AgentScore mailer with a single-use redemption code. This page tells you, the
agent, exactly how to turn that code into a shipped order.

{sku_text} The 402 challenge on /purchase tells you the actual settle amount
after the code is applied; discounts can range from a partial amount off list
down to free.

## Cold-start bootstrap (skip if your wallet + Passport are already set up)

If `agentscore-pay` isn't installed yet, install it (`npm i -g @agent-score/pay`
or `brew install agentscore/tap/agentscore-pay`), then run `agentscore-pay
agent-guide --json` for the canonical cold-start path. That walks
`agentscore-pay init` (creates keystore + per-chain wallet),
`agentscore-pay passport login` (one-time KYC; opens a verify URL the human
completes, after which pay caches the `operator_token`), and
`agentscore-pay balance` to confirm funds. Fund enough to cover the
post-discount settle amount the 402 advertises; for $0 codes the merchant
skips the on-chain settle entirely so funds aren't required, but the wallet
still needs to exist so the credential can be signed.

You don't have to use `agentscore-pay` specifically; any spec-compliant client
for the merchant's accepted rails (Tempo MPP, x402 Base, Solana MPP, Stripe SPT)
works. The 402 challenge lists every accepted rail in `accepted_methods`.

## TL;DR

1. Ask the user for their redemption code, email, and US shipping address.
2. `GET {app_url}/catalog`; find the product whose `purchase_mode` is
   `redemption_only`. Read its `purchase_note` for any product-specific rules.
3. `POST {app_url}/purchase` with body:
   ```json
   {{
     "product_slug": "<slug>",
     "redemption_code": "<code>",
     "email": "user@example.com",
     "shipping": {{ "name": "...", "address_1": "...", "city": "...", "state": "CA", "zip": "94573" }}
   }}
   ```
4. If you get **403 `operator_verification_required`**, surface the body's
   `verify_url` to the user for one-time KYC and poll `poll_url` with
   `poll_secret`. After verification, retry with `X-Operator-Token` attached.
   If you already have an `opc_...` from a prior AgentScore-gated merchant,
   attach it on the first call and skip this step.
5. On **402**, the body carries `accepted_methods` and `agent_instructions.how_to_pay`.
   Settle with `agentscore-pay pay POST {app_url}/purchase --chain <rail> -d '<body>'
   --max-spend <amount>`; pay handles 402 retry, rail selection, signing, and
   Passport attachment. Pass `--max-spend` ≥ the amount in the 402.
6. **200**; order confirmed. Response carries `order.id`, `next_steps.order_status_url`,
   and an `agent_memory` block you should persist (the cross-merchant pattern hint,
   NOT the operator_token or poll_secret). For $0 redemptions `tx_hash` is `null`;
   the credential is still authenticated and the code is burned single-use.

## Body rules

- `quantity` is fixed at 1; one product per code.
- `shipping.country` defaults to `"US"`; non-US shipping is rejected for
  redemption-eligible products.
- `shipping.state` must be a 2-letter US state code; `unsupported_jurisdiction`
  400 if the state isn't on the merchant's allowlist.
- `email` must be valid; FastAPI returns 422 on malformed input.

## Code rules

- Codes are case-insensitive (server uppercases on receipt), single-use, and
  burned atomically against `(code, operator_token)` OR `(code, signer_address)`
  for token-less wallet flows. A second attempt returns 400 `redemption_already_used`.
- Submit the code in the JSON body as `"redemption_code"`; never as a header.

## Recovery on common errors

| HTTP | error.code | What it means | What to do |
|---|---|---|---|
| 403 | `operator_verification_required` | User has no Passport / KYC pending | Surface `verify_url`; poll `poll_url` with `poll_secret`; retry with `X-Operator-Token` |
| 403 | `wallet_signer_mismatch` | Operator token + signer wallet aren't linked to the same identity | Switch to a wallet in `linked_wallets[]`, or drop the operator_token to re-KYC the new wallet |
| 400 | `invalid_body` | JSON parse failed | Fix the JSON and retry |
| 400 | `missing_fields` | Required field absent | Add the field per `error.message` and retry |
| 400 | `product_not_found` | `product_slug` doesn't match an active product | Re-check `/catalog` and use the exact slug |
| 400 | `product_out_of_stock` | Product real but stock 0 | Tell the user; no retry possible |
| 400 | `invalid_redemption_code` | Code unknown / expired | Ask the user for the code as printed; do not invent variants |
| 400 | `redemption_already_used` | Code burned | Tell the user; codes are single-use |
| 400 | `codes_not_accepted` | Product is `paid_only` and rejects codes | Drop `redemption_code` and retry, or pick a different product |
| 400 | `unsupported_jurisdiction` | Shipping state not on allowlist | Ask for an allowed shipping address |
| 402 | (challenge) | Identity OK; payment required | Run `agentscore-pay pay` against the same URL |
{peer_section}"""


__all__ = ["build_redemption_skill_md"]
