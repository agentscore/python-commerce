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


@dataclass
class IdentityMetadataInput:
    mode: IdentityMode
    wallet: str | None = None
    signer_match_result: SignerMatchResult | None = None
    linked_wallets: list[str] | None = None
    signer_constraint: str | None = None


def build_identity_metadata(input: IdentityMetadataInput) -> dict[str, Any]:
    """Build the identity-metadata block. Echoes wallet-mode signer requirements so agents can self-correct."""
    block: dict[str, Any] = {"identity_mode": input.mode}
    if input.mode != "wallet":
        return block
    if input.wallet:
        block["required_signer"] = (
            input.signer_match_result.expected_signer
            if input.signer_match_result and input.signer_match_result.expected_signer
            else input.wallet
        )
    if input.linked_wallets:
        block["linked_wallets"] = input.linked_wallets
    block["signer_constraint"] = input.signer_constraint or (
        "Payment must be signed with the claimed wallet OR any same-operator linked wallet listed in linked_wallets."
    )
    return block
