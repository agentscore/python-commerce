"""Google A2A (Agent-to-Agent) Signed Agent Cards builder.

Compose the JSON payload for an A2A v1.0 Signed Agent Card that includes the agent's
AgentScore identity claims. Returned object is the unsigned card body — the merchant
(or agent) signs it with their wallet / signing key before publishing.

Why publish: A2A is a Linux Foundation standard with broad cross-vendor adoption.
Signed Agent Cards let any A2A-compatible reader discover an agent's verified-identity
claims without per-platform integration. Publishing operator identity in this format
means our identity travels with the agent across A2A-aware ecosystems.

Spec reference: https://a2a-protocol.org/latest/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentscore_commerce.identity.types import AssessResult

_PROTOCOL_VERSION = "1.0"
_CARD_VERSION = 1


@dataclass
class A2AAgentCardCapabilities:
    """Endpoints the agent exposes + skill tags."""

    endpoints: list[dict[str, str]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.endpoints:
            out["endpoints"] = self.endpoints
        if self.skills:
            out["skills"] = self.skills
        return out


@dataclass
class A2AAgentCardIdentity:
    """AgentScore identity claims embedded in the A2A card."""

    issuer: str
    operator_id: str
    kyc_level: str
    sanctions_clear: bool
    age_bracket: str
    jurisdiction: str
    verified_at: str | None
    verify_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "operator_id": self.operator_id,
            "kyc_level": self.kyc_level,
            "sanctions_clear": self.sanctions_clear,
            "age_bracket": self.age_bracket,
            "jurisdiction": self.jurisdiction,
            "verified_at": self.verified_at,
            "verify_url": self.verify_url,
        }


@dataclass
class A2AAgentCard:
    """A2A v1.0 Agent Card body.

    Use :meth:`to_dict` to serialize for signing + publishing. Signing happens
    vendor-side (the agent's signing key never leaves their environment).
    """

    name: str
    identity: A2AAgentCardIdentity | None = None
    description: str | None = None
    url: str | None = None
    capabilities: A2AAgentCardCapabilities | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    protocol_version: str = _PROTOCOL_VERSION
    card_version: int = _CARD_VERSION

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "card_version": self.card_version,
            "name": self.name,
            "identity": self.identity.to_dict() if self.identity is not None else None,
        }
        if self.description is not None:
            out["description"] = self.description
        if self.url is not None:
            out["url"] = self.url
        if self.capabilities is not None:
            out["capabilities"] = self.capabilities.to_dict()
        if self.extras:
            out.update(self.extras)
        return out


def build_a2a_agent_card(
    name: str,
    description: str | None = None,
    url: str | None = None,
    capabilities: A2AAgentCardCapabilities | None = None,
    data: AssessResult | None = None,
    issuer: str = "https://agentscore.sh",
    verify_url: str | None = None,
    extras: dict[str, Any] | None = None,
) -> A2AAgentCard:
    """Compose an A2A Signed Agent Card body with AgentScore identity claims included.

    Returns the UNSIGNED card. The vendor signs it with their wallet (typically the
    same wallet they use for x402 / MPP payments) and publishes the signed envelope
    to wherever A2A consumers discover cards.

    Pass ``data=None`` to emit a card with no identity claims (publishable but
    unverified).

    Example::

        from agentscore_commerce.identity.a2a import (
            A2AAgentCardCapabilities,
            build_a2a_agent_card,
        )

        result = client.check(identity)
        card = build_a2a_agent_card(
            name="Martin Estate Wine Concierge",
            description="Buy regulated wines from Martin Estate via agent payments.",
            url="https://agents.martinestate.com",
            capabilities=A2AAgentCardCapabilities(
                endpoints=[{"name": "purchase", "path": "/purchase", "method": "POST"}],
                skills=["wine-purchase", "regulated-commerce"],
            ),
            data=result,
        )
        signed = your_sign(card.to_dict())
    """
    identity: A2AAgentCardIdentity | None = None
    if data is not None and data.resolved_operator:
        raw = data.raw or {}
        operator_verification = raw.get("operator_verification") if isinstance(raw, dict) else None
        account_verification = raw.get("account_verification") if isinstance(raw, dict) else None
        if not isinstance(operator_verification, dict):
            operator_verification = {}
        if not isinstance(account_verification, dict):
            account_verification = {}
        identity = A2AAgentCardIdentity(
            issuer=issuer,
            operator_id=data.resolved_operator,
            kyc_level=account_verification.get("kyc_level") or operator_verification.get("level") or "none",
            sanctions_clear=account_verification.get("sanctions_clear") is True,
            age_bracket=account_verification.get("age_bracket", "unknown"),
            jurisdiction=account_verification.get("jurisdiction", ""),
            verified_at=account_verification.get("verified_at") or operator_verification.get("verified_at"),
            verify_url=verify_url or data.verify_url or f"{issuer}/verify",
        )

    return A2AAgentCard(
        name=name,
        identity=identity,
        description=description,
        url=url,
        capabilities=capabilities,
        extras=extras or {},
    )


__all__ = [
    "A2AAgentCard",
    "A2AAgentCardCapabilities",
    "A2AAgentCardIdentity",
    "build_a2a_agent_card",
]
