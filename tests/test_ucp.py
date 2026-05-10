"""Tests for build_ucp_profile (spec-compliant shape)."""

from typing import cast

import pytest

from agentscore_commerce.identity import (
    AGENTSCORE_UCP_CAPABILITY,
    AssessResult,
    OperatorVerification,
    UCPCapabilityBinding,
    UCPPaymentHandlerBinding,
    UCPProfileBody,
    UCPServiceBinding,
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


def _sample_service() -> UCPServiceBinding:
    return UCPServiceBinding(
        version="2026-04-08",
        spec="https://ucp.dev/2026-04-08/specification/overview",
        transport="mcp",
        endpoint="https://agents.example/api/ucp/mcp",
        schema="https://ucp.dev/services/shopping/openrpc.json",
    )


def _base_kwargs():
    return {
        "services": {"dev.ucp.shopping": [_sample_service()]},
        "signing_keys": [
            UCPSigningKey(kid="me", kty="EC", alg="ES256", crv="P-256", extras={"x": "x", "y": "y"}),
        ],
    }


def _agentscore_cap(d: dict) -> dict:
    return d["ucp"]["capabilities"][AGENTSCORE_UCP_CAPABILITY][0]


def test_emits_spec_envelope_with_ucp_body_and_outer_signing_keys():
    profile = build_ucp_profile(**_base_kwargs())
    d = profile.to_dict()
    assert "ucp" in d
    assert "signing_keys" in d
    # No top-level `spec` field per UCP spec — spec lives per-binding.
    assert "spec" not in d
    assert "version" not in d  # version lives under `ucp`
    assert d["ucp"]["version"]
    assert d["ucp"]["services"]["dev.ucp.shopping"][0]["transport"] == "mcp"
    assert d["ucp"]["capabilities"] == {}
    assert d["ucp"]["payment_handlers"] == {}
    assert d["signing_keys"][0]["kid"] == "me"


def test_appends_agentscore_capability_when_data_provided():
    profile = build_ucp_profile(**_base_kwargs(), data=_full_result())
    d = profile.to_dict()
    cap = _agentscore_cap(d)
    assert cap["version"] == "1"
    assert "sh-agentscore-identity-v1.json" in cap["schema"]
    # Multi-parent extends — matches Shopify's dev.shopify.catalog.storefront pattern
    # and UCP-canonical dev.ucp.shopping.discount (extends [checkout, cart]).
    assert cap["extends"] == ["dev.ucp.shopping.checkout", "dev.ucp.shopping.cart"]
    claims = cap["claims"]
    assert claims["operator_id"] == "op_abc"
    assert claims["kyc_level"] == "enhanced"
    assert claims["sanctions_clear"] is True
    assert claims["jurisdiction"] == "US"


def test_skips_agentscore_capability_when_no_resolved_operator():
    profile = build_ucp_profile(**_base_kwargs(), data=AssessResult(allow=True, resolved_operator=None))
    d = profile.to_dict()
    assert AGENTSCORE_UCP_CAPABILITY not in d["ucp"]["capabilities"]


def test_preserves_caller_capabilities_and_appends_agentscore():
    checkout_binding = UCPCapabilityBinding(
        version="2026-04-08",
        spec="https://ucp.dev/2026-04-08/specification/checkout",
        schema="https://ucp.dev/2026-04-08/schemas/shopping/checkout.json",
    )
    profile = build_ucp_profile(
        **_base_kwargs(),
        capabilities={"dev.ucp.shopping.checkout": [checkout_binding]},
        data=_full_result(),
    )
    d = profile.to_dict()
    assert d["ucp"]["capabilities"]["dev.ucp.shopping.checkout"][0]["version"] == "2026-04-08"
    assert _agentscore_cap(d)["version"] == "1"


def test_passes_through_name_payment_handlers_extras():
    tempo_handler = UCPPaymentHandlerBinding(
        id="tempo",
        version="2026-04-08",
        spec="https://agentscore.sh/specification/payment-handlers/tempo",
        schema="https://agentscore.sh/schemas/payment-handlers/tempo.json",
        config={"recipient": "0xtempo"},
    )
    profile = build_ucp_profile(
        **_base_kwargs(),
        name="Example Merchant",
        payment_handlers={"sh.agentscore.payment.tempo": [tempo_handler]},
        extras={"custom_top_level": "top_value"},
        ucp_extras={"custom_ucp_field": "ucp_value"},
    )
    d = profile.to_dict()
    assert d["ucp"]["name"] == "Example Merchant"
    assert d["ucp"]["payment_handlers"]["sh.agentscore.payment.tempo"][0]["id"] == "tempo"
    assert d["custom_top_level"] == "top_value"
    assert d["ucp"]["custom_ucp_field"] == "ucp_value"


def test_payment_handler_omits_config_when_caller_does_not_set_it():
    handler = UCPPaymentHandlerBinding(
        id="tempo",
        version="2026-04-08",
        spec="https://agentscore.sh/specification/payment-handlers/tempo",
        schema="https://agentscore.sh/schemas/payment-handlers/tempo.json",
    )
    profile = build_ucp_profile(
        **_base_kwargs(),
        payment_handlers={"sh.agentscore.payment.tempo": [handler]},
    )
    d = profile.to_dict()
    serialized = d["ucp"]["payment_handlers"]["sh.agentscore.payment.tempo"][0]
    assert "config" not in serialized


def test_respects_version_override():
    profile = build_ucp_profile(**_base_kwargs(), version="2026-12-31")
    assert profile.ucp.version == "2026-12-31"


def test_respects_agentscore_schema_url_override():
    profile = build_ucp_profile(
        **_base_kwargs(),
        data=_full_result(),
        agentscore_schema_url="https://custom.example/schema.json",
    )
    cap = profile.ucp.capabilities[AGENTSCORE_UCP_CAPABILITY][0]
    assert cap.schema == "https://custom.example/schema.json"


def test_respects_agentscore_spec_url_override():
    profile = build_ucp_profile(
        **_base_kwargs(),
        data=_full_result(),
        agentscore_spec_url="https://custom.example/spec",
    )
    cap = profile.ucp.capabilities[AGENTSCORE_UCP_CAPABILITY][0]
    assert cap.spec == "https://custom.example/spec"


def test_emits_supported_versions_map_when_supplied():
    profile = build_ucp_profile(
        **_base_kwargs(),
        supported_versions={
            "2026-04-08": "https://merchant.example/.well-known/ucp/2026-04-08",
            "2026-01-23": "https://merchant.example/.well-known/ucp/2026-01-23",
        },
    )
    d = profile.to_dict()
    assert d["ucp"]["supported_versions"]["2026-04-08"].endswith("/2026-04-08")


@pytest.mark.parametrize(
    "key",
    ["ucp", "signing_keys", "signature", "__proto__", "constructor", "prototype"],
)
def test_extras_top_level_reserved_collision_rejected(key: str) -> None:
    profile = build_ucp_profile(**_base_kwargs(), extras={key: "attacker"})
    with pytest.raises(ValueError, match="collides with a reserved profile field"):
        profile.to_dict()


@pytest.mark.parametrize(
    "key",
    [
        "version",
        "name",
        "services",
        "capabilities",
        "payment_handlers",
        "supported_versions",
        "__proto__",
        "constructor",
        "prototype",
    ],
)
def test_ucp_extras_reserved_collision_rejected(key: str) -> None:
    profile = build_ucp_profile(**_base_kwargs(), ucp_extras={key: "attacker"})
    with pytest.raises(ValueError, match="collides with a reserved `ucp` field"):
        profile.to_dict()


# Empty-string and null normalization: the API can emit `account_verification` with
# either null or "" for un-set fields, and the node + python siblings must produce
# the SAME canonical claims block for either shape so a profile signed in one
# language verifies in the other.


def _claims_of(account_verification: dict, operator_verification: dict | None = None) -> dict:
    raw: dict = {"account_verification": account_verification}
    if operator_verification is not None:
        raw["operator_verification"] = operator_verification
    result = AssessResult(allow=True, resolved_operator="op_abc", raw=raw)
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    d = profile.to_dict()
    return _agentscore_cap(d)["claims"]


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


def test_both_empty_string_verified_at_normalizes_to_none() -> None:
    """Both account_verification + operator_verification with verified_at=""
    must normalize to None for cross-language byte parity with Node SDK.
    """
    assert (
        _claims_of(
            {"verified_at": ""},
            operator_verification={"verified_at": ""},
        )["verified_at"]
        is None
    )


# Typed-field fallback: production callers populate `data.raw`, but a
# hand-constructed AssessResult (no raw) should still surface the verification
# block via the typed `AssessResult.operator_verification` /
# `AssessResult.account_verification` fields. Mirrors the node sibling's
# typed-field read path.


def test_typed_operator_verification_fallback_when_raw_is_none() -> None:
    result = AssessResult(
        allow=True,
        resolved_operator="op_typed",
        operator_verification=OperatorVerification(
            level="enhanced",
            operator_type="api",
            verified_at="2026-04-01T00:00:00Z",
        ),
        raw=None,
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    d = profile.to_dict()
    claims = _agentscore_cap(d)["claims"]
    assert claims["operator_id"] == "op_typed"
    assert claims["kyc_level"] == "enhanced"
    assert claims["verified_at"] == "2026-04-01T00:00:00Z"


def test_typed_account_verification_fallback_when_raw_is_none() -> None:
    result = AssessResult(
        allow=True,
        resolved_operator="op_typed",
        operator_verification=OperatorVerification(level="verified"),
        account_verification={
            "kyc_level": "verified",
            "age_bracket": "21+",
            "jurisdiction": "US",
            "sanctions_clear": True,
        },
        raw=None,
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    d = profile.to_dict()
    claims = _agentscore_cap(d)["claims"]
    assert claims["kyc_level"] == "verified"
    assert claims["age_bracket"] == "21+"
    assert claims["jurisdiction"] == "US"
    assert claims["sanctions_clear"] is True


def test_typed_takes_precedence_over_raw() -> None:
    result = AssessResult(
        allow=True,
        resolved_operator="op_xyz",
        operator_verification=OperatorVerification(level="verified"),
        account_verification={"kyc_level": "verified"},
        raw={
            "operator_verification": {"level": "none"},
            "account_verification": {"kyc_level": "none"},
        },
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    cap = profile.ucp.capabilities[AGENTSCORE_UCP_CAPABILITY][0]
    assert cap.extras["claims"]["kyc_level"] == "verified"


def test_raw_fallback_used_when_typed_missing() -> None:
    result = AssessResult(
        allow=True,
        resolved_operator="op_raw",
        operator_verification=None,
        raw={
            "operator_verification": {"level": "enhanced"},
            "account_verification": {"kyc_level": "enhanced"},
        },
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    cap = profile.ucp.capabilities[AGENTSCORE_UCP_CAPABILITY][0]
    assert cap.extras["claims"]["kyc_level"] == "enhanced"


# Per-element to_dict reserved-key collision guard. Vendor extras can't silently
# overwrite a canonical field on the new binding dataclasses.


def test_ucp_service_binding_extras_collision_rejected() -> None:
    svc = UCPServiceBinding(
        version="2026-04-08",
        spec="https://ucp.dev/spec",
        transport="rest",
        extras={"transport": "different"},
    )
    with pytest.raises(ValueError, match=r"UCPServiceBinding\.extras key 'transport' collides"):
        svc.to_dict()


def test_ucp_service_binding_extras_non_reserved_pass_through() -> None:
    svc = UCPServiceBinding(
        version="2026-04-08",
        spec="https://ucp.dev/spec",
        transport="rest",
        endpoint="https://x.example",
        extras={"region": "us-west-1"},
    )
    out = svc.to_dict()
    assert out["region"] == "us-west-1"
    assert out["endpoint"] == "https://x.example"


def test_ucp_capability_binding_extras_collision_rejected() -> None:
    cap = UCPCapabilityBinding(
        version="1",
        spec="https://x/spec",
        schema="https://x/schema",
        extras={"schema": "https://attacker"},
    )
    with pytest.raises(ValueError, match=r"UCPCapabilityBinding\.extras key 'schema' collides"):
        cap.to_dict()


def test_ucp_capability_binding_claims_extra_passes_through() -> None:
    cap = UCPCapabilityBinding(
        version="1",
        spec="https://x/spec",
        schema="https://x/schema",
        extras={"claims": {"k": "v"}},
    )
    out = cap.to_dict()
    assert out["claims"] == {"k": "v"}


def test_ucp_payment_handler_binding_omits_default_empty_config() -> None:
    h = UCPPaymentHandlerBinding(
        id="tempo",
        version="1",
        spec="https://x",
        schema="https://x",
    )
    out = h.to_dict()
    assert "config" not in out
    assert out["id"] == "tempo"


def test_ucp_payment_handler_binding_omits_explicit_empty_config() -> None:
    h = UCPPaymentHandlerBinding(
        id="tempo",
        version="1",
        spec="https://x",
        schema="https://x",
        config={},
    )
    assert "config" not in h.to_dict()


def test_ucp_payment_handler_binding_preserves_populated_config() -> None:
    h = UCPPaymentHandlerBinding(
        id="tempo",
        version="1",
        spec="https://x",
        schema="https://x",
        config={"recipient": "0xabc"},
    )
    out = h.to_dict()
    assert out["config"] == {"recipient": "0xabc"}


def test_ucp_signing_key_extras_collision_with_kid_rejected() -> None:
    sk = UCPSigningKey(kid="me", kty="EC", extras={"kid": "attacker"})
    with pytest.raises(ValueError, match=r"UCPSigningKey\.extras key 'kid' collides"):
        sk.to_dict()


def test_ucp_signing_key_extras_non_reserved_pass_through() -> None:
    sk = UCPSigningKey(kid="me", kty="EC", alg="ES256", crv="P-256", extras={"x": "abc", "y": "def"})
    out = sk.to_dict()
    assert out == {"kid": "me", "kty": "EC", "alg": "ES256", "crv": "P-256", "x": "abc", "y": "def"}


def test_typed_empty_account_verification_wins_over_raw() -> None:
    result = AssessResult(
        allow=True,
        resolved_operator="op_xyz",
        account_verification={},
        raw={"account_verification": {"kyc_level": "verified"}},
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    cap = profile.ucp.capabilities[AGENTSCORE_UCP_CAPABILITY][0]
    assert cap.extras["claims"]["kyc_level"] == "none"


def test_typed_empty_operator_verification_wins_over_raw() -> None:
    result = AssessResult(
        allow=True,
        resolved_operator="op_xyz",
        operator_verification=cast("OperatorVerification", {}),
        raw={"operator_verification": {"level": "enhanced", "verified_at": "2026-01-01T00:00:00Z"}},
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    cap = profile.ucp.capabilities[AGENTSCORE_UCP_CAPABILITY][0]
    assert cap.extras["claims"]["verified_at"] is None


def test_ucp_profile_body_can_be_constructed_directly() -> None:
    """UCPProfileBody is exported so callers can pre-build the body if they want."""
    body = UCPProfileBody(version="2026-04-08")
    assert body.to_dict()["version"] == "2026-04-08"
    assert body.to_dict()["services"] == {}
