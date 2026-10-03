"""The card we build parses as the official A2A ``AgentCard`` protobuf.

``json_format.ParseDict`` refuses any field the current A2A specification does not
define, so a field we emit that the spec renamed or removed fails here by name. This
is what would have caught the card sitting a whole protocol version behind.
"""

from a2a.types import AgentCard
from google.protobuf import json_format

from agentscore_commerce.identity.a2a import (
    A2AAgentInterface,
    A2AAgentProvider,
    A2AAgentSkill,
    build_a2a_agent_card,
    ucp_a2a_extension,
)


def _full_card() -> dict:
    return build_a2a_agent_card(
        name="Example Merchant",
        description="Buy products via agent payments.",
        url="https://agents.example.com",
        version="1.0.0",
        skills=[
            A2AAgentSkill(
                id="purchase",
                name="Purchase",
                description="Buy products via agent payments.",
                tags=["commerce", "payment"],
                examples=["buy a wine"],
                security=[{"bearer": ["read"]}],
            )
        ],
        extensions=[ucp_a2a_extension({"dev.ucp.shopping.checkout": [{"version": "2026-08-25"}]}, required=True)],
        documentation_url="https://agents.example.com/docs",
        icon_url="https://agents.example.com/icon.png",
        provider=A2AAgentProvider(organization="Example Inc", url="https://example.com"),
        push_notifications=True,
        streaming=True,
        extended_agent_card=True,
        security=[{"bearer": []}],
        additional_interfaces=[A2AAgentInterface(transport="GRPC", url="https://agents.example.com/grpc")],
    ).to_dict()


def test_a_fully_populated_card_parses_as_the_official_agent_card():
    parsed = json_format.ParseDict(_full_card(), AgentCard())
    assert parsed.supported_interfaces[0].url == "https://agents.example.com"
    assert parsed.capabilities.extended_agent_card is True


def test_a_field_the_spec_does_not_define_is_refused_which_is_what_makes_this_a_guard():
    stale = {**_full_card(), "url": "https://agents.example.com", "preferredTransport": "HTTP+JSON"}
    try:
        json_format.ParseDict(stale, AgentCard())
    except json_format.ParseError as e:
        assert "url" in str(e)
    else:
        raise AssertionError("a 0.3-shaped card parsed as A2A 1.0")
