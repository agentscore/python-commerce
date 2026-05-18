"""Factory for the standard ``on_denied`` callback used by Checkout's gate.

Replaces the ~100-line switch every consumer codebase (store, sayer-py,
martin-py) wrote by hand.

The shape is framework-neutral (``{status, body, headers?}``) — matches
``Checkout``'s ``on_denied`` signature directly. For per-framework gate
middleware (``AgentScoreGate(...)``) the merchant adapts at the call site
with the framework's ``JSONResponse(body, status_code=status, headers=headers)``
/ equivalent.

Mirrors node-commerce ``src/identity/default_denied.ts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentscore_commerce.identity._denial import (
    build_contact_support_next_steps,
    build_signer_mismatch_body,
)
from agentscore_commerce.identity._response import denial_reason_to_body
from agentscore_commerce.identity.types import DenialReason, VerifyWalletSignerResult

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class DefaultOnDeniedResult:
    """Framework-neutral denial response shape."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str] | None = None


def create_default_on_denied(
    *,
    merchant_name: str,
    support_email: str,
    support_context: str | None = None,
    payment_required_message: str | None = None,
    wallet_not_trusted_message: str | None = None,
) -> Callable[[DenialReason], DefaultOnDeniedResult]:
    """Build the canonical ``on_denied(reason)`` callback.

    Returns a framework-neutral ``DefaultOnDeniedResult(status, body, headers?)``
    matching ``Checkout``'s ``on_denied`` signature.

    Branch table (matches the hand-rolled version in every consumer):

    - ``wallet_signer_mismatch`` / ``wallet_auth_requires_wallet_signing`` →
      ``build_signer_mismatch_body(...)``, status 403
    - ``wallet_not_trusted`` → custom ``compliance_denied`` body + contact-support
      ``next_steps``, status 403
    - ``payment_required`` → denial body + ``compliance_error`` message, status 403
    - ``token_expired`` / ``invalid_credential`` → 401
    - ``api_error`` → 503 + ``Cache-Control: no-store``
    - default → 403
    """
    final_support_context = support_context or "Contact support if you believe this denial is in error."
    final_payment_required_message = (
        payment_required_message or "AgentScore tier does not support assess. Contact support."
    )
    final_wallet_not_trusted_message = (
        wallet_not_trusted_message or f"Identity check did not satisfy policy for {merchant_name}."
    )

    def _on_denied(reason: DenialReason) -> DefaultOnDeniedResult:
        if reason.code in ("wallet_signer_mismatch", "wallet_auth_requires_wallet_signing"):
            verdict = VerifyWalletSignerResult(
                kind=reason.code,
                claimed_operator=reason.claimed_operator,
                actual_signer_operator=reason.actual_signer_operator,
                expected_signer=reason.expected_signer or "",
                actual_signer=reason.actual_signer or "",
                linked_wallets=list(reason.linked_wallets or []),
                claimed_wallet=reason.expected_signer or "",
            )
            body = build_signer_mismatch_body(verdict)
            return DefaultOnDeniedResult(
                status=403,
                body=body or denial_reason_to_body(reason),
            )

        if reason.code == "wallet_not_trusted":
            policy_result = reason.extra.get("policy_result") if reason.extra else None
            return DefaultOnDeniedResult(
                status=403,
                body={
                    "error": {"code": "compliance_denied", "message": final_wallet_not_trusted_message},
                    "reasons": list(reason.reasons or []),
                    "policy_result": policy_result,
                    "verify_url": reason.verify_url,
                    "next_steps": build_contact_support_next_steps(support_email, final_support_context),
                },
            )

        if reason.code == "payment_required":
            body = denial_reason_to_body(reason)
            body["error"] = {"code": "compliance_error", "message": final_payment_required_message}
            return DefaultOnDeniedResult(status=403, body=body)

        if reason.code in ("token_expired", "invalid_credential"):
            return DefaultOnDeniedResult(status=401, body=denial_reason_to_body(reason))
        if reason.code == "api_error":
            return DefaultOnDeniedResult(
                status=503,
                body=denial_reason_to_body(reason),
                headers={"Cache-Control": "no-store"},
            )
        return DefaultOnDeniedResult(status=403, body=denial_reason_to_body(reason))

    return _on_denied


def default_read_only_on_denied(reason: DenialReason) -> DefaultOnDeniedResult:
    """Canonical ``on_denied`` for read-only resource gates (e.g. ``GET /orders/:id``).

    Collapses every denial code to **401 ``unauthorized``** while still spreading
    :func:`denial_reason_to_body` so ``agent_instructions`` / ``verify_url`` /
    session-mint fields ride through for the agent's recovery path. Stamps
    ``Cache-Control: no-store`` because RFC 7234 makes 4xx responses
    heuristically cacheable; transient denials (``api_error``, ``token_expired``)
    must not be replayed by a shared cache.

    Pair with ``AgentScoreGate(on_denied=default_read_only_on_denied)`` on
    routes where the resource owner is the only authorized identity (full
    compliance policy already ran at ``/purchase`` time; the read-back leg
    only needs presence-of-valid-credential).
    """
    message = (
        "X-Wallet-Address or X-Operator-Token header required"
        if reason.code == "missing_identity"
        else "Invalid identity"
    )
    body = denial_reason_to_body(reason)
    body["error"] = {"code": "unauthorized", "message": message}
    return DefaultOnDeniedResult(
        status=401,
        body=body,
        headers={"Cache-Control": "no-store"},
    )
