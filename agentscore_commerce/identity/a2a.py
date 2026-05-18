"""Google A2A (Agent-to-Agent) v1.0 Agent Card builder.

Compose the JSON payload for an A2A v1.0 Agent Card matching the canonical
``AgentCard`` type from ``@a2a-js/sdk``. Returned object is the unsigned card
body — wrap with an ``A2AAgentCardSignature`` (RFC 7515 JWS) to sign vendor-side
before publishing at /.well-known/agent-card.json.

Why publish: A2A is a Linux Foundation standard. Signed Agent Cards let any
A2A-compatible reader discover an agent's capabilities + protocol bindings without
per-platform integration. Per UCP §A2A binding, agents serving UCP via the A2A
transport MUST declare the canonical UCP extension URI in capabilities.extensions[]
so platforms detect UCP support without re-fetching the profile.

Spec reference: https://a2a-protocol.org/latest/
Authoritative types: https://www.npmjs.com/package/@a2a-js/sdk (interface ``AgentCard``).

Python attribute names follow snake_case (PEP 8). The ``to_dict()`` output uses
camelCase keys to match the canonical A2A wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_PROTOCOL_VERSION = "1.0"
_DEFAULT_TRANSPORT = "JSONRPC"
_DEFAULT_BUILDER_TRANSPORT = "HTTP+JSON"
_DEFAULT_INPUT_MODE = "application/json"
_DEFAULT_OUTPUT_MODE = "application/json"

A2A_PROTOCOL_VERSION = _PROTOCOL_VERSION
A2A_DEFAULT_TRANSPORT = _DEFAULT_TRANSPORT

UCP_A2A_EXTENSION_URI = "https://ucp.dev/2026-04-08/specification/reference"
"""Canonical UCP A2A extension URI — verifiers look for this exact URI in
``capabilities.extensions[]`` to detect UCP support on the agent card."""


@dataclass
class A2AAgentInterface:
    """One transport+URL combination the agent exposes.

    Lives in ``AgentCard.additional_interfaces[]`` for multi-binding agents; the
    primary transport+URL pair lives on ``AgentCard.url`` + ``AgentCard.preferred_transport``.
    """

    transport: str
    """Open string — core values are ``JSONRPC``, ``GRPC``, ``HTTP+JSON``."""
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {"transport": self.transport, "url": self.url}


@dataclass
class A2AAgentProvider:
    """Org/service that provides the agent."""

    organization: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"organization": self.organization, "url": self.url}


@dataclass
class A2AAgentSkill:
    """A distinct capability or function the agent performs.

    Lives at the TOP LEVEL of ``AgentCard.skills[]`` (not inside ``capabilities``).
    """

    id: str
    name: str
    description: str
    tags: list[str]
    examples: list[str] = field(default_factory=list)
    input_modes: list[str] = field(default_factory=list)
    output_modes: list[str] = field(default_factory=list)
    security: list[dict[str, list[str]]] = field(default_factory=list)
    """Security schemes scoped to this skill. List = OR of ANDs."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
        }
        if self.examples:
            out["examples"] = self.examples
        if self.input_modes:
            out["inputModes"] = self.input_modes
        if self.output_modes:
            out["outputModes"] = self.output_modes
        if self.security:
            out["security"] = self.security
        return out


@dataclass
class A2AAgentCardExtension:
    """A protocol extension the agent supports.

    Lives in ``capabilities.extensions[]``. Canonical type marks ``description``
    and ``required`` optional, but we keep them in the builder to make UCP
    discovery deterministic.
    """

    uri: str
    description: str
    required: bool = False
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "uri": self.uri,
            "description": self.description,
            "required": self.required,
        }
        if self.params:
            out["params"] = self.params
        return out


def ucp_a2a_extension(
    capabilities: dict[str, list[dict[str, str]]] | None = None,
    *,
    required: bool = False,
) -> A2AAgentCardExtension:
    """Build the canonical UCP entry for an A2A agent card's extensions[] array.

    Per UCP §A2A binding: "Businesses supporting UCP must advertise the extension
    and any optional capabilities in their A2A Agent Card to allow platforms to
    activate the extension." Pass the ``capabilities`` map keyed by reverse-DNS
    service/capability name (e.g. ``dev.ucp.shopping.checkout``), each value a
    list of ``{"version": "..."}`` records. Pass ``None`` (or an empty dict) when
    you serve UCP at the discovery layer but have no formal capability bindings
    yet.

    ``required=True`` declares the platform must understand UCP to interoperate
    with this agent. Default ``False``: UCP is offered but not mandatory.
    """
    return A2AAgentCardExtension(
        uri=UCP_A2A_EXTENSION_URI,
        description="UCP support: this agent serves Universal Commerce Protocol bindings via the A2A transport.",
        required=required,
        params={"capabilities": capabilities or {}},
    )


@dataclass
class A2AAgentCardCapabilities:
    """Optional capabilities the agent supports."""

    extensions: list[A2AAgentCardExtension] = field(default_factory=list)
    push_notifications: bool | None = None
    state_transition_history: bool | None = None
    streaming: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.extensions:
            out["extensions"] = [e.to_dict() for e in self.extensions]
        if self.push_notifications is not None:
            out["pushNotifications"] = self.push_notifications
        if self.state_transition_history is not None:
            out["stateTransitionHistory"] = self.state_transition_history
        if self.streaming is not None:
            out["streaming"] = self.streaming
        return out


@dataclass
class A2AAgentCardSignature:
    """JWS signature embedded in an Agent Card.

    Multiple signatures MAY be attached. Verifiers reconstruct the card body
    without ``signatures`` to verify each entry. Format follows RFC 7515.
    """

    protected: str
    """Base64url-encoded JSON of the protected JWS header. REQUIRED."""
    signature: str
    """Base64url-encoded computed signature. REQUIRED."""
    header: dict[str, Any] = field(default_factory=dict)
    """Optional unprotected JWS header values."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"protected": self.protected, "signature": self.signature}
        if self.header:
            out["header"] = self.header
        return out


@dataclass
class A2AAgentCard:
    """A2A v1.0 Agent Card body, matching ``AgentCard`` from ``@a2a-js/sdk``.

    Use :meth:`to_dict` to serialize for signing + publishing.
    """

    name: str
    description: str
    url: str
    """Preferred endpoint URL — MUST support ``preferred_transport``."""
    protocol_version: str
    """A2A protocol version, e.g. ``"1.0"``. Distinct from the agent's own ``version``."""
    version: str
    """Agent's own version, e.g. ``"1.0.0"``."""
    capabilities: A2AAgentCardCapabilities
    default_input_modes: list[str]
    default_output_modes: list[str]
    skills: list[A2AAgentSkill] = field(default_factory=list)
    """REQUIRED non-empty per spec. ``build_a2a_agent_card`` enforces."""
    preferred_transport: str | None = None
    """Transport at the primary ``url``. Canonical default per spec is ``JSONRPC``;
    our builder sets ``HTTP+JSON`` explicitly for REST-shaped merchants."""
    additional_interfaces: list[A2AAgentInterface] = field(default_factory=list)
    """Additional transport+URL bindings beyond the primary."""
    provider: A2AAgentProvider | None = None
    documentation_url: str | None = None
    icon_url: str | None = None
    supports_authenticated_extended_card: bool | None = None
    """Agent can provide an extended card with additional details to authenticated users."""
    signatures: list[A2AAgentCardSignature] = field(default_factory=list)
    """JWS signatures embedded in the card."""
    security: list[dict[str, list[str]]] = field(default_factory=list)
    """OpenAPI 3.0 security requirement objects (OR of ANDs)."""
    security_schemes: dict[str, Any] = field(default_factory=dict)
    """Map of security scheme definitions (key = scheme name)."""
    extras: dict[str, Any] = field(default_factory=dict)
    """Vendor-specific extras merged at top level."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "protocolVersion": self.protocol_version,
            "version": self.version,
            "capabilities": self.capabilities.to_dict(),
            "defaultInputModes": self.default_input_modes,
            "defaultOutputModes": self.default_output_modes,
        }
        if self.preferred_transport is not None:
            out["preferredTransport"] = self.preferred_transport
        if self.additional_interfaces:
            out["additionalInterfaces"] = [i.to_dict() for i in self.additional_interfaces]
        if self.skills:
            out["skills"] = [s.to_dict() for s in self.skills]
        if self.provider is not None:
            out["provider"] = self.provider.to_dict()
        if self.documentation_url is not None:
            out["documentationUrl"] = self.documentation_url
        if self.icon_url is not None:
            out["iconUrl"] = self.icon_url
        if self.supports_authenticated_extended_card is not None:
            out["supportsAuthenticatedExtendedCard"] = self.supports_authenticated_extended_card
        if self.signatures:
            out["signatures"] = [s.to_dict() for s in self.signatures]
        if self.security:
            out["security"] = self.security
        if self.security_schemes:
            out["securitySchemes"] = self.security_schemes
        for k, v in self.extras.items():
            out[k] = v
        return out


def build_a2a_agent_card(
    *,
    name: str,
    description: str,
    url: str,
    skills: list[A2AAgentSkill],
    version: str = "1.0.0",
    preferred_transport: str = _DEFAULT_BUILDER_TRANSPORT,
    protocol_version: str = _PROTOCOL_VERSION,
    additional_interfaces: list[A2AAgentInterface] | None = None,
    extensions: list[A2AAgentCardExtension] | None = None,
    streaming: bool | None = None,
    push_notifications: bool | None = None,
    state_transition_history: bool | None = None,
    supports_authenticated_extended_card: bool | None = None,
    provider: A2AAgentProvider | None = None,
    documentation_url: str | None = None,
    icon_url: str | None = None,
    signatures: list[A2AAgentCardSignature] | None = None,
    default_input_modes: list[str] | None = None,
    default_output_modes: list[str] | None = None,
    security: list[dict[str, list[str]]] | None = None,
    security_schemes: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> A2AAgentCard:
    """Compose an A2A v1.0 Agent Card body matching ``AgentCard`` from ``@a2a-js/sdk``.

    Returns the UNSIGNED card. To attach identity claims, sign the ``to_dict()``
    output as an RFC 7515 JWS (``A2AAgentCardSignature``). Vendors can also add
    an identity-flavored extension to ``capabilities.extensions[]``.

    The ``url`` argument becomes the top-level ``AgentCard.url``;
    ``preferred_transport`` declares the transport at that URL (default
    ``HTTP+JSON``). For multi-binding agents, pass ``additional_interfaces``.

    Example::

        from agentscore_commerce.identity.a2a import (
            A2AAgentSkill,
            build_a2a_agent_card,
            ucp_a2a_extension,
        )

        card = build_a2a_agent_card(
            name="Example Merchant Concierge",
            description="Buy regulated goods via agent payments.",
            url="https://agents.example.com",
            version="1.0.0",
            skills=[
                A2AAgentSkill(
                    id="purchase",
                    name="Purchase",
                    description="Buy products via agent payments.",
                    tags=["commerce", "payment"],
                ),
            ],
            extensions=[ucp_a2a_extension()],
        )
        signed = your_jws_sign(card.to_dict())
    """
    if not skills:
        msg = (
            "build_a2a_agent_card: `skills` MUST be a non-empty list. Per spec §4.4.1 "
            "(proto field 12 [field_behavior=REQUIRED]), every Agent Card must declare "
            "at least one AgentSkill. Construct A2AAgentCard directly to bypass."
        )
        raise ValueError(msg)
    capabilities = A2AAgentCardCapabilities(
        extensions=extensions or [],
        push_notifications=push_notifications,
        state_transition_history=state_transition_history,
        streaming=streaming,
    )
    return A2AAgentCard(
        name=name,
        description=description,
        url=url,
        protocol_version=protocol_version,
        version=version,
        capabilities=capabilities,
        default_input_modes=default_input_modes or [_DEFAULT_INPUT_MODE],
        default_output_modes=default_output_modes or [_DEFAULT_OUTPUT_MODE],
        skills=skills,
        preferred_transport=preferred_transport,
        additional_interfaces=additional_interfaces or [],
        provider=provider,
        documentation_url=documentation_url,
        icon_url=icon_url,
        supports_authenticated_extended_card=supports_authenticated_extended_card,
        signatures=signatures or [],
        security=security or [],
        security_schemes=security_schemes or {},
        extras=extras or {},
    )


__all__ = [
    "A2A_DEFAULT_TRANSPORT",
    "A2A_PROTOCOL_VERSION",
    "UCP_A2A_EXTENSION_URI",
    "A2AAgentCard",
    "A2AAgentCardCapabilities",
    "A2AAgentCardExtension",
    "A2AAgentCardSignature",
    "A2AAgentInterface",
    "A2AAgentProvider",
    "A2AAgentSkill",
    "build_a2a_agent_card",
    "ucp_a2a_extension",
]
