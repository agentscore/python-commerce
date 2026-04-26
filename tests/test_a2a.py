"""Tests for build_a2a_agent_card."""

from agentscore_commerce.identity import (
    A2AAgentCardCapabilities,
    AssessResult,
    build_a2a_agent_card,
)


def _full_result() -> AssessResult:
    return AssessResult(
        allow=True,
        resolved_operator="op_abc",
        verify_url="https://agentscore.sh/verify",
        raw={
            "account_verification": {
                "kyc_level": "enhanced",
                "sanctions_clear": True,
                "age_bracket": "21+",
                "jurisdiction": "US",
                "verified_at": "2026-04-01T00:00:00Z",
            },
        },
    )


def test_card_with_identity_when_data_provided():
    card = build_a2a_agent_card(
        name="Martin Estate",
        url="https://agents.martinestate.com",
        data=_full_result(),
    )
    assert card.protocol_version == "1.0"
    assert card.card_version == 1
    assert card.name == "Martin Estate"
    assert card.url == "https://agents.martinestate.com"
    assert card.identity is not None
    assert card.identity.operator_id == "op_abc"
    assert card.identity.kyc_level == "enhanced"
    assert card.identity.sanctions_clear is True
    assert card.identity.age_bracket == "21+"
    assert card.identity.jurisdiction == "US"


def test_card_with_no_identity_when_data_omitted():
    card = build_a2a_agent_card(name="X")
    assert card.identity is None
    assert card.name == "X"


def test_card_with_no_identity_when_no_resolved_operator():
    card = build_a2a_agent_card(name="X", data=AssessResult(allow=True, resolved_operator=None))
    assert card.identity is None


def test_passes_through_capabilities_description_extras():
    card = build_a2a_agent_card(
        name="X",
        description="test agent",
        capabilities=A2AAgentCardCapabilities(
            endpoints=[{"name": "pay", "method": "POST"}],
            skills=["wine"],
        ),
        extras={"custom": "value"},
    )
    assert card.description == "test agent"
    assert card.capabilities is not None
    assert card.capabilities.endpoints == [{"name": "pay", "method": "POST"}]
    assert card.capabilities.skills == ["wine"]
    assert card.extras == {"custom": "value"}


def test_to_dict_round_trip():
    card = build_a2a_agent_card(
        name="X",
        description="test",
        url="https://x.example",
        capabilities=A2AAgentCardCapabilities(skills=["a", "b"]),
        data=_full_result(),
        extras={"foo": 1},
    )
    d = card.to_dict()
    assert d["protocol_version"] == "1.0"
    assert d["card_version"] == 1
    assert d["name"] == "X"
    assert d["description"] == "test"
    assert d["url"] == "https://x.example"
    assert d["capabilities"] == {"skills": ["a", "b"]}
    assert d["identity"]["operator_id"] == "op_abc"
    assert d["foo"] == 1


def test_respects_issuer_and_verify_url_overrides():
    card = build_a2a_agent_card(
        name="X",
        data=_full_result(),
        issuer="https://other.example",
        verify_url="https://other.example/v",
    )
    assert card.identity is not None
    assert card.identity.issuer == "https://other.example"
    assert card.identity.verify_url == "https://other.example/v"
