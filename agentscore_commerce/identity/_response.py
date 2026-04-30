"""Shared serialization for DenialReason → HTTP body dict.

Every adapter (ASGI, FastAPI, Flask, Django, AIOHTTP, Sanic) renders the same
body shape for a denial — this helper keeps them in sync and in one place.
Includes the wallet-signer-match fields and the agent_memory payload.

Body shape: ``{"error": {"code": ..., "message": ...}, ...}`` — matches the
canonical AgentScore core API response shape so downstream agents see one
consistent ``error.code`` + ``error.message`` pair regardless of which layer
produced the denial.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from agentscore_commerce.identity.types import DenialReason, build_agent_memory_hint

_log = logging.getLogger("agentscore_gate")

# Field names the gate claims authority over. Merchant-provided ``extra`` (from the
# on_before_session hook) MUST NOT override these — a buggy or malicious hook could
# otherwise replace ``verify_url`` with a phishing URL or drop agent_instructions.
_RESERVED_FIELDS = frozenset(
    {
        "error",
        "decision",
        "reasons",
        "verify_url",
        "session_id",
        "poll_secret",
        "poll_url",
        "agent_instructions",
        "agent_memory",
        "claimed_operator",
        "actual_signer_operator",
        "expected_signer",
        "actual_signer",
        "linked_wallets",
    }
)

_MISSING_IDENTITY_INSTRUCTIONS = json.dumps(
    {
        "action": "probe_identity_then_session",
        "steps": [
            (
                "If you have a wallet AND this merchant accepts a wallet-signing rail "
                "(Tempo MPP or x402 — see openapi.json x-payment-info): send X-Wallet-Address "
                "and retry. If the wallet is already linked to an AgentScore operator (via "
                "prior SIWE claim or prior captureWallet), this passes in one round trip. If "
                "the wallet is unlinked or the account has no KYC, the 403 will include a "
                "verify_url — share it with the user to claim the wallet + complete KYC, "
                "then retry."
            ),
            (
                "If step 1 is denied or you already have a stored operator_token (valid + "
                "not expired): send X-Operator-Token: opc_... and retry."
            ),
            (
                "If neither applies: retry with NO identity header. Merchants that "
                "auto-create verification sessions (most AgentScore merchants do) return "
                "verify_url + session_id + poll_secret in the 403 body — share verify_url "
                "with the user, then poll poll_url every 5s with the X-Poll-Secret header "
                "until status=verified (the poll returns a one-time operator_token). If the "
                "retry returns the same bare 403, this merchant does not support self-service "
                "session bootstrapping — direct the user to https://agentscore.sh/sign-up to "
                "create an AgentScore identity and mint an operator_token from their "
                "dashboard (https://agentscore.sh/dashboard/verify). The user hands the "
                "opc_... to you, and you retry with X-Operator-Token."
            ),
        ],
        "user_message": (
            "Try X-Wallet-Address first if you have a wallet and the merchant accepts Tempo/x402; "
            "fall back to a stored X-Operator-Token, then to the session/verify flow described in "
            "agent_memory.bootstrap."
        ),
    }
)

WALLET_SIGNER_MISMATCH_INSTRUCTIONS = json.dumps(
    {
        "action": "resign_or_switch_to_operator_token",
        "steps": [
            (
                "Preferred: re-submit the payment signed by expected_signer (or any entry in "
                "linked_wallets — same-operator wallets are fungible) and retry with the same "
                "X-Wallet-Address."
            ),
            (
                "Alternative: drop X-Wallet-Address and retry with X-Operator-Token. Use a "
                "stored opc_... if you have one; otherwise retry this request with NO "
                "identity header — the merchant will mint a verification session in the "
                "403 body (verify_url + poll_secret). Share verify_url with the user, poll, "
                "receive a fresh opc_..."
            ),
        ],
        "user_message": (
            "The payment signer resolves to a different operator than X-Wallet-Address. Re-sign "
            "from expected_signer or any linked_wallets entry, or switch to X-Operator-Token."
        ),
    }
)

WALLET_AUTH_REQUIRES_WALLET_SIGNING_INSTRUCTIONS = json.dumps(
    {
        "action": "switch_to_operator_token",
        "steps": [
            (
                "This payment rail (Stripe SPT, card) carries no wallet signature — "
                "X-Wallet-Address cannot be verified against the payment."
            ),
            (
                "Drop X-Wallet-Address and retry with X-Operator-Token. If you do not have a "
                "stored opc_..., retry with no identity header to receive a verification session."
            ),
        ],
        "user_message": (
            "Wallet-address identity is only supported on wallet-signing rails (Tempo MPP, x402). "
            "On Stripe or card, use X-Operator-Token instead."
        ),
    }
)

WALLET_NOT_TRUSTED_INSTRUCTIONS = json.dumps(
    {
        "action": "contact_support",
        "steps": [
            (
                "The wallet's operator failed an UNFIXABLE compliance check (sanctions, "
                "age, or jurisdiction). `reasons` lists which: `sanctions_flagged` / "
                "`age_insufficient` / `jurisdiction_restricted`. KYC re-verification "
                "won't change the outcome — the policy denial is structural."
            ),
            (
                "Surface the denial to the user with the merchant's support contact. "
                "Do not retry the same merchant request; do not hand the user a "
                "verify_url (verification won't fix this code path)."
            ),
            (
                "Fixable compliance reasons (`kyc_required`, `kyc_pending`, "
                "`kyc_failed`) do NOT land on this code — the gate auto-mints a "
                "verification session for those and returns "
                "`identity_verification_required` with poll endpoints, same shape as "
                "`missing_identity`. `jurisdiction_restricted` IS in the unfixable "
                "bucket because the API only emits it after KYC is verified (the "
                "user's KYC'd country is in the blocked list — re-doing KYC won't "
                "change the country)."
            ),
        ],
        "user_message": (
            "This purchase is denied by the merchant's compliance policy and cannot be "
            "resolved by re-verifying. Contact the merchant's support if you believe "
            "this is in error."
        ),
    }
)

PAYMENT_REQUIRED_INSTRUCTIONS = json.dumps(
    {
        "action": "contact_merchant",
        "steps": [
            (
                "The merchant's AgentScore tier does not include the assess feature, so "
                "agent identity cannot be evaluated. This is a merchant-side configuration "
                "gap — there is no agent-side recovery."
            ),
            (
                "Contact the merchant (their support channel — typically listed in "
                "/llms.txt or the OpenAPI servers metadata) and request they upgrade "
                "their AgentScore plan."
            ),
        ],
        "user_message": (
            "This merchant's identity gate is misconfigured (AgentScore tier doesn't "
            "support assess). Contact the merchant — there's nothing to fix on the "
            "agent side."
        ),
    }
)

# Fallback when API didn't supply next_steps. Normal path provides them; this is
# defense-in-depth so 403s never go out without a machine-readable recovery step.
IDENTITY_VERIFICATION_REQUIRED_FALLBACK_INSTRUCTIONS = json.dumps(
    {
        "action": "deliver_verify_url_and_poll",
        "steps": [
            "Share verify_url with the user — they complete identity verification on AgentScore.",
            (
                "If session_id + poll_secret are present in the body, poll poll_url every "
                "5 seconds with header `X-Poll-Secret: <poll_secret>` until status=verified. "
                "The poll returns a one-time operator_token."
            ),
            "Retry the original request with header `X-Operator-Token: <opc_...>`.",
        ],
        "user_message": (
            "Identity verification is required. Visit verify_url, then poll poll_url for the operator token and retry."
        ),
    }
)

TOKEN_EXPIRED_FALLBACK_INSTRUCTIONS = json.dumps(
    {
        "action": "deliver_verify_url_and_poll",
        "steps": [
            (
                "The operator token is expired or revoked. AgentScore auto-mints a fresh "
                "verification session — complete it to receive a new opc_..."
            ),
            (
                "Share verify_url with the user, then poll poll_url every 5 seconds with "
                "header `X-Poll-Secret: <poll_secret>` until status=verified. The poll "
                "returns a fresh one-time operator_token."
            ),
            "Retry the original request with header `X-Operator-Token: <new_opc_...>`.",
        ],
        "user_message": (
            "Operator token is expired or revoked. A new verification session has been "
            "minted — visit verify_url to refresh."
        ),
    }
)

# Default agent_instructions per denial code. Adapters can override by passing
# ``agent_instructions=`` on the DenialReason; otherwise the body emitter looks
# up this map so every denial carries a machine-readable next step.
_DEFAULT_AGENT_INSTRUCTIONS: dict[str, str] = {
    "missing_identity": _MISSING_IDENTITY_INSTRUCTIONS,
    "wallet_signer_mismatch": WALLET_SIGNER_MISMATCH_INSTRUCTIONS,
    "wallet_auth_requires_wallet_signing": WALLET_AUTH_REQUIRES_WALLET_SIGNING_INSTRUCTIONS,
    "wallet_not_trusted": WALLET_NOT_TRUSTED_INSTRUCTIONS,
    "payment_required": PAYMENT_REQUIRED_INSTRUCTIONS,
    "identity_verification_required": IDENTITY_VERIFICATION_REQUIRED_FALLBACK_INSTRUCTIONS,
    "token_expired": TOKEN_EXPIRED_FALLBACK_INSTRUCTIONS,
}


def build_missing_identity_reason() -> DenialReason:
    """Construct a missing_identity DenialReason with the cross-merchant memory hint attached.

    Emitted when the adapter has no identity AND no create_session_on_missing config — this is the
    cold-start bootstrap path where the memory hint is most useful. The attached agent_instructions
    hint the agent to try stored identity (returning-customer fast path) before running the
    session/verify flow.
    """
    return DenialReason(
        code="missing_identity",
        agent_instructions=_MISSING_IDENTITY_INSTRUCTIONS,
        agent_memory=build_agent_memory_hint(),
    )


_DEFAULT_MESSAGES: dict[str, str] = {
    "missing_identity": "No identity provided. Send X-Wallet-Address (wallet) or X-Operator-Token (credential).",
    "identity_verification_required": (
        "Identity verification is required to access this resource. Visit verify_url to complete KYC."
    ),
    "wallet_not_trusted": "The wallet does not meet the merchant compliance policy.",
    "api_error": "AgentScore is unreachable. This is transient — retry in a few seconds.",
    "payment_required": "AgentScore tier does not support assess. Contact support.",
    "wallet_signer_mismatch": (
        "Payment signer does not match the wallet claimed via X-Wallet-Address. The signer and the "
        "claimed wallet must both resolve to the same AgentScore operator."
    ),
    "wallet_auth_requires_wallet_signing": (
        "X-Wallet-Address was sent with a rail that has no wallet signature (Stripe SPT / card). "
        "Switch to X-Operator-Token, or use a wallet-signing rail (Tempo MPP, x402)."
    ),
    "token_expired": (
        "The operator token is expired or revoked. A fresh verification session has been minted — "
        "visit verify_url to mint a new token."
    ),
    "invalid_credential": (
        "The operator token is not recognized. Switch to a different stored token, or drop the "
        "header to bootstrap a fresh session."
    ),
}


def denial_reason_to_body(reason: DenialReason) -> dict[str, Any]:
    """Marshal a DenialReason dataclass into a flat dict suitable for the 403 JSON body.

    Shared across all adapters. Omits falsy optional fields so the body stays compact.
    Emits ``error: {code, message}`` matching the core API canonical shape; ``message``
    falls back to a per-code default when ``reason.message`` is None.
    """
    message = reason.message or _DEFAULT_MESSAGES.get(reason.code, "")
    body: dict[str, Any] = {"error": {"code": reason.code, "message": message}}
    if reason.decision is not None:
        body["decision"] = reason.decision
    if reason.reasons:
        body["reasons"] = reason.reasons
    if reason.verify_url:
        body["verify_url"] = reason.verify_url
    if reason.session_id:
        body["session_id"] = reason.session_id
    if reason.poll_secret:
        body["poll_secret"] = reason.poll_secret
    if reason.poll_url:
        body["poll_url"] = reason.poll_url
    instructions = reason.agent_instructions or _DEFAULT_AGENT_INSTRUCTIONS.get(reason.code)
    if instructions:
        body["agent_instructions"] = instructions
    # Cross-merchant pattern hint.
    if reason.agent_memory is not None:
        body["agent_memory"] = asdict(reason.agent_memory)
    # Wallet-signer-match fields, populated only for wallet_signer_mismatch.
    # For that code, actual_signer_operator is ALWAYS meaningful: a string means the signer
    # resolves to a different operator; null means the signer wallet isn't linked to any
    # operator. Both carry actionable info, so emit `null` explicitly (matches node-commerce).
    if reason.claimed_operator:
        body["claimed_operator"] = reason.claimed_operator
    if reason.code == "wallet_signer_mismatch":
        body["actual_signer_operator"] = reason.actual_signer_operator
    if reason.expected_signer:
        body["expected_signer"] = reason.expected_signer
    if reason.actual_signer:
        body["actual_signer"] = reason.actual_signer
    if reason.linked_wallets:
        body["linked_wallets"] = reason.linked_wallets
    # api_error denials get a default retry hint so agents know it's transient. Vendors can
    # override by spreading their own next_steps into a custom on_denied body.
    if reason.code == "api_error" and not (reason.extra and reason.extra.get("next_steps")):
        body["next_steps"] = {"action": "retry", "retry_after_seconds": 5}
    # Merchant-supplied fields from on_before_session hook. Guard against collision
    # with reserved fields — the gate owns those and can't let a hook override them.
    if reason.extra:
        for key, value in reason.extra.items():
            if key in _RESERVED_FIELDS:
                _log.warning(
                    "on_before_session returned reserved field '%s' — ignoring to preserve gate authority",
                    key,
                )
                continue
            body[key] = value
    return body
