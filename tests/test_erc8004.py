"""Tests for build_erc8004_attribute."""

from agentscore_commerce.identity import (
    AGENTSCORE_ERC8004_SCHEMA,
    AssessResult,
    OperatorVerification,
    build_erc8004_attribute,
)


def _full_result() -> AssessResult:
    return AssessResult(
        allow=True,
        decision="allow",
        resolved_operator="op_abc123",
        verify_url="https://agentscore.sh/verify?op=op_abc123",
        operator_verification=OperatorVerification(
            level="verified",
            operator_type="human",
            verified_at="2026-04-01T00:00:00Z",
        ),
        raw={
            "operator_verification": {
                "level": "verified",
                "verified_at": "2026-04-01T00:00:00Z",
            },
            "account_verification": {
                "kyc_level": "enhanced",
                "sanctions_clear": True,
                "age_bracket": "21+",
                "jurisdiction": "US",
                "verified_at": "2026-04-01T00:00:00Z",
            },
        },
    )


def test_returns_none_when_no_resolved_operator():
    result = AssessResult(allow=True, resolved_operator=None)
    assert build_erc8004_attribute(result) is None


def test_formats_full_data_into_canonical_schema():
    attr = build_erc8004_attribute(_full_result())
    assert attr is not None
    assert attr.schema == AGENTSCORE_ERC8004_SCHEMA
    assert attr.operator_id == "op_abc123"
    assert attr.kyc_level == "enhanced"
    assert attr.sanctions_clear is True
    assert attr.age_bracket == "21+"
    assert attr.jurisdiction == "US"
    assert attr.verified_at == "2026-04-01T00:00:00Z"
    assert attr.verify_url == "https://agentscore.sh/verify?op=op_abc123"
    assert attr.issuer == "https://agentscore.sh"
    assert attr.version == 1


def test_to_dict_serializable():
    attr = build_erc8004_attribute(_full_result())
    assert attr is not None
    d = attr.to_dict()
    assert d["operator_id"] == "op_abc123"
    assert d["sanctions_clear"] is True
    assert "schema" in d


def test_falls_back_to_operator_verification_level():
    result = AssessResult(
        allow=True,
        resolved_operator="op_x",
        raw={"operator_verification": {"level": "basic", "verified_at": "2026-04-01T00:00:00Z"}},
    )
    attr = build_erc8004_attribute(result)
    assert attr is not None
    assert attr.kyc_level == "basic"
    assert attr.sanctions_clear is False
    assert attr.age_bracket == "unknown"


def test_respects_custom_issuer_and_verify_url():
    attr = build_erc8004_attribute(
        _full_result(),
        issuer="https://other.example",
        verify_url="https://other.example/v",
    )
    assert attr is not None
    assert attr.issuer == "https://other.example"
    assert attr.verify_url == "https://other.example/v"


def test_sanctions_clear_strict_boolean():
    """Treats absent or non-True account_verification.sanctions_clear as False."""
    result = AssessResult(allow=True, resolved_operator="op_x", raw={})
    attr = build_erc8004_attribute(result)
    assert attr is not None
    assert attr.sanctions_clear is False
