"""Identity-metadata builder for the 402 body (wallet-mode echoer)."""

from dataclasses import dataclass
from typing import Any, Literal

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
