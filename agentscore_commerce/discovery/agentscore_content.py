"""Standard agent-facing prose for AgentScore-gated merchants.

Every AgentScore merchant emits roughly the same skill.md onboarding steps,
catalog purchase-mode notes, and endpoint descriptions. These helpers ship
those canonical strings so merchants supply only the merchant-specific parts
(name, URL, accepted rails) and get consistent agent-facing content back.

Rationale: agents that hit one AgentScore merchant should see the same pattern
hints at every other one. Custom prose per merchant adds noise without adding
information; the SDK owns the cross-merchant boilerplate so it stays consistent.
"""

from __future__ import annotations

from typing import Final, Literal

# Purchase-mode semantics; every AgentScore commerce merchant that supports
# product-level redemption codes uses the same three modes. Agents read the
# ``purchase_note`` from /catalog to decide whether a code is required, optional,
# or rejected before posting to /purchase.

PurchaseMode = Literal["redemption_only", "coupon_applicable", "paid_only"]


PURCHASE_MODE_NOTES: Final[dict[str, str]] = {
    "redemption_only": (
        "Requires a single-use redemption code (printed on a mailer or other "
        "out-of-band delivery). Submit the code in the request body as "
        "`redemption_code`. Without a valid code the order is rejected."
    ),
    "coupon_applicable": (
        "Codes are optional. Without one, settle at list price. With a valid "
        "code the discount is applied automatically (percent_off, fixed_off, "
        "or fixed_settle)."
    ),
    "paid_only": (
        "Codes are NOT accepted. Settle at the listed price. Submitting a "
        "`redemption_code` field returns 400 codes_not_accepted."
    ),
}


def purchase_mode_note(mode: str) -> str:
    """Return the canonical agent-facing note for a ``purchase_mode``.

    Falls back to an empty string for unknown modes so /catalog responses don't
    leak ``None`` when the merchant introduces a non-standard mode.
    """
    return PURCHASE_MODE_NOTES.get(mode, "")


def build_agentscore_onboarding_steps(
    *,
    merchant_name: str,
    app_url: str,
    accepted_rails: list[str],
    requires_kyc: bool = False,
) -> list[str]:
    """Build the canonical skill.md ``onboarding_steps`` for an AgentScore merchant.

    Returns a list of imperative step strings the agent follows to bootstrap
    wallet + Passport, browse the catalog, and place an order. Generic across
    every AgentScore-gated merchant; only the merchant_name + app_url + rails
    list are substituted in.

    Rails accepted today: ``"tempo"``, ``"x402-base"``, ``"solana-mpp"``,
    ``"stripe-spt"``. Unknown rail names are passed through verbatim so future
    rails work without an SDK bump.
    """
    rail_word_map = {
        "tempo": "Tempo USDC",
        "x402-base": "x402 USDC on Base",
        "solana-mpp": "Solana SPL USDC",
        "stripe-spt": "Stripe Shared Payment Token",
    }
    rails_human = ", ".join(rail_word_map.get(r, r) for r in accepted_rails)
    chain_flags = (
        " | ".join(
            flag
            for rail, flag in (
                ("tempo", "tempo"),
                ("x402-base", "base"),
                ("solana-mpp", "solana"),
            )
            if rail in accepted_rails
        )
        or "tempo|base"
    )

    return [
        (
            f"Install agentscore-pay: `npm i -g @agent-score/pay` (or "
            f"`brew install agentscore/tap/agentscore-pay`). {merchant_name} "
            f"accepts: {rails_human}. agentscore-pay speaks every supported rail; "
            f"any spec-compliant client for an individual rail works too."
        ),
        (
            "First-run only: bootstrap wallet + Passport via `agentscore-pay init` "
            "(creates keystore + per-chain wallet), `agentscore-pay passport login` "
            f"(one-time KYC{'; required for this merchant' if requires_kyc else ''}), "
            "then `agentscore-pay balance` to confirm funds. Skip if your "
            "wallet+Passport are already provisioned."
        ),
        f"Browse the catalog: `curl {app_url}/catalog`.",
        (
            "Read each product's `purchase_mode` and `purchase_note` to decide "
            "whether a redemption code is required, optional, or rejected."
        ),
        (
            f"Place the order: `agentscore-pay pay POST {app_url}/purchase "
            f"--chain <{chain_flags}> -d '<body>' --max-spend <amount>`; pay "
            "handles 402 retry, rail selection, signing, and Passport attachment."
        ),
    ]


def standard_endpoint_descriptions(*, app_url: str) -> dict[str, str]:
    """Canonical descriptions for the standard AgentScore commerce endpoints.

    Use in ``/`` discovery JSON, OpenAPI summaries, or anywhere the merchant
    needs to describe what each endpoint does in agent-readable language.
    """
    return {
        "GET /catalog": ("List in-stock products with `purchase_mode` and `purchase_note` describing code rules."),
        "GET /catalog/{slug}": "Single product detail (same shape as catalog row).",
        "POST /purchase": (
            "Body: `{ product_slug, redemption_code?, email, shipping }`. "
            "Returns 402 with payment rails on the discovery leg, 400 with "
            "structured agent_instructions on body/code rejection, 403 + "
            "recovery payload when a hard-gated product needs identity, 200 "
            "with order confirmation on success."
        ),
        "GET /orders/{id}": "Order status + tracking ref. Identity-scoped.",
    }


def build_order_success_next_steps(
    *,
    order_status_url: str,
    fulfillment_eta: str | None = None,
) -> dict[str, str]:
    """Standard ``next_steps`` block emitted in a 200 order-success body.

    The ``user_message`` reinforces the cross-merchant Passport pattern; agents
    persist this hint so future AgentScore-gated endpoints recognize the user.
    ``fulfillment_eta`` is merchant-specific (shipping window).
    """
    out: dict[str, str] = {
        "action": "done",
        "order_status_url": order_status_url,
        "user_message": (
            "Order complete. Your AgentScore Passport is now active across every AgentScore-gated merchant."
        ),
    }
    if fulfillment_eta is not None:
        out["fulfillment_eta"] = fulfillment_eta
    return out


__all__ = [
    "PURCHASE_MODE_NOTES",
    "PurchaseMode",
    "build_agentscore_onboarding_steps",
    "build_order_success_next_steps",
    "purchase_mode_note",
    "standard_endpoint_descriptions",
]
