"""ERC-8004 (Trustless Agents) attribute publisher.

Format an operator's AgentScore identity into the attribute payload an ERC-8004
registry expects. The merchant (or AgentScore itself) writes the resulting object
on-chain via their own wallet — this helper does NOT submit transactions; it only
shapes the payload so the on-chain write is deterministic.

Why publish: ERC-8004 is the canonical on-chain standard for agent identity (mainnet
Jan 2026, ENS / EigenLayer / Graph / Taiko backed). Publishing operator identity in
this format means any ERC-8004 reader (other agent platforms, on-chain reputation
systems, downstream contracts) can discover AgentScore-verified operators without an
API call.

Spec reference: https://eips.ethereum.org/EIPS/eip-8004
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentscore_commerce.identity.types import AssessResult

AGENTSCORE_ERC8004_SCHEMA = "agentscore.identity.v1"
"""Schema name AgentScore writes for ERC-8004 attributes. Consumers reading from
an ERC-8004 registry filter on this string to find AgentScore-verified operators."""

_SCHEMA_VERSION = 1


@dataclass
class AgentScoreERC8004Attribute:
    """ERC-8004 attribute payload, serialization-friendly.

    Use :meth:`to_dict` to get the dict form for JSON encoding or contract calldata.
    """

    schema: str
    operator_id: str
    jurisdiction: str
    kyc_level: str
    sanctions_clear: bool
    age_bracket: str
    verified_at: str | None
    verify_url: str
    issuer: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "operator_id": self.operator_id,
            "jurisdiction": self.jurisdiction,
            "kyc_level": self.kyc_level,
            "sanctions_clear": self.sanctions_clear,
            "age_bracket": self.age_bracket,
            "verified_at": self.verified_at,
            "verify_url": self.verify_url,
            "issuer": self.issuer,
            "version": self.version,
        }


def build_erc8004_attribute(
    data: AssessResult,
    issuer: str = "https://agentscore.sh",
    verify_url: str | None = None,
) -> AgentScoreERC8004Attribute | None:
    """Format an operator's AgentScore identity as an ERC-8004 attribute payload.

    Returns ``None`` when the assess result lacks the minimum fields needed (no
    operator id — pre-KYC bootstrap state). Caller should check before submitting
    to the ERC-8004 registry contract.

    The actual on-chain write (transaction signing + gas + contract address) is
    vendor-side. This helper just composes the payload so every AgentScore consumer
    publishes the same shape.

    Example::

        from agentscore_commerce.identity.erc8004 import build_erc8004_attribute

        result = client.check(identity=AgentIdentity(operator_token="opc_..."))
        attr = build_erc8004_attribute(result)
        if attr is not None:
            # ... encode to calldata, submit via your wallet to the ERC-8004 registry ...
            pass
    """
    operator_id = data.resolved_operator
    if not operator_id:
        return None

    raw = data.raw or {}
    operator_verification = raw.get("operator_verification") if isinstance(raw, dict) else None
    account_verification = raw.get("account_verification") if isinstance(raw, dict) else None
    if not isinstance(operator_verification, dict):
        operator_verification = {}
    if not isinstance(account_verification, dict):
        account_verification = {}

    resolved_verify_url = (
        verify_url
        or data.verify_url
        or f"{issuer}/verify"
    )

    return AgentScoreERC8004Attribute(
        schema=AGENTSCORE_ERC8004_SCHEMA,
        operator_id=operator_id,
        jurisdiction=account_verification.get("jurisdiction", ""),
        kyc_level=account_verification.get("kyc_level")
        or operator_verification.get("level")
        or "none",
        sanctions_clear=account_verification.get("sanctions_clear") is True,
        age_bracket=account_verification.get("age_bracket", "unknown"),
        verified_at=account_verification.get("verified_at")
        or operator_verification.get("verified_at"),
        verify_url=resolved_verify_url,
        issuer=issuer,
        version=_SCHEMA_VERSION,
    )


__all__ = [
    "AGENTSCORE_ERC8004_SCHEMA",
    "AgentScoreERC8004Attribute",
    "build_erc8004_attribute",
]
