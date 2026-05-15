"""Standard ``/redemption.md`` template for merchants offering redemption codes.

Renders the canonical cold-start bootstrap + TL;DR + recovery table + body /
code rules for any merchant that accepts single-use codes against a paid
endpoint. The pattern is delivery-neutral: codes can be printed on a mailer,
emailed, surfaced in-app, or issued as API trial credits.

Goods merchants get the default body-shape (product_slug + shipping + email).
API merchants or digital-credit issuers pass ``body_shape`` to override the
JSON example, and ``extra_recovery_rows`` to add merchant-specific error rows.

Mirrors the prose every AgentScore merchant otherwise hand-writes so agents
encounter the same shape of redemption flow at any merchant.
"""

# ruff: noqa: E501

from __future__ import annotations

_DEFAULT_BODY_SHAPE = """{
     "product_slug": "<slug>",
     "redemption_code": "<code>",
     "email": "user@example.com",
     "shipping": { "name": "...", "address_1": "...", "city": "...", "state": "CA", "zip": "94573" }
   }"""

_DEFAULT_BODY_RULES = """## Body rules

- `quantity` is fixed at 1; one product per code.
- `shipping.country` defaults to `"US"`; non-US shipping is rejected for
  redemption-eligible products.
- `shipping.state` must be a 2-letter US state code; `unsupported_jurisdiction`
  400 if the state isn't on the merchant's allowlist.
- `email` must be valid; the server returns 422 on malformed input."""


def build_redemption_skill_md(
    *,
    merchant_name: str,
    app_url: str,
    endpoint_path: str = "/purchase",
    sku_intro: str | None = None,
    delivery_intro: str | None = None,
    body_shape: str | None = None,
    body_rules: str | None = None,
    extra_recovery_rows: str | None = None,
    peer_merchant_pointer: str | None = None,
) -> str:
    """Render the canonical ``redemption.md`` for an AgentScore merchant.

    ``endpoint_path`` is the merchant's redemption endpoint relative to
    ``app_url``. Defaults to ``"/purchase"`` for goods merchants; API merchants
    typically pass ``"/<endpoint>"`` (the per-call paid route that accepts a
    ``redemption_code`` field in the body).

    ``delivery_intro`` overrides the cold-start paragraph describing how the
    code was distributed. Default copy covers printed mailers, emails, and any
    other out-of-band delivery channel. API merchants distributing trial credits
    might override with vendor-specific language.

    ``body_shape`` is the JSON example shown in the TL;DR. Defaults to the
    goods-merchant shape (product_slug + redemption_code + email + shipping).
    API merchants pass their endpoint's body shape (which still includes
    ``redemption_code``).

    ``body_rules`` overrides the body-rules section. Default covers
    goods-shipping rules; API merchants typically pass either ``""`` (drop the
    section) or their own constraints.

    ``extra_recovery_rows`` is appended verbatim to the recovery table after
    the universal rows. Use it for merchant-specific error codes (e.g.
    ``unsupported_jurisdiction`` rows, per-tier code rules).

    ``sku_intro`` is one paragraph describing what the code unlocks at this
    merchant. Defaults to a generic placeholder.

    ``peer_merchant_pointer`` is the optional "Don't have a code?" cross-link
    at the bottom. Omit to drop the section.
    """
    sku_text = sku_intro or (
        "The code redeems a product or paid call at this merchant which you'll find in "
        "the merchant's catalog or per-endpoint documentation with `purchase_mode = redemption_only`."
    )
    delivery_text = delivery_intro or (
        f"You're reading this because the user you're working for received a single-use "
        f"redemption code from {merchant_name} (printed on a mailer, emailed, surfaced in-app, "
        "or distributed out-of-band). This page tells you, the agent, exactly how to turn "
        "that code into a settled call."
    )
    rendered_body_shape = body_shape or _DEFAULT_BODY_SHAPE
    rendered_body_rules = body_rules if body_rules is not None else _DEFAULT_BODY_RULES

    peer_section = ""
    if peer_merchant_pointer:
        peer_section = (
            "\n## Don't have a code?\n\n"
            "This page is the redemption flow for single-use codes. "
            f"If you're looking to buy or call without a code, see: {peer_merchant_pointer}\n"
        )

    body_rules_section = f"\n{rendered_body_rules}\n" if rendered_body_rules else ""
    extra_rows = f"\n{extra_recovery_rows.rstrip()}" if extra_recovery_rows else ""

    return f"""# Redeeming an AgentScore code at {merchant_name}

{delivery_text}

{sku_text} The 402 challenge on {endpoint_path} tells you the actual settle amount
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

1. Ask the user for their redemption code, plus any merchant-specific fields the
   body requires (email, shipping address for goods merchants, identifiers for
   API merchants, etc.).
2. Discover the redemption-eligible target. Goods merchants: `GET {app_url}/catalog`
   and find the product whose `purchase_mode` is `redemption_only`. API merchants:
   read the per-endpoint docs for the route that accepts `redemption_code`.
   Read any `purchase_note` for product-specific rules.
3. `POST {app_url}{endpoint_path}` with body:
   ```json
   {rendered_body_shape}
   ```
4. If you get **403 `operator_verification_required`**, surface the body's
   `verify_url` to the user for one-time KYC and poll `poll_url` with
   `poll_secret`. After verification, retry with `X-Operator-Token` attached.
   If you already have an `opc_...` from a prior AgentScore-gated merchant,
   attach it on the first call and skip this step.
5. On **402**, the body carries `accepted_methods` and `agent_instructions.how_to_pay`.
   Settle with `agentscore-pay pay POST {app_url}{endpoint_path} --chain <rail> -d '<body>'
   --max-spend <amount>`; pay handles 402 retry, rail selection, signing, and
   Passport attachment. Pass `--max-spend` ≥ the amount in the 402.
6. **200**; the call settled. Response carries a receipt `id` (order id for goods,
   request id for API), `next_steps.order_status_url` (or usage dashboard URL),
   and an `agent_memory` block you should persist (the cross-merchant pattern
   hint, NOT the operator_token or poll_secret). For $0 redemptions `tx_hash`
   is `null`; the credential is still authenticated and the code is burned
   single-use.
{body_rules_section}
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
| 400 | `product_not_found` | Identifier doesn't match an active product / endpoint | Re-check the catalog or endpoint docs and use the exact slug / route |
| 400 | `product_out_of_stock` | Goods-only: stock 0 | Tell the user; no retry possible |
| 400 | `invalid_redemption_code` | Code unknown / expired | Ask the user for the code as printed; do not invent variants |
| 400 | `redemption_already_used` | Code burned | Tell the user; codes are single-use |
| 400 | `codes_not_accepted` | Target is `paid_only` and rejects codes | Drop `redemption_code` and retry, or pick a different target |
| 402 | (challenge) | Identity OK; payment required | Run `agentscore-pay pay` against the same URL |{extra_rows}
{peer_section}"""


__all__ = ["build_redemption_skill_md"]
