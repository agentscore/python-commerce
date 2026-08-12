"""Classifier for known mppx verification-failure patterns.

When a pympp rail's ``verify()`` throws, the inner error contains a signal
agents can recover from (e.g. the agent's wallet isn't enrolled with
Tempo's keychain). This module maps known failure-reason strings to typed
``ClassifiedMppxFailure`` envelopes so the merchant SDK can return them
instead of the generic ``payment_proof_invalid: regenerate`` body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ClassifiedMppxFailure:
    """Typed envelope a CLI like ``tempo request`` or ``agentscore-pay`` can pattern-match on."""

    code: str
    status: int
    message: str
    next_steps: dict[str, str]
    extra: dict[str, Any] = field(default_factory=dict)


_TEMPO_KEY_NOT_REGISTERED = ClassifiedMppxFailure(
    code="tempo_key_not_registered",
    status=401,
    message=("Tempo rejected the transaction: signer wallet is not registered with Tempo's keychain."),
    next_steps={
        "action": "register_tempo_key",
        "user_message": (
            "Your wallet is not enrolled with Tempo. Run `tempo wallet login` to "
            "complete the one-time WebAuthn enrollment (or use `tempo request` "
            "directly), then retry. To skip enrollment, switch to the Base or "
            "Solana rail on this 402."
        ),
    },
    extra={"upstream_error": "KeyNotFound", "chain": "tempo"},
)


# The dangerous one. pympp verifies a transaction-payload credential by
# broadcasting it (funds move, a signature is minted) and then awaiting
# confirmation in the same verify() call, with a fixed timeout not exposed to
# callers. When confirmation times out (routine under load, since Solana status
# propagation can lag the window even on a production RPC), verify() raises on a
# transaction that MAY HAVE ALREADY LANDED. Left unclassified, that maps to the
# generic payment_proof_invalid + regenerate_payment_credential, i.e. the
# merchant tells the agent to pay AGAIN for money that already left the wallet
# (observed live 2026-08-12: an on-chain balance delta with no service
# delivered and a regenerate 402 in hand). A confirmation timeout cannot be
# reliably told apart from never-landed, so the honest response is 504 with an
# explicit do-not-blindly-resubmit instruction; 504 (unlike 402) does not
# trigger an automatic re-pay retry in x402/MPP clients.
_SOLANA_CONFIRMATION_TIMEOUT = ClassifiedMppxFailure(
    code="payment_pending_confirmation",
    status=504,
    message=(
        "Payment was submitted on-chain but its confirmation timed out. It may "
        "have settled. Do NOT resubmit without checking first, or you risk "
        "paying twice."
    ),
    next_steps={
        "action": "check_settlement_before_retry",
        "user_message": (
            "Your payment was broadcast to the network but confirmation timed "
            "out, so it is unconfirmed rather than failed. Check your wallet "
            "balance and the recipient before retrying: if the balance "
            "decreased, the payment likely landed and you should NOT pay again, "
            "wait for the merchant to reconcile or contact support. Only "
            "resubmit if the funds are still in your wallet."
        ),
    },
    extra={"chain": "solana", "broadcast": True},
)


def classify_mppx_failure(reason: str | None) -> ClassifiedMppxFailure | None:
    """Classify a failure-reason string against known patterns.

    Returns ``None`` when unrecognized: callers fall back to the generic
    ``payment_proof_invalid`` envelope. The reason argument may be the raw
    ``Exception`` message, ``shortMessage`` from a viem-shaped error, or any
    string carrying the upstream description. Substring match, case-insensitive.
    """
    if not reason:
        return None
    lower = reason.lower()
    if "keychain validation failed" in lower or "keynotfound" in lower:
        return _TEMPO_KEY_NOT_REGISTERED
    # A broadcast Solana transfer whose confirmation timed out: money may have
    # moved, so this must never fall through to regenerate_payment_credential.
    if "confirmation timeout" in lower or "confirmation timed out" in lower:
        return _SOLANA_CONFIRMATION_TIMEOUT
    return None
