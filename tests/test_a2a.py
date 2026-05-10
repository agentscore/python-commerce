"""Tests for build_a2a_agent_card (A2A v1.0 spec compliance)."""

import pytest

from agentscore_commerce.identity import (
    UCP_A2A_EXTENSION_URI,
    A2AAgentCardCapabilities,
    A2AAgentCardExtension,
    A2AAgentInterface,
    A2AAgentProvider,
    A2AAgentSkill,
    build_a2a_agent_card,
    ucp_a2a_extension,
)
from agentscore_commerce.identity.a2a import A2AAgentCard


def test_minimum_required_fields_emitted():
    card = build_a2a_agent_card(
        name="Example Merchant",
        description="Buy regulated goods via agent payments.",
        url="https://agents.example.com",
    )
    d = card.to_dict()
    # Per spec §4.4.1: name, description, supported_interfaces, version,
    # capabilities, default_input_modes, default_output_modes are REQUIRED.
    assert d["name"] == "Example Merchant"
    assert d["description"] == "Buy regulated goods via agent payments."
    assert isinstance(d["supported_interfaces"], list)
    assert len(d["supported_interfaces"]) == 1
    assert d["supported_interfaces"][0]["url"] == "https://agents.example.com"
    assert d["supported_interfaces"][0]["protocol_binding"] == "HTTP+JSON"
    assert d["supported_interfaces"][0]["protocol_version"] == "1.0"
    assert d["version"] == "1.0.0"
    assert d["capabilities"] == {}
    assert d["default_input_modes"] == ["application/json"]
    assert d["default_output_modes"] == ["application/json"]


def test_does_not_emit_invented_fields():
    """Confirms we no longer emit `protocol_version` / `card_version` / `endpoints` /
    top-level `identity` / top-level `extensions` — none of these exist in the A2A proto."""
    card = build_a2a_agent_card(name="X", description="y", url="https://x.example")
    d = card.to_dict()
    assert "protocol_version" not in d
    assert "card_version" not in d
    assert "endpoints" not in d
    assert "identity" not in d
    assert "extensions" not in d  # extensions live INSIDE capabilities


def test_skills_serialize_as_top_level_objects_not_strings():
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


def test_skills_omitted_when_empty():
    card = build_a2a_agent_card(name="X", description="y", url="https://x.example")
    assert "skills" not in card.to_dict()


def test_extensions_live_inside_capabilities_not_top_level():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        extensions=[ucp_a2a_extension()],
    )
    d = card.to_dict()
    assert "extensions" not in d  # NOT at top level
    assert "extensions" in d["capabilities"]
    assert len(d["capabilities"]["extensions"]) == 1
    assert d["capabilities"]["extensions"][0]["uri"] == UCP_A2A_EXTENSION_URI


def test_extensions_omitted_when_empty():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        extensions=[],
    )
    assert "extensions" not in card.to_dict()["capabilities"]


def test_capability_flags_emitted_when_set():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        streaming=True,
        push_notifications=False,
        extended_agent_card=True,
    )
    caps = card.to_dict()["capabilities"]
    assert caps["streaming"] is True
    assert caps["push_notifications"] is False
    assert caps["extended_agent_card"] is True


def test_capability_flags_omitted_when_unset():
    card = build_a2a_agent_card(name="X", description="y", url="https://x.example")
    caps = card.to_dict()["capabilities"]
    assert "streaming" not in caps
    assert "push_notifications" not in caps
    assert "extended_agent_card" not in caps


def test_provider_emitted_when_set():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        provider=A2AAgentProvider(url="https://acme.example", organization="Acme"),
    )
    assert card.to_dict()["provider"] == {"url": "https://acme.example", "organization": "Acme"}


def test_documentation_url_emitted_when_set():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        documentation_url="https://docs.example",
    )
    assert card.to_dict()["documentation_url"] == "https://docs.example"


def test_default_input_output_modes_overridable():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain"],
    )
    d = card.to_dict()
    assert d["default_input_modes"] == ["text/plain", "application/json"]
    assert d["default_output_modes"] == ["text/plain"]


def test_protocol_binding_overridable():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        protocol_binding="GRPC",
        a2a_protocol_version="1.0",
    )
    iface = card.to_dict()["supported_interfaces"][0]
    assert iface["protocol_binding"] == "GRPC"


def test_extras_merge_at_top_level():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        extras={"vendor_field": 42},
    )
    assert card.to_dict()["vendor_field"] == 42


def test_security_schemes_emitted_when_set():
    card = build_a2a_agent_card(
        name="X",
        description="y",
        url="https://x.example",
        security_schemes={"bearer": {"type": "http", "scheme": "bearer"}},
    )
    assert card.to_dict()["security_schemes"] == {"bearer": {"type": "http", "scheme": "bearer"}}


# ---- AgentExtension shape ----


def test_agent_extension_emits_required_fields():
    """Per spec §4.4.4: AgentExtension MUST carry uri, description, required."""
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


def test_ucp_a2a_extension_default_args_emit_empty_capabilities():
    ext = ucp_a2a_extension()
    d = ext.to_dict()
    assert d["uri"] == UCP_A2A_EXTENSION_URI
    assert d["description"]  # non-empty per spec
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


def test_agent_interface_emits_required_fields():
    iface = A2AAgentInterface(
        url="https://x.example",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )
    d = iface.to_dict()
    assert d == {
        "url": "https://x.example",
        "protocol_binding": "JSONRPC",
        "protocol_version": "1.0",
    }


def test_agent_interface_tenant_emitted_when_set():
    iface = A2AAgentInterface(
        url="https://x.example",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
        tenant="tenant-123",
    )
    assert iface.to_dict()["tenant"] == "tenant-123"


# ---- AgentSkill ----


def test_agent_skill_required_fields():
    s = A2AAgentSkill(id="x", name="X", description="d", tags=["t"])
    d = s.to_dict()
    assert d == {"id": "x", "name": "X", "description": "d", "tags": ["t"]}


def test_agent_skill_optional_fields_omitted_when_empty():
    s = A2AAgentSkill(id="x", name="X", description="d", tags=["t"])
    d = s.to_dict()
    assert "examples" not in d
    assert "input_modes" not in d
    assert "output_modes" not in d


def test_agent_skill_optional_fields_emitted_when_set():
    s = A2AAgentSkill(
        id="x",
        name="X",
        description="d",
        tags=["t"],
        examples=["buy a wine"],
        input_modes=["application/json"],
        output_modes=["text/plain"],
    )
    d = s.to_dict()
    assert d["examples"] == ["buy a wine"]
    assert d["input_modes"] == ["application/json"]
    assert d["output_modes"] == ["text/plain"]


# ---- Direct AgentCard construction (multi-binding agents) ----


def test_direct_agent_card_construction_with_multiple_interfaces():
    card = A2AAgentCard(
        name="X",
        description="y",
        supported_interfaces=[
            A2AAgentInterface(url="https://x.example/jsonrpc", protocol_binding="JSONRPC", protocol_version="1.0"),
            A2AAgentInterface(url="https://x.example/grpc", protocol_binding="GRPC", protocol_version="1.0"),
        ],
        version="1.0.0",
        capabilities=A2AAgentCardCapabilities(),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
    )
    d = card.to_dict()
    assert len(d["supported_interfaces"]) == 2
    assert d["supported_interfaces"][0]["protocol_binding"] == "JSONRPC"
    assert d["supported_interfaces"][1]["protocol_binding"] == "GRPC"


@pytest.mark.parametrize(
    "missing_kwarg",
    ["name", "description", "url"],
)
def test_required_kwargs_enforced(missing_kwarg: str) -> None:
    """Per spec §4.4.1: name, description, supported_interfaces (built from url) are REQUIRED.

    Our build_a2a_agent_card uses keyword-only required args; missing one raises TypeError.
    """
    kwargs = {"name": "X", "description": "y", "url": "https://x.example"}
    del kwargs[missing_kwarg]
    with pytest.raises(TypeError):
        build_a2a_agent_card(**kwargs)  # type: ignore[arg-type]
