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


def classify_mppx_failure(reason: str | None) -> ClassifiedMppxFailure | None:
    """Classify a failure-reason string against known patterns.

    Returns ``None`` when unrecognized — callers fall back to the generic
    ``payment_proof_invalid`` envelope. The reason argument may be the raw
    ``Exception`` message, ``shortMessage`` from a viem-shaped error, or any
    string carrying the upstream description. Substring match, case-insensitive.
    """
    if not reason:
        return None
    lower = reason.lower()
    if "keychain validation failed" in lower or "keynotfound" in lower:
        return _TEMPO_KEY_NOT_REGISTERED
    return None
