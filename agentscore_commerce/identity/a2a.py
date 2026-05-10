"""Google A2A (Agent-to-Agent) v1.0 Agent Card builder.

Compose the JSON payload for an A2A v1.0 Agent Card per the canonical proto definition
at https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto. Returned object
is the unsigned card body — wrap with an A2A AgentCardSignature (RFC 7515 JWS) to sign
vendor-side before publishing at /.well-known/agent-card.json.

Why publish: A2A is a Linux Foundation standard. Signed Agent Cards let any
A2A-compatible reader discover an agent's capabilities + protocol bindings without
per-platform integration. Per UCP §A2A binding, agents serving UCP via the A2A
transport MUST declare the canonical UCP extension URI in capabilities.extensions[]
so platforms detect UCP support without re-fetching the profile.

Spec reference: https://a2a-protocol.org/latest/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_PROTOCOL_VERSION = "1.0"
_DEFAULT_PROTOCOL_BINDING = "HTTP+JSON"
_DEFAULT_INPUT_MODE = "application/json"
_DEFAULT_OUTPUT_MODE = "application/json"

UCP_A2A_EXTENSION_URI = "https://ucp.dev/2026-04-08/specification/reference"
"""Canonical UCP A2A extension URI — verifiers look for this exact URI in
``capabilities.extensions[]`` to detect UCP support on the agent card."""


@dataclass
class A2AAgentInterface:
    """Per spec §4.4.6. Each entry advertises one protocol binding the agent supports.

    `supported_interfaces[0]` is the preferred binding (ordered list).
    """

    url: str
    protocol_binding: str
    """Open string — core values are ``JSONRPC``, ``GRPC``, ``HTTP+JSON``."""
    protocol_version: str
    """A2A protocol version, e.g. ``1.0``. Distinct from the agent's own version."""
    tenant: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "url": self.url,
            "protocol_binding": self.protocol_binding,
            "protocol_version": self.protocol_version,
        }
        if self.tenant is not None:
            out["tenant"] = self.tenant
        return out


@dataclass
class A2AAgentProvider:
    """Per spec §4.4.2. The org/service that provides the agent."""

    url: str
    organization: str

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "organization": self.organization}


@dataclass
class A2AAgentSkill:
    """Per spec §4.4.5. A distinct capability or function the agent performs.

    Lives at the TOP LEVEL of AgentCard (not inside ``capabilities``).
    """

    id: str
    name: str
    description: str
    tags: list[str]
    examples: list[str] = field(default_factory=list)
    input_modes: list[str] = field(default_factory=list)
    output_modes: list[str] = field(default_factory=list)

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
            out["input_modes"] = self.input_modes
        if self.output_modes:
            out["output_modes"] = self.output_modes
        return out


@dataclass
class A2AAgentCardExtension:
    """Per spec §4.4.4. A protocol extension the agent supports.

    Lives in ``capabilities.extensions[]``. ``description`` and ``required`` are
    spec-mandated fields, not optional.
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
    """Per spec §4.4.3. Optional capabilities the agent supports.

    Per the canonical proto, ``capabilities`` declares: streaming, push_notifications,
    extensions (the protocol extensions the agent supports), and extended_agent_card.
    REST-style endpoint metadata does NOT belong here — A2A uses ``supported_interfaces``
    on the AgentCard for protocol bindings, and ``skills`` (top-level) for capability
    descriptions.
    """

    streaming: bool | None = None
    push_notifications: bool | None = None
    extensions: list[A2AAgentCardExtension] = field(default_factory=list)
    extended_agent_card: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.streaming is not None:
            out["streaming"] = self.streaming
        if self.push_notifications is not None:
            out["push_notifications"] = self.push_notifications
        if self.extensions:
            out["extensions"] = [e.to_dict() for e in self.extensions]
        if self.extended_agent_card is not None:
            out["extended_agent_card"] = self.extended_agent_card
        return out


@dataclass
class A2AAgentCard:
    """Per spec §4.4.1. A2A v1.0 Agent Card body.

    Use :meth:`to_dict` to serialize for signing + publishing. Identity claims live
    in a separate ``AgentCardSignature`` (RFC 7515 JWS) wrapping the serialized card,
    NOT in the card body itself. Per-vendor identity attestation can be expressed via
    a vendor extension entry inside ``capabilities.extensions[]``.
    """

    name: str
    description: str
    supported_interfaces: list[A2AAgentInterface]
    version: str
    """Agent's own version, e.g. ``"1.0.0"``. Distinct from the A2A protocol version,
    which lives on each ``A2AAgentInterface.protocol_version``."""
    capabilities: A2AAgentCardCapabilities
    default_input_modes: list[str]
    default_output_modes: list[str]
    provider: A2AAgentProvider | None = None
    documentation_url: str | None = None
    skills: list[A2AAgentSkill] = field(default_factory=list)
    security_schemes: dict[str, Any] = field(default_factory=dict)
    security_requirements: list[Any] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "supported_interfaces": [i.to_dict() for i in self.supported_interfaces],
            "version": self.version,
            "capabilities": self.capabilities.to_dict(),
            "default_input_modes": self.default_input_modes,
            "default_output_modes": self.default_output_modes,
        }
        if self.provider is not None:
            out["provider"] = self.provider.to_dict()
        if self.documentation_url is not None:
            out["documentation_url"] = self.documentation_url
        if self.skills:
            out["skills"] = [s.to_dict() for s in self.skills]
        if self.security_schemes:
            out["security_schemes"] = self.security_schemes
        if self.security_requirements:
            out["security_requirements"] = self.security_requirements
        for k, v in self.extras.items():
            out[k] = v
        return out


def build_a2a_agent_card(
    *,
    name: str,
    description: str,
    url: str,
    version: str = "1.0.0",
    skills: list[A2AAgentSkill] | None = None,
    extensions: list[A2AAgentCardExtension] | None = None,
    streaming: bool | None = None,
    push_notifications: bool | None = None,
    extended_agent_card: bool | None = None,
    provider: A2AAgentProvider | None = None,
    documentation_url: str | None = None,
    default_input_modes: list[str] | None = None,
    default_output_modes: list[str] | None = None,
    protocol_binding: str = _DEFAULT_PROTOCOL_BINDING,
    a2a_protocol_version: str = _PROTOCOL_VERSION,
    security_schemes: dict[str, Any] | None = None,
    security_requirements: list[Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> A2AAgentCard:
    """Compose an A2A v1.0 Agent Card body per the canonical proto.

    Returns the UNSIGNED card. To attach identity claims, sign the ``to_dict()``
    output as an RFC 7515 JWS (``AgentCardSignature``). Vendors can also add an
    identity-flavored extension to ``capabilities.extensions[]``.

    The single ``url`` argument becomes the primary ``supported_interfaces[0].url``
    (with ``protocol_binding=HTTP+JSON``, ``protocol_version=1.0`` by default).
    Override these via the ``protocol_binding`` and ``a2a_protocol_version`` kwargs,
    or build ``A2AAgentInterface`` objects directly via the dataclass for multi-binding
    agents (in which case construct the ``A2AAgentCard`` directly).

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
    capabilities = A2AAgentCardCapabilities(
        streaming=streaming,
        push_notifications=push_notifications,
        extensions=extensions or [],
        extended_agent_card=extended_agent_card,
    )
    interface = A2AAgentInterface(
        url=url,
        protocol_binding=protocol_binding,
        protocol_version=a2a_protocol_version,
    )
    return A2AAgentCard(
        name=name,
        description=description,
        supported_interfaces=[interface],
        version=version,
        capabilities=capabilities,
        default_input_modes=default_input_modes or [_DEFAULT_INPUT_MODE],
        default_output_modes=default_output_modes or [_DEFAULT_OUTPUT_MODE],
        provider=provider,
        documentation_url=documentation_url,
        skills=skills or [],
        security_schemes=security_schemes or {},
        security_requirements=security_requirements or [],
        extras=extras or {},
    )


__all__ = [
    "UCP_A2A_EXTENSION_URI",
    "A2AAgentCard",
    "A2AAgentCardCapabilities",
    "A2AAgentCardExtension",
    "A2AAgentInterface",
    "A2AAgentProvider",
    "A2AAgentSkill",
    "build_a2a_agent_card",
    "ucp_a2a_extension",
]
