"""Universal denial helpers shared across every adapter.

What lives here:
    FIXABLE_DENIAL_REASONS / is_fixable_denial — classifier for compliance reasons that can
        be resolved by re-completing KYC (vs sanctions / age failures which are permanent).
    denial_reason_status — picks the right HTTP status code per denial code (401 for credential
        problems, 503 for transient API errors, 403 for everything else).
    build_signer_mismatch_body — produces the standard 403 body for a non-pass signer_match
        verdict (read via get_signer_verdict).
    build_contact_support_next_steps — standard `next_steps.action: "contact_support"` shape for
        unfixable compliance denials.
    verification_agent_instructions — the canned `agent_instructions` block for
        identity-verification 403s. Vendors can override individual fields.

Adapters use `denial_reason_status` inside their default `on_denied` so vendors get the right
status code for free. The body builders are exported from each adapter so vendors who write
a custom `on_denied` can compose them without copy-paste.
"""

from collections.abc import Iterable
from typing import Any

from agentscore_commerce.identity.types import DenialReason, VerifyWalletSignerResult

# Compliance denial reasons that can be resolved by re-completing KYC. The API emits these
# when KYC is missing/pending/failed; the user can re-verify and retry.
#
# `jurisdiction_restricted` is NOT in this set — the API only emits it AFTER KYC is verified,
# meaning the user's KYC'd country is in the merchant's blocked list (or absent from the
# allowed list). Re-doing KYC won't change the country, so it's permanent. Same shape as
# `sanctions_flagged` and `age_insufficient` — surface contact_support, don't waste a
# /v1/sessions mint.
FIXABLE_DENIAL_REASONS: frozenset[str] = frozenset(
    {
        "kyc_required",
        "kyc_pending",
        "kyc_failed",
    }
)


def is_fixable_denial(reasons: Iterable[str] | None) -> bool:
    """Return True when every reason is fixable via KYC re-verification.

    False when any reason is permanent (sanctions, age, jurisdiction_restricted) OR when
    reasons is empty/None — without a known reason we can't promise a fix, so default to
    the bare denial path.
    """
    if not reasons:
        return False
    reasons_list = list(reasons)
    if not reasons_list:
        return False
    return all(r in FIXABLE_DENIAL_REASONS for r in reasons_list)


def denial_reason_status(reason: DenialReason) -> int:
    """Return the right HTTP status for a denial code.

    401 for `token_expired` / `invalid_credential`, 503 for `api_error`, 403 for everything else.
    """
    if reason.code in ("token_expired", "invalid_credential"):
        return 401
    if reason.code == "api_error":
        return 503
    return 403


def build_signer_mismatch_body(
    result: VerifyWalletSignerResult,
    *,
    user_message: str | None = None,
    learn_more_url: str | None = None,
) -> dict[str, Any] | None:
    """Standard 403 body for a non-pass signer_match verdict.

    Returns None for pass so vendors can call unconditionally::

        verdict = get_signer_verdict(request)
        if verdict is not None and verdict.signer_match is not None:
            body = build_signer_mismatch_body(verdict.signer_match)
            if body:
                return JSONResponse(body, status_code=403)
    """
    if result.kind == "pass":
        return None

    learn_more = learn_more_url or "https://docs.agentscore.com/guides/agent-identity"

    if result.kind == "wallet_signer_mismatch":
        linked = result.linked_wallets or []
        msg = user_message or (
            f"Sign the payment with one of the wallets linked to this operator: {', '.join(linked)}. Then retry."
            if linked
            else "Sign the payment with the same wallet you claimed via X-Wallet-Address, "
            "or switch to X-Operator-Token for rail-independent identity."
        )
        return {
            "error": {
                "code": "wallet_signer_mismatch",
                "message": (
                    "Payment signer does not match the wallet claimed via X-Wallet-Address. "
                    "The signer and the claimed wallet must both resolve to the same AgentScore operator."
                ),
            },
            "claimed_operator": result.claimed_operator,
            "actual_signer_operator": result.actual_signer_operator,
            "expected_signer": result.expected_signer,
            "actual_signer": result.actual_signer,
            "linked_wallets": linked,
            "next_steps": {
                "action": "regenerate_payment_from_linked_wallet",
                "user_message": msg,
                "learn_more_url": learn_more,
            },
        }

    return {
        "error": {
            "code": "wallet_auth_requires_wallet_signing",
            "message": (
                "Wallet-auth requires a payment rail that carries a wallet signature (Tempo MPP, x402). "
                "Stripe SPT and card rails have no wallet signer; switch to X-Operator-Token to use those."
            ),
        },
        "next_steps": {
            "action": "switch_to_operator_token",
            "user_message": user_message
            or "Drop the X-Wallet-Address header and retry with X-Operator-Token (works on every payment rail).",
            "learn_more_url": learn_more,
        },
    }


def build_contact_support_next_steps(support_email: str, message: str | None = None) -> dict[str, Any]:
    """Standard `next_steps` block for unfixable compliance denials (sanctions, age, etc.)."""
    return {
        "action": "contact_support",
        "support_email": support_email,
        "user_message": message
        or (
            f"If you believe this denial is in error, contact support at "
            f"{support_email} with the details of your request."
        ),
    }


def verification_agent_instructions(
    *,
    user_action: str | None = None,
    retry_step: str | None = None,
    extra_steps: list[str] | None = None,
    poll_interval_seconds: int = 5,
    timeout_seconds: int = 3600,
    order_ttl: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical `agent_instructions` block for identity-verification 403s.

    Tells the agent how to present verify_url, poll for the operator_token, and retry the
    original request. Universal across every AgentScore-gated merchant — overrides let vendors
    add merchant-specific steps.

    ``retry_step`` REPLACES the generic "Retry the original merchant request..." step with a
    merchant-specific one (e.g. "Retry POST /purchase with X-Operator-Token AND include
    order_id..."). Use it when your retry instruction is a refinement of the canonical retry,
    not an additional step. ``extra_steps`` is for genuinely additional steps that come AFTER
    the retry (e.g. "After payment the same call returns 200").
    """
    base_steps = [
        "Present the verify_url directly to the user — it is a complete, ready-to-open URL with the session "
        "token already embedded (e.g. https://www.agentscore.com/verify?session=sess_...). "
        "Do NOT modify or construct the URL yourself.",
        f"Immediately begin polling poll_url every {poll_interval_seconds} seconds with header X-Poll-Secret "
        "set to poll_secret. The user will complete verification in their browser while you poll in the background.",
        "The user visits the URL, signs in, completes identity verification (photo ID + selfie via Stripe Identity), "
        "and closes the tab. They do NOT need to copy or paste anything back to you.",
        'When your poll returns status "verified", extract operator_token from the response. This is a one-time '
        'value — save it immediately. Subsequent polls return status "consumed" without the token.',
        retry_step
        or "Retry the original merchant request with header X-Operator-Token set to the operator_token value.",
    ]
    out: dict[str, Any] = {
        "action": "poll_for_credential",
        "user_action": user_action
        or "The user must visit verify_url to complete identity verification before this request can proceed",
        "steps": [*base_steps, *(extra_steps or [])],
        "poll_interval_seconds": poll_interval_seconds,
        "poll_secret_header": "X-Poll-Secret",
        "retry_token_header": "X-Operator-Token",
        "timeout_seconds": timeout_seconds,
    }
    if order_ttl:
        out["order_ttl"] = order_ttl
    if extra:
        out.update(extra)
    return out


__all__ = [
    "FIXABLE_DENIAL_REASONS",
    "build_contact_support_next_steps",
    "build_signer_mismatch_body",
    "denial_reason_status",
    "is_fixable_denial",
    "verification_agent_instructions",
]
