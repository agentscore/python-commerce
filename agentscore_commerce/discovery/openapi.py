"""OpenAPI snippets for AgentScore-related concepts (security schemes, denial schemas, 402 schema)."""

from dataclasses import dataclass
from typing import Any


def agentscore_security_schemes() -> dict[str, Any]:
    """Standard AgentScore identity security schemes for `components.securitySchemes`."""
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
    }


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
                "Denial code emitted by AgentScore's gate middleware in 403 responses. Each comes with a "
                "structured agent_instructions block describing recovery actions."
            ),
        },
        "AgentScoreDenialBody": {
            "type": "object",
            "properties": {
                "error": {"$ref": "#/components/schemas/AgentScoreDenialReason"},
                "agent_instructions": {
                    "type": "string",
                    "description": (
                        "JSON-encoded { action, steps, user_message } block. Agents parse this to learn how "
                        "to recover (e.g., poll a verify_url, switch headers, re-sign)."
                    ),
                },
                "verify_url": {
                    "type": "string",
                    "format": "uri",
                    "description": "Present for missing_identity / token_expired denials.",
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


@dataclass
class BuildAgentScoreOpenApiSnippetsInput:
    security: bool = True
    denials: bool = True
    payment_required: bool = True


def agentscore_openapi_snippets(opts: BuildAgentScoreOpenApiSnippetsInput | None = None) -> dict[str, Any]:
    """Returns a `components` snippet ready to merge into an OpenAPI document."""
    o = opts or BuildAgentScoreOpenApiSnippetsInput()
    out: dict[str, Any] = {}
    if o.security:
        out["securitySchemes"] = agentscore_security_schemes()
    if o.denials or o.payment_required:
        schemas: dict[str, Any] = {}
        if o.denials:
            schemas.update(agentscore_denial_schemas())
        if o.payment_required:
            schemas.update(agentscore_payment_required_schema())
        out["schemas"] = schemas
    return out
