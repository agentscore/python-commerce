"""Builder for ``/.well-known/agent-card.json`` (A2A v1.0).

A2A (Agent-to-Agent) is the schema MCP / agent runtimes consume to discover
endpoint capabilities + compliance requirements before initiating commerce.
Same shape as ``mpp.json`` but consumer-side: ``audience: agents`` rather than
``audience: facilitators``, with explicit endpoint method/path pairs and a
compliance block agents can show users before signing.

Lifted from agentscore/store after every merchant hand-rolled the same
literal dict. Spec: https://a2a-protocol.org/v1/agent-card.json
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class A2AEndpoint:
    """One entry in ``endpoints[]``."""

    path: str
    """URL path (e.g. ``/purchase``, ``/orders/{id}``)."""

    method: str
    """HTTP method (uppercase: ``GET``, ``POST``)."""

    description: str
    """Human-readable summary; agent runtimes surface this to users."""


@dataclass
class A2AComplianceBlock:
    """Optional compliance block — surfaces gating policy to agents pre-call."""

    kyc_required: bool = False
    min_age: int | None = None
    jurisdiction: str | None = None
    """Single jurisdiction shorthand (e.g. ``US``). Use ``allowed_jurisdictions``
    for multi-jurisdiction merchants."""

    allowed_jurisdictions: list[str] | None = None


@dataclass
class A2AAgentCardInput:
    """Input shape for :func:`build_a2a_agent_card`."""

    id: str
    """Stable merchant identifier (kebab-case)."""

    name: str
    """Display name shown in agent UIs."""

    description: str
    """1-2 sentence pitch — what this merchant sells, who it's for."""

    url: str
    """Canonical merchant URL (``https://agents.merchant.com``)."""

    endpoints: list[A2AEndpoint]
    """Discoverable endpoints. Order is preserved in the output."""

    supported_rails: list[str] = field(default_factory=list)
    """Rail symbolic names (``tempo``, ``x402-base``, ``x402-solana``, ``stripe``)."""

    compliance: A2AComplianceBlock | None = None
    """Compliance gating preview, if any."""

    audience: str = "agents"
    """A2A audience tag; vendors should leave at default."""

    schema_version: str = "1.0"
    """A2A schema version — bump only when the spec does."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Vendor-specific fields merged at the top level."""


def build_a2a_agent_card(input: A2AAgentCardInput) -> dict[str, Any]:
    """Build the A2A v1.0 agent card payload."""
    out: dict[str, Any] = {
        "schemaVersion": input.schema_version,
        "id": input.id,
        "name": input.name,
        "description": input.description,
        "url": input.url,
        "audience": input.audience,
    }
    if input.compliance is not None:
        compliance: dict[str, Any] = {"kyc_required": input.compliance.kyc_required}
        if input.compliance.min_age is not None:
            compliance["min_age"] = input.compliance.min_age
        if input.compliance.jurisdiction is not None:
            compliance["jurisdiction"] = input.compliance.jurisdiction
        if input.compliance.allowed_jurisdictions is not None:
            compliance["allowed_jurisdictions"] = input.compliance.allowed_jurisdictions
        out["compliance"] = compliance
    out["endpoints"] = [
        {"path": e.path, "method": e.method.upper(), "description": e.description} for e in input.endpoints
    ]
    if input.supported_rails:
        out["supported_rails"] = input.supported_rails
    out.update(input.extra)
    return out


__all__ = [
    "A2AAgentCardInput",
    "A2AComplianceBlock",
    "A2AEndpoint",
    "build_a2a_agent_card",
]
