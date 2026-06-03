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

from typing import Any, Final, Literal

# Whether a paid surface accepts redemption codes. Applies to any merchant
# that bills per-purchase or per-call — goods (catalog rows) and API
# (per-endpoint or per-tier billing) both use this enum.

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

    Falls back to an empty string for unknown modes so responses don't leak
    ``None`` when the merchant introduces a non-standard mode.
    """
    return PURCHASE_MODE_NOTES.get(mode, "")


def build_agentscore_onboarding_steps(
    *,
    merchant_name: str,
    app_url: str,
    accepted_rails: list[str],
    requires_kyc: bool = False,
    vendor_type: Literal["goods", "api"] = "goods",
) -> list[str]:
    """Build the canonical skill.md ``onboarding_steps`` for an AgentScore merchant.

    Returns a list of imperative step strings the agent follows to bootstrap
    wallet + Passport, then either browse + buy (goods) or make the paid call
    (api). Generic across every AgentScore-gated merchant; only the
    merchant_name + app_url + rails list are substituted in.

    Rails accepted today: ``"tempo"``, ``"x402-base"``, ``"solana-mpp"``,
    ``"stripe-spt"``. Unknown rail names are passed through verbatim so future
    rails work without an SDK bump.

    Pass ``vendor_type="api"`` for per-call API providers — the catalog step is
    dropped and the final step becomes "Make the paid call" instead of
    "Place the order".
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

    # Per-rail compatible-client hints; mirrors `compatible_clients_by_rails`
    # on the 402 body so skill.md and the runtime challenge stay in sync.
    compatible_hints = [
        ("tempo", "`tempo request` works for tempo USDC.e"),
        ("x402-base", "`x402-proxy` / `purl` work for Base x402"),
        ("stripe-spt", "`@stripe/link-cli` works for Stripe SPT"),
    ]
    compatible_fragment = ", ".join(hint for rail, hint in compatible_hints if rail in accepted_rails)

    compatible_clients_clause = (
        f"the rails table also lists per-rail `compatible_clients` — {compatible_fragment}. "
        if compatible_fragment
        else ""
    )
    install_step = (
        "Install agentscore-pay if you don't already have a compatible client for your funded chain: "
        "`npm i -g @agent-score/pay` (or `brew install agentscore/tap/agentscore-pay`). "
        f"{merchant_name} accepts: {rails_human}. agentscore-pay speaks every supported rail; "
        f"{compatible_clients_clause}"
        "Any spec-compliant client for an individual rail works too."
    )
    bootstrap_step = (
        "First-run only: bootstrap wallet + Passport. Run `agentscore-pay agent-guide --json` "
        "for the canonical cold-start path — it walks `agentscore-pay init` "
        "(creates keystore + per-chain wallet), `agentscore-pay passport login` "
        f"(one-time KYC{'; required for this merchant' if requires_kyc else ''}; "
        "the human completes a verify URL once and pay caches the operator_token), "
        "and `agentscore-pay balance` to see which chain has USDC. Skip if your "
        "wallet+Passport are already provisioned."
    )
    stripe_fallback_step = (
        "If your only payment method is a Stripe / Link card (no crypto), install `@stripe/link-cli` "
        "instead of agentscore-pay and use it on the SPT rail. Identity gating still applies — the "
        "merchant's 403 with `verify_url` lets you bootstrap a Passport even with no crypto wallet involved."
    )
    returning_user_step = (
        "Returning user note: if you've paid an AgentScore-gated merchant before from this wallet, "
        "the wallet is already in your Passport's `linked_wallets[]` and identity flows through "
        "automatically with no re-KYC prompt. Paying from a NEW wallet while you already hold an "
        "`opc_...` token returns 403 `wallet_signer_mismatch`; the body lists `linked_wallets[]` and "
        "`agent_instructions.action: resign_or_switch_to_operator_token` with three deterministic "
        "recoveries (switch to a linked wallet, drop the operator_token to re-KYC the new wallet, "
        "or pre-claim the new wallet via SIWE on agentscore.com/verify)."
    )
    rail_count = len(accepted_rails)
    rail_plural = "" if rail_count == 1 else "s"
    pick_rail_step = (
        f"Pick the rail your wallet is funded for. The 402 advertises {rail_count} rail{rail_plural}. "
        "`agentscore-pay balance` (without `--chain`) lists every chain's USDC; pay rejects with "
        "`multi_rail_ambiguity` if you don't pass `--chain` on a multi-rail challenge."
    )
    place_order_step = (
        f"Place the order: `agentscore-pay pay POST {app_url}/purchase --chain <{chain_flags}> "
        "-d '<body>' --max-spend <amount>` for crypto rails. For Stripe SPT, follow the handoff "
        "hint pay emits and use `@stripe/link-cli` instead. Either way pay handles the 402 retry, "
        "signing, and Passport attachment; branch on the structured CliError `code` on non-zero "
        "exit (insufficient_balance, multi_rail_ambiguity, config_error for missing wallet/Passport, etc.)."
    )
    make_call_step = (
        f"Make the paid call: `agentscore-pay pay POST {app_url}/<endpoint> --chain <{chain_flags}> "
        "--max-spend <amount>`; pay handles 402 retry, rail selection, signing, and Passport "
        "attachment. Branch on the structured CliError `code` on non-zero exit (insufficient_balance, "
        "multi_rail_ambiguity, config_error for missing wallet/Passport, etc.)."
    )

    accepts_stripe = "stripe-spt" in accepted_rails
    stripe_steps = [stripe_fallback_step] if accepts_stripe else []
    if vendor_type == "api":
        return [
            install_step,
            bootstrap_step,
            *stripe_steps,
            returning_user_step,
            pick_rail_step,
            make_call_step,
        ]
    return [
        install_step,
        bootstrap_step,
        *stripe_steps,
        returning_user_step,
        f"Browse the catalog: `curl {app_url}/catalog`.",
        (
            "Read each product's `purchase_mode` and `purchase_note` to decide "
            "whether a redemption code is required, optional, or rejected."
        ),
        pick_rail_step,
        place_order_step,
    ]


def build_merchant_index_json(
    *,
    name: str,
    description: str,
    docs: dict[str, str],
    endpoints: dict[str, str],
    supported_rails: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical AgentScore commerce ``/`` root discovery body.

    Works for both goods merchants (catalog + purchase + orders) and API
    merchants (per-call paid endpoints) — ``endpoints`` and any
    merchant-specific fields are passed through ``extra``.

    Common fields surfaced: ``name``, ``description``, ``docs``, ``endpoints``,
    ``audience: "agents"``, ``supported_rails``. Pass ``extra`` for
    merchant-specific additions: ``compliance`` for goods merchants, ``pricing``
    for API merchants, ``website`` for branded fronts.

    ``docs`` keys map to absolute URLs; pass whichever discovery surfaces this
    merchant ships (``llms``, ``openapi``, ``skill_md``, ``mpp``, ``agent_card``,
    ``ucp``, ``jwks``, ``redemption``, ...).
    """
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "docs": docs,
        "endpoints": endpoints,
        "audience": "agents",
        "supported_rails": supported_rails,
    }
    if extra:
        body.update(extra)
    return body


def standard_endpoint_descriptions(
    *,
    kind: Literal["goods", "api"] = "goods",
    include_order_status_route: bool = False,
) -> dict[str, str]:
    """Canonical descriptions for the standard AgentScore commerce endpoints.

    Use in ``/`` discovery JSON, OpenAPI summaries, or anywhere the merchant
    needs to describe what each endpoint does in agent-readable language.

    Descriptions are merchant-agnostic; they describe the response semantics
    (402 on discovery, 400 on validation, 403 on identity, 200 on success), not
    the body schema (which varies per merchant; surface that in OpenAPI).

    Pass ``kind="api"`` for per-call API providers; the bundle drops catalog +
    orders routes and surfaces ``POST /<endpoint>`` + ``GET /usage`` instead.

    ``include_order_status_route=True`` (goods only) adds the lightweight
    ``/orders/{id}/status`` PII-free variant alongside the full ``/orders/{id}``.
    """
    if kind == "api":
        return {
            "POST /<endpoint>": (
                "Per-call paid endpoint. Returns 402 on the discovery leg with "
                "payment rails; 400 on body rejection; 403 + recovery payload "
                "when identity is required; 200 with the call result on success."
            ),
            "GET /usage": "Per-credential usage / billing summary. Identity-scoped.",
        }
    out: dict[str, str] = {
        "GET /catalog": "List purchasable products.",
        "GET /catalog/{slug}": "Single product detail.",
        "POST /purchase": (
            "Place an order. Returns 402 on the discovery leg with payment "
            "rails; 400 on body rejection; 403 + recovery payload when identity "
            "is required; 200 with order confirmation on success."
        ),
        "GET /orders/{id}": "Order detail (PII). Identity-scoped.",
    }
    if include_order_status_route:
        out["GET /orders/{id}/status"] = "Payment status only (no PII)."
    return out


def build_success_next_steps(
    *,
    order_status_url: str | None = None,
    fulfillment_eta: str | None = None,
    user_message: str | None = None,
) -> dict[str, str]:
    """Standard ``next_steps`` block emitted in a 200 success body.

    Works for both goods-merchant order-success and API-merchant per-call-success
    — the ``user_message`` reinforces the cross-merchant Passport pattern
    (universal), with merchant-specific copy overridable via ``user_message``.

    ``order_status_url`` is emitted as ``order_status_url``. API merchants that
    don't have an order-detail endpoint can pass a usage/dashboard URL or omit
    the field.

    ``fulfillment_eta`` is goods-specific (shipping window) — omit for API or
    digital-goods merchants.
    """
    out: dict[str, str] = {
        "action": "done",
        "user_message": user_message
        or ("Payment complete. Your AgentScore Passport is now active across every AgentScore-gated merchant."),
    }
    if order_status_url:
        out["order_status_url"] = order_status_url
    if fulfillment_eta is not None:
        out["fulfillment_eta"] = fulfillment_eta
    return out


__all__ = [
    "PURCHASE_MODE_NOTES",
    "PurchaseMode",
    "build_agentscore_onboarding_steps",
    "build_success_next_steps",
    "purchase_mode_note",
    "standard_endpoint_descriptions",
]
