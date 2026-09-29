"""Identity-metadata builder for the 402 body (wallet-mode echoer)."""

from dataclasses import dataclass
from typing import Any, Literal

from agentscore_commerce.payment.payment_header import VERIFICATION_SESSION_HEADER, VERIFICATION_SESSION_VALUE

IdentityMode = Literal["wallet", "operator_token"]


@dataclass
class SignerMatchResult:
    kind: str
    expected_signer: str | None = None
    actual_signer: str | None = None
    linked_wallets: list[str] | None = None


def build_identity_metadata(
    *,
    mode: IdentityMode,
    wallet: str | None = None,
    signer_match_result: SignerMatchResult | None = None,
    linked_wallets: list[str] | None = None,
    signer_constraint: str | None = None,
) -> dict[str, Any]:
    """Build the identity-metadata block. Echoes wallet-mode signer requirements so agents can self-correct."""
    block: dict[str, Any] = {"identity_mode": mode}
    if mode != "wallet":
        return block
    if wallet:
        block["required_signer"] = (
            signer_match_result.expected_signer
            if signer_match_result and signer_match_result.expected_signer
            else wallet
        )
    if linked_wallets:
        block["linked_wallets"] = linked_wallets
    block["signer_constraint"] = signer_constraint or (
        "Payment must be signed with the claimed wallet OR any same-operator linked wallet listed in linked_wallets."
    )
    return block


def build_identity_bootstrap() -> dict[str, str]:
    """Build the ``identity_bootstrap`` block for an identity-gated 402.

    The block rides a 402 whose request carried no identity header. It names the
    ``X-Verification-Session: create`` request that returns the gate's session-bearing 403
    (verify_url + poll data) without a payment credential. ``Checkout`` attaches it automatically;
    merchants building their own 402 with ``build_402_body`` pass it in ``extra``.
    """
    return {
        "header": VERIFICATION_SESSION_HEADER,
        "value": VERIFICATION_SESSION_VALUE,
        "instructions": (
            "This purchase requires a verified identity. Without an operator token, repeat this same "
            f"request with the header {VERIFICATION_SESSION_HEADER}: {VERIFICATION_SESSION_VALUE} and "
            "no payment credential. The response is a 403 carrying verify_url, session_id, poll_secret "
            "and poll_url: give verify_url to the buyer, poll poll_url for an operator_token, then pay "
            "with X-Operator-Token set."
        ),
    }
