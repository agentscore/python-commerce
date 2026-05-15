"""OpenAPI snippets for AgentScore-related concepts (security schemes, denial schemas, 402 schema)."""

from dataclasses import dataclass
from typing import Any, Literal


def agentscore_security_schemes() -> dict[str, Any]:
    """Standard AgentScore identity security schemes for `components.securitySchemes`.

    Includes ``siwx`` (Sign-In With X) per the x402scan discovery spec so identity-gated
    operations can declare ``security: [{ "siwx": [] }]`` and stay classified as
    identity-only, not paid.
    """
    return {
        "OperatorToken": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Operator-Token",
            "description": (
                "Operator-token-path identity (opc_...). Works on every payment rail; reusable across "
                "AgentScore merchants. If both X-Operator-Token and X-Wallet-Address are sent, this one wins."
            ),
        },
        "WalletAddress": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Wallet-Address",
            "description": (
                "Wallet-path identity (0x... or base58). Only works on rails that carry a wallet signature "
                "(Tempo MPP, x402 EIP-3009, x402 SPL Token). The wallet you claim MUST sign the payment."
            ),
        },
        "siwx": siwx_security_scheme(),
    }


def siwx_security_scheme() -> dict[str, Any]:
    """Sign-In With X security scheme entry, per the x402scan discovery spec.

    Reference it on identity-gated (but free) operations as
    ``security: [{ "siwx": [] }]``. Do NOT also attach ``x-payment-info`` to those
    routes; x402scan will misclassify them as paid.
    """
    return {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "SIWX",
        "description": (
            "Sign-In With X wallet authentication. Agent signs a challenge with their wallet (any supported chain) "
            "and presents the proof in the Authorization header. Used for identity-gated free endpoints; "
            "payment-required endpoints declare x-payment-info instead."
        ),
    }


@dataclass
class XPaymentInfoFixedPrice:
    currency: str
    amount: str
    mode: Literal["fixed"] = "fixed"


@dataclass
class XPaymentInfoDynamicPrice:
    currency: str
    min: str
    max: str
    mode: Literal["dynamic"] = "dynamic"


XPaymentInfoPrice = XPaymentInfoFixedPrice | XPaymentInfoDynamicPrice


@dataclass
class XPaymentInfoMpp:
    method: str
    intent: str
    currency: str


def x_payment_info_extension(
    *,
    price: XPaymentInfoPrice,
    protocols: list[dict[str, Any]],
    description: str | None = None,
) -> dict[str, Any]:
    """Wrap a price + protocols block under ``x-payment-info``.

    For spreading into an OpenAPI operation object. ``protocols`` is a list of
    single-key dicts: ``{"x402": {}}`` for x402, ``{"mpp": {"method": ...,
    "intent": ..., "currency": ...}}`` for MPP. Order is preserved.

    Emits ``authMode: "payment"`` by default per the x402scan convention.
    """
    if isinstance(price, XPaymentInfoFixedPrice):
        price_dict: dict[str, Any] = {"mode": "fixed", "currency": price.currency, "amount": price.amount}
    else:
        price_dict = {"mode": "dynamic", "currency": price.currency, "min": price.min, "max": price.max}
    block: dict[str, Any] = {"authMode": "payment", "price": price_dict, "protocols": protocols}
    if description is not None:
        block["description"] = description
    return {"x-payment-info": block}


def x_guidance_extension(text: str) -> dict[str, str]:
    """Wrap a prose blurb under ``x-guidance`` for spreading into an OpenAPI ``info`` block.

    Per the x402scan discovery spec, ``info.x-guidance`` should explain to an agent
    how to use the API at a high level. Discovery crawlers surface this on listings.
    """
    return {"x-guidance": text}


def x_service_info_extension(
    *,
    categories: list[str],
    docs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Wrap a service-info block under ``x-service-info``.

    Spread into the OpenAPI document's root alongside ``paths``, ``info``, etc.
    Discovery crawlers (x402scan, agent CLIs) read this to categorize the
    service and follow links to human-side docs.
    """
    block: dict[str, Any] = {"categories": categories}
    if docs is not None:
        block["docs"] = docs
    return {"x-service-info": block}


def x_payment_info_from_checkout(
    *,
    checkout: Any,
    price: XPaymentInfoPrice,
    description: str | None = None,
    protocol_extras: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Derive an ``x-payment-info`` extension from a configured ``Checkout``.

    Walks ``checkout.rails`` and emits one entry in ``protocols[]`` per rail —
    Tempo MPP, x402 (Base), Solana MPP, Stripe SPT. Saves merchants from
    enumerating protocols by hand and keeps the OpenAPI doc in sync with the
    actual rails the Checkout serves.

    ``price`` is merchant-supplied (the rail registry doesn't carry per-merchant
    pricing). Per-rail extras (client commands) can be merged via
    ``protocol_extras`` keyed by rail slug (``tempo``, ``base``, ``solana``,
    ``stripe``).

    For Solana MPP, ``currency`` is the SPL mint address per the official
    spec (paymentauth.org/draft-solana-charge-00) — read from ``spec.token``.
    """
    from agentscore_commerce.payment.rail_spec import (
        SolanaMppRailSpec,
        StripeRailSpec,
        TempoRailSpec,
        TempoSessionRailSpec,
        X402BaseRailSpec,
    )

    protocols: list[dict[str, Any]] = []
    extras = protocol_extras or {}
    for spec in checkout.rails.values():
        if isinstance(spec, StripeRailSpec):
            entry = {"method": "stripe", "intent": "charge", "currency": "usd"}
            entry.update(extras.get("stripe", {}))
            protocols.append({"mpp": entry})
        elif isinstance(spec, X402BaseRailSpec):
            entry = {"scheme": "exact", "network": "base", "asset": "USDC"}
            entry.update(extras.get("base", {}))
            protocols.append({"x402": entry})
        elif isinstance(spec, SolanaMppRailSpec):
            token = getattr(spec, "token", None)
            entry = {"method": "solana", "intent": "charge"}
            if isinstance(token, str) and token:
                entry["currency"] = token
            entry.update(extras.get("solana", {}))
            protocols.append({"mpp": entry})
        elif isinstance(spec, (TempoRailSpec, TempoSessionRailSpec)):
            token = getattr(spec, "token", None)
            currency = getattr(spec, "currency", None)
            entry = {"method": "tempo", "intent": "charge"}
            value = currency if isinstance(currency, str) and currency else token
            if isinstance(value, str) and value:
                entry["currency"] = value
            entry.update(extras.get("tempo", {}))
            protocols.append({"mpp": entry})
    return x_payment_info_extension(
        price=price,
        protocols=protocols,
        description=description,
    )


def agentscore_denial_schemas() -> dict[str, Any]:
    """Standard AgentScore denial response schemas for `components.schemas`."""
    return {
        "AgentScoreDenialReason": {
            "type": "string",
            "enum": [
                "missing_identity",
                "identity_verification_required",
                "token_expired",
                "invalid_credential",
                "wallet_signer_mismatch",
                "wallet_auth_requires_wallet_signing",
                "wallet_not_trusted",
                "api_error",
                "payment_required",
            ],
            "description": (
                "Denial code emitted by AgentScore's gate middleware in 403 responses. Every code carries a "
                "structured agent_instructions block describing recovery actions (per-code action: "
                "missing_identity → probe_identity_then_session, identity_verification_required / "
                "token_expired → deliver_verify_url_and_poll, invalid_credential → "
                "switch_token_or_restart_session, wallet_signer_mismatch → resign_or_switch_to_operator_token, "
                "wallet_auth_requires_wallet_signing → switch_to_operator_token, wallet_not_trusted → "
                "contact_support — UNFIXABLE compliance only (sanctions/age/jurisdiction_restricted); "
                "fixable reasons re-route to identity_verification_required, payment_required → contact_merchant)."
            ),
        },
        "AgentScoreDenialBody": {
            "type": "object",
            "properties": {
                "error": {"$ref": "#/components/schemas/AgentScoreDenialReason"},
                "agent_instructions": {
                    "type": "string",
                    "description": (
                        "JSON-encoded { action, steps, user_message } block. Always present on every "
                        "denial; agents parse this to learn how to recover (e.g., poll verify_url, "
                        "switch headers, re-sign)."
                    ),
                },
                "verify_url": {
                    "type": "string",
                    "format": "uri",
                    "description": (
                        "Present for missing_identity / identity_verification_required / token_expired "
                        "denials. Agent shares this with the user to complete KYC or claim a wallet. "
                        "Not present on wallet_not_trusted (UNFIXABLE compliance — re-verification "
                        "won't change the outcome)."
                    ),
                },
                "session_id": {"type": "string"},
                "poll_url": {"type": "string", "format": "uri"},
                "poll_secret": {"type": "string"},
                "agent_memory": {
                    "type": "object",
                    "description": "Cross-merchant pattern hint emitted on first-encounter denials.",
                },
            },
            "required": ["error", "agent_instructions"],
        },
    }


def agentscore_payment_required_schema() -> dict[str, Any]:
    """Standard 402 PaymentRequired body schema for AgentScore-extended 402 responses."""
    return {
        "AgentScorePaymentRequired": {
            "type": "object",
            "properties": {
                "payment_required": {"type": "boolean", "enum": [True]},
                "x402Version": {"type": "integer", "enum": [1, 2]},
                "accepts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "x402 PaymentRequired.accepts entries.",
                },
                "accepted_methods": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "MPP method entries (tempo/charge, x402/exact, stripe/charge, ...).",
                },
                "amount_usd": {"type": "string"},
                "currency": {"type": "string"},
                "pricing": {
                    "type": "object",
                    "properties": {
                        "subtotal": {"type": "string"},
                        "tax": {"type": "string"},
                        "tax_rate": {"type": "number"},
                        "tax_state": {"type": "string"},
                        "total": {"type": "string"},
                    },
                },
                "identity_mode": {"type": "string", "enum": ["wallet", "operator_token"]},
                "required_signer": {"type": "string"},
                "linked_wallets": {"type": "array", "items": {"type": "string"}},
                "signer_constraint": {"type": "string"},
                "agent_instructions": {"type": "object"},
                "agent_memory": {"type": "object"},
            },
        },
    }


def agentscore_openapi_snippets(
    *,
    security: bool = True,
    denials: bool = True,
    payment_required: bool = True,
) -> dict[str, Any]:
    """Returns a `components` snippet ready to merge into an OpenAPI document."""
    out: dict[str, Any] = {}
    if security:
        out["securitySchemes"] = agentscore_security_schemes()
    if denials or payment_required:
        schemas: dict[str, Any] = {}
        if denials:
            schemas.update(agentscore_denial_schemas())
        if payment_required:
            schemas.update(agentscore_payment_required_schema())
        out["schemas"] = schemas
    return out
