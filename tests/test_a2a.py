"""Tests for build_a2a_agent_card (A2A v1.0 wire format)."""

import json

import pytest

from agentscore_commerce.identity import (
    A2A_DEFAULT_TRANSPORT,
    A2A_PROTOCOL_VERSION,
    UCP_A2A_EXTENSION_URI,
    A2AAgentCardCapabilities,
    A2AAgentCardExtension,
    A2AAgentCardSignature,
    A2AAgentInterface,
    A2AAgentProvider,
    A2AAgentSkill,
    build_a2a_agent_card,
    ucp_a2a_extension,
)
from agentscore_commerce.identity.a2a import A2AAgentCard

_DEFAULT_SKILL = A2AAgentSkill(
    id="purchase",
    name="Purchase",
    description="Buy products via agent payments.",
    tags=["commerce", "payment"],
)


def test_minimum_required_fields_emitted():
    card = build_a2a_agent_card(
        name="Example Merchant",
        description="Buy regulated goods via agent payments.",
        url="https://agents.example.com",
        skills=[_DEFAULT_SKILL],
    )
    d = card.to_dict()
    assert d["name"] == "Example Merchant"
    assert d["description"] == "Buy regulated goods via agent payments."
    assert d["url"] == "https://agents.example.com"
    assert d["preferredTransport"] == "HTTP+JSON"
    assert d["protocolVersion"] == "1.0"
    assert d["version"] == "1.0.0"
    assert d["capabilities"] == {}
    assert d["defaultInputModes"] == ["application/json"]
    assert d["defaultOutputModes"] == ["application/json"]
    assert len(d["skills"]) == 1
    assert "additionalInterfaces" not in d


def test_emits_only_camelcase_keys():
    """Canonical A2A wire format is camelCase. No snake_case keys must leak through to_dict()."""
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        push_notifications=True,
        state_transition_history=False,
        documentation_url="https://docs.example",
        icon_url="https://x.example/icon.png",
        supports_authenticated_extended_card=True,
    )
    serialized = json.dumps(card.to_dict())
    for bad in (
        "supported_interfaces",
        "protocol_binding",
        "protocol_version",
        "default_input_modes",
        "default_output_modes",
        "documentation_url",
        "icon_url",
        "push_notifications",
        "state_transition_history",
        "extended_agent_card",
        "security_schemes",
        "security_requirements",
        "input_modes",
        "output_modes",
        "supports_authenticated_extended_card",
        "additional_interfaces",
        "preferred_transport",
    ):
        assert bad not in serialized, f"snake_case key {bad!r} leaked into wire format"


def test_skills_required_non_empty():
    with pytest.raises(ValueError, match="MUST be a non-empty list"):
        build_a2a_agent_card(
            name="X",
            description="y",
            url="https://x.example",
            skills=[],
        )


def test_does_not_emit_invented_fields():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
    )
    d = card.to_dict()
    assert "supported_interfaces" not in d
    assert "endpoints" not in d
    assert "identity" not in d
    assert "card_version" not in d
    assert "extensions" not in d  # extensions live INSIDE capabilities


def test_skills_serialize_as_top_level_objects():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[
            A2AAgentSkill(
                id="purchase",
                name="Purchase",
                description="Buy products via agent payments.",
                tags=["commerce", "payment"],
            ),
        ],
    )
    d = card.to_dict()
    assert d["skills"] == [
        {
            "id": "purchase",
            "name": "Purchase",
            "description": "Buy products via agent payments.",
            "tags": ["commerce", "payment"],
        },
    ]


def test_extensions_live_inside_capabilities():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        extensions=[ucp_a2a_extension()],
    )
    d = card.to_dict()
    assert "extensions" not in d
    assert "extensions" in d["capabilities"]
    assert len(d["capabilities"]["extensions"]) == 1
    assert d["capabilities"]["extensions"][0]["uri"] == UCP_A2A_EXTENSION_URI


def test_extensions_omitted_when_empty():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        extensions=[],
    )
    assert "extensions" not in card.to_dict()["capabilities"]


def test_capability_flags_emitted_when_set():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        streaming=True,
        push_notifications=False,
        state_transition_history=True,
    )
    caps = card.to_dict()["capabilities"]
    assert caps["streaming"] is True
    assert caps["pushNotifications"] is False
    assert caps["stateTransitionHistory"] is True


def test_capability_flags_omitted_when_unset():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
    )
    caps = card.to_dict()["capabilities"]
    assert "streaming" not in caps
    assert "pushNotifications" not in caps
    assert "stateTransitionHistory" not in caps


def test_supports_authenticated_extended_card_at_top_level():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        supports_authenticated_extended_card=True,
    )
    d = card.to_dict()
    assert d["supportsAuthenticatedExtendedCard"] is True
    assert "supportsAuthenticatedExtendedCard" not in d["capabilities"]


def test_provider_emitted_when_set():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        provider=A2AAgentProvider(organization="Acme", url="https://acme.example"),
    )
    assert card.to_dict()["provider"] == {"organization": "Acme", "url": "https://acme.example"}


def test_documentation_url_emitted_as_camelcase():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        documentation_url="https://docs.example",
    )
    d = card.to_dict()
    assert d["documentationUrl"] == "https://docs.example"
    assert "documentation_url" not in d


def test_icon_url_emitted_as_camelcase():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        icon_url="https://x.example/icon.png",
    )
    d = card.to_dict()
    assert d["iconUrl"] == "https://x.example/icon.png"
    assert "icon_url" not in d


def test_signatures_emitted_when_set():
    sig = A2AAgentCardSignature(protected="eyJhbGciOiJFZERTQSJ9", signature="abc")
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        signatures=[sig],
    )
    assert card.to_dict()["signatures"] == [
        {"protected": "eyJhbGciOiJFZERTQSJ9", "signature": "abc"},
    ]


def test_signatures_omitted_when_empty():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
    )
    assert "signatures" not in card.to_dict()


def test_default_input_output_modes_overridable():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain"],
    )
    d = card.to_dict()
    assert d["defaultInputModes"] == ["text/plain", "application/json"]
    assert d["defaultOutputModes"] == ["text/plain"]


def test_preferred_transport_overridable():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        preferred_transport="GRPC",
        protocol_version="1.0",
    )
    d = card.to_dict()
    assert d["preferredTransport"] == "GRPC"


def test_additional_interfaces_emitted_when_set():
    ifaces = [
        A2AAgentInterface(transport="GRPC", url="https://x.example/grpc"),
        A2AAgentInterface(transport="JSONRPC", url="https://x.example/jsonrpc"),
    ]
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        additional_interfaces=ifaces,
    )
    d = card.to_dict()
    assert d["additionalInterfaces"] == [
        {"transport": "GRPC", "url": "https://x.example/grpc"},
        {"transport": "JSONRPC", "url": "https://x.example/jsonrpc"},
    ]


def test_additional_interfaces_omitted_when_empty():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        additional_interfaces=[],
    )
    assert "additionalInterfaces" not in card.to_dict()


def test_extras_merge_at_top_level():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        extras={"vendorField": 42},
    )
    assert card.to_dict()["vendorField"] == 42


def test_security_and_security_schemes_emitted_camelcase():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        skills=[_DEFAULT_SKILL],
        security=[{"bearer": []}],
        security_schemes={"bearer": {"type": "http", "scheme": "bearer"}},
    )
    d = card.to_dict()
    assert d["security"] == [{"bearer": []}]
    assert d["securitySchemes"] == {"bearer": {"type": "http", "scheme": "bearer"}}
    assert "security_schemes" not in d
    assert "security_requirements" not in d


# ---- AgentExtension shape ----


def test_agent_extension_emits_required_fields():
    ext = A2AAgentCardExtension(uri="https://example/ext", description="test", required=True)
    d = ext.to_dict()
    assert d["uri"] == "https://example/ext"
    assert d["description"] == "test"
    assert d["required"] is True


def test_agent_extension_required_defaults_false():
    ext = A2AAgentCardExtension(uri="https://example/ext", description="test")
    assert ext.to_dict()["required"] is False


def test_agent_extension_params_omitted_when_empty():
    ext = A2AAgentCardExtension(uri="u", description="d")
    assert "params" not in ext.to_dict()


def test_agent_extension_params_emitted_when_set():
    ext = A2AAgentCardExtension(uri="u", description="d", params={"k": "v"})
    assert ext.to_dict()["params"] == {"k": "v"}


# ---- ucp_a2a_extension helper ----


def test_ucp_a2a_extension_uri_pinned():
    assert UCP_A2A_EXTENSION_URI == "https://ucp.dev/2026-04-08/specification/reference"


def test_a2a_constants_exported():
    assert A2A_PROTOCOL_VERSION == "1.0"
    assert A2A_DEFAULT_TRANSPORT == "JSONRPC"


def test_ucp_a2a_extension_default_args_emit_empty_capabilities():
    ext = ucp_a2a_extension()
    d = ext.to_dict()
    assert d["uri"] == UCP_A2A_EXTENSION_URI
    assert d["description"]
    assert d["required"] is False
    assert d["params"] == {"capabilities": {}}


def test_ucp_a2a_extension_passes_capabilities_under_params():
    ext = ucp_a2a_extension(
        {
            "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}],
            "dev.ucp.shopping.cart": [{"version": "2026-04-08"}],
        },
    )
    assert ext.params["capabilities"] == {
        "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}],
        "dev.ucp.shopping.cart": [{"version": "2026-04-08"}],
    }


def test_ucp_a2a_extension_required_kwarg():
    ext = ucp_a2a_extension(required=True)
    assert ext.required is True


# ---- AgentInterface ----


def test_agent_interface_emits_canonical_shape():
    iface = A2AAgentInterface(transport="JSONRPC", url="https://x.example")
    assert iface.to_dict() == {"transport": "JSONRPC", "url": "https://x.example"}


# ---- AgentSkill ----


def test_agent_skill_required_fields():
    s = A2AAgentSkill(id="x", name="X", description="d", tags=["t"])
    d = s.to_dict()
    assert d == {"id": "x", "name": "X", "description": "d", "tags": ["t"]}


def test_agent_skill_optional_fields_omitted_when_empty():
    s = A2AAgentSkill(id="x", name="X", description="d", tags=["t"])
    d = s.to_dict()
    assert "examples" not in d
    assert "inputModes" not in d
    assert "outputModes" not in d
    assert "security" not in d


def test_agent_skill_optional_fields_emitted_camelcase():
    s = A2AAgentSkill(
        id="x",
        name="X",
        description="d",
        tags=["t"],
        examples=["buy a wine"],
        input_modes=["application/json"],
        output_modes=["text/plain"],
        security=[{"bearer": []}],
    )
    d = s.to_dict()
    assert d["examples"] == ["buy a wine"]
    assert d["inputModes"] == ["application/json"]
    assert d["outputModes"] == ["text/plain"]
    assert d["security"] == [{"bearer": []}]
    assert "input_modes" not in d
    assert "output_modes" not in d


# ---- AgentCardSignature ----


def test_agent_card_signature_required_fields():
    sig = A2AAgentCardSignature(protected="eyJ...", signature="abc")
    d = sig.to_dict()
    assert d == {"protected": "eyJ...", "signature": "abc"}


def test_agent_card_signature_unprotected_header_emitted_when_set():
    sig = A2AAgentCardSignature(
        protected="eyJ...",
        signature="abc",
        header={"kid": "key-1"},
    )
    assert sig.to_dict()["header"] == {"kid": "key-1"}


# ---- Direct AgentCard construction (multi-binding agents) ----


def test_direct_agent_card_construction_with_additional_interfaces():
    card = A2AAgentCard(
        name="X",
        description="y",
        url="https://x.example",
        protocol_version="1.0",
        version="1.0.0",
        capabilities=A2AAgentCardCapabilities(),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[_DEFAULT_SKILL],
        preferred_transport="HTTP+JSON",
        additional_interfaces=[
            A2AAgentInterface(transport="JSONRPC", url="https://x.example/jsonrpc"),
            A2AAgentInterface(transport="GRPC", url="https://x.example/grpc"),
        ],
    )
    d = card.to_dict()
    assert d["url"] == "https://x.example"
    assert d["preferredTransport"] == "HTTP+JSON"
    assert len(d["additionalInterfaces"]) == 2
    assert d["additionalInterfaces"][0]["transport"] == "JSONRPC"


@pytest.mark.parametrize(
    "missing_kwarg",
    ["name", "description", "url", "skills"],
)
def test_required_kwargs_enforced(missing_kwarg: str) -> None:
    kwargs: dict = {
        "name": "X",
        "description": "y",
        "url": "https://x.example",
        "skills": [_DEFAULT_SKILL],
    }
    del kwargs[missing_kwarg]
    with pytest.raises((TypeError, ValueError)):
        build_a2a_agent_card(**kwargs)
