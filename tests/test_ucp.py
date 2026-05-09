"""Tests for build_ucp_profile."""

import pytest

from agentscore_commerce.identity import (
    AGENTSCORE_UCP_CAPABILITY,
    AssessResult,
    UCPCapability,
    UCPPaymentHandler,
    UCPService,
    UCPSigningKey,
    build_ucp_profile,
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


def _base_kwargs():
    return {
        "services": [UCPService(type="rest", url="https://agents.example")],
        "signing_keys": [
            UCPSigningKey(kid="me", kty="EC", alg="ES256", crv="P-256", extras={"x": "x", "y": "y"}),
        ],
    }


def test_base_profile_has_required_fields():
    profile = build_ucp_profile(**_base_kwargs())
    d = profile.to_dict()
    assert d["spec"] == "https://ucp.dev/"
    assert "version" in d
    assert d["services"][0]["url"] == "https://agents.example"
    assert d["signing_keys"][0]["kid"] == "me"
    assert d["capabilities"] == []
    assert d["payment_handlers"] == []


def test_appends_agentscore_capability_when_data_provided():
    profile = build_ucp_profile(**_base_kwargs(), data=_full_result())
    d = profile.to_dict()
    matching = [c for c in d["capabilities"] if c["name"] == AGENTSCORE_UCP_CAPABILITY]
    assert len(matching) == 1
    cap = matching[0]
    assert cap["version"] == "1"
    assert "agentscore-identity.v1.json" in cap["schema"]
    claims = cap["claims"]
    assert claims["operator_id"] == "op_abc"
    assert claims["kyc_level"] == "enhanced"
    assert claims["sanctions_clear"] is True
    assert claims["jurisdiction"] == "US"


def test_skips_agentscore_capability_when_no_resolved_operator():
    profile = build_ucp_profile(**_base_kwargs(), data=AssessResult(allow=True, resolved_operator=None))
    d = profile.to_dict()
    assert all(c["name"] != AGENTSCORE_UCP_CAPABILITY for c in d["capabilities"])


def test_preserves_caller_capabilities_and_appends_agentscore():
    profile = build_ucp_profile(
        **_base_kwargs(),
        capabilities=[UCPCapability(name="checkout", version="2")],
        data=_full_result(),
    )
    d = profile.to_dict()
    assert d["capabilities"][0]["name"] == "checkout"
    assert d["capabilities"][1]["name"] == AGENTSCORE_UCP_CAPABILITY


def test_passes_through_name_payment_handlers_extras():
    profile = build_ucp_profile(
        **_base_kwargs(),
        name="Example Merchant",
        payment_handlers=[
            UCPPaymentHandler(name="tempo", config={"recipient": "0xtempo"}),
            UCPPaymentHandler(name="stripe", config={"profile_id": "prof_x"}),
        ],
        extras={"custom_field": "custom_value"},
    )
    d = profile.to_dict()
    assert d["name"] == "Example Merchant"
    assert len(d["payment_handlers"]) == 2
    assert d["custom_field"] == "custom_value"


def test_respects_version_override():
    profile = build_ucp_profile(**_base_kwargs(), version="2026-12-31")
    assert profile.version == "2026-12-31"


def test_respects_agentscore_schema_url_override():
    profile = build_ucp_profile(
        **_base_kwargs(),
        data=_full_result(),
        agentscore_schema_url="https://custom.example/schema.json",
    )
    cap = next(c for c in profile.capabilities if c.name == AGENTSCORE_UCP_CAPABILITY)
    assert cap.schema == "https://custom.example/schema.json"


@pytest.mark.parametrize(
    "key",
    [
        "version",
        "spec",
        "services",
        "capabilities",
        "payment_handlers",
        "signing_keys",
        "name",
        "signature",
        "__proto__",
        "constructor",
        "prototype",
    ],
)
def test_extras_reserved_collision_rejected(key: str) -> None:
    profile = build_ucp_profile(**_base_kwargs(), extras={key: "attacker"})
    with pytest.raises(ValueError, match="collides with a reserved profile field"):
        profile.to_dict()


# Empty-string and null normalization: the API can emit
# ``account_verification`` with either null or ``""`` for un-set fields, and the
# node + python siblings must produce the SAME canonical claims block for either
# shape so a profile signed in one language verifies in the other.


def _claims_of(account_verification: dict) -> dict:
    result = AssessResult(
        allow=True,
        resolved_operator="op_abc",
        raw={"account_verification": account_verification},
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    d = profile.to_dict()
    cap = next(c for c in d["capabilities"] if c["name"] == AGENTSCORE_UCP_CAPABILITY)
    return cap["claims"]


def test_coerces_empty_string_kyc_level_to_none() -> None:
    assert _claims_of({"kyc_level": ""})["kyc_level"] == "none"


def test_coerces_null_age_bracket_to_unknown() -> None:
    assert _claims_of({"age_bracket": None})["age_bracket"] == "unknown"


def test_coerces_empty_string_age_bracket_to_unknown() -> None:
    assert _claims_of({"age_bracket": ""})["age_bracket"] == "unknown"


def test_coerces_null_jurisdiction_to_empty_string() -> None:
    assert _claims_of({"jurisdiction": None})["jurisdiction"] == ""


def test_coerces_empty_string_jurisdiction_to_empty_string() -> None:
    assert _claims_of({"jurisdiction": ""})["jurisdiction"] == ""


def test_coerces_null_verified_at_to_none() -> None:
    assert _claims_of({"verified_at": None})["verified_at"] is None


def test_coerces_empty_string_verified_at_to_none() -> None:
    assert _claims_of({"verified_at": ""})["verified_at"] is None
