"""Tests for build_ucp_profile."""

from typing import cast

import pytest

from agentscore_commerce.identity import (
    AGENTSCORE_UCP_CAPABILITY,
    AssessResult,
    OperatorVerification,
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
    assert cap["name"] == "sh.agentscore.identity"
    assert "sh-agentscore-identity-v1.json" in cap["schema"]
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


def _claims_of(account_verification: dict, operator_verification: dict | None = None) -> dict:
    raw: dict = {"account_verification": account_verification}
    if operator_verification is not None:
        raw["operator_verification"] = operator_verification
    result = AssessResult(
        allow=True,
        resolved_operator="op_abc",
        raw=raw,
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


def test_both_empty_string_verified_at_normalizes_to_none() -> None:
    """Both account_verification + operator_verification with verified_at=''
    must normalize to None for cross-language byte parity with Node SDK.
    Without the trailing ``or None``, Python's chained ``or`` returns the last
    falsy value (``""``); Node's ``a || b || null`` returns ``null``.
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
    cap = next(c for c in d["capabilities"] if c["name"] == AGENTSCORE_UCP_CAPABILITY)
    claims = cap["claims"]
    assert claims["operator_id"] == "op_typed"
    assert claims["kyc_level"] == "enhanced"
    assert claims["verified_at"] == "2026-04-01T00:00:00Z"


def test_typed_account_verification_fallback_when_raw_is_none() -> None:
    # `AssessResult.account_verification` is a typed optional field; a
    # hand-constructed result populates it directly via the constructor and the
    # builder reads it without consulting `raw`.
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
    cap = next(c for c in d["capabilities"] if c["name"] == AGENTSCORE_UCP_CAPABILITY)
    claims = cap["claims"]
    assert claims["kyc_level"] == "verified"
    assert claims["age_bracket"] == "21+"
    assert claims["jurisdiction"] == "US"
    assert claims["sanctions_clear"] is True


def test_typed_takes_precedence_over_raw() -> None:
    # When the typed `operator_verification` / `account_verification` fields
    # disagree with `data.raw`, the typed values win. Mirrors the node sibling
    # which reads `input.data.operator_verification` directly without
    # consulting `raw`. Production callers populate raw and the typed fields
    # stay in sync; pinning typed-precedence keeps a hand-constructed
    # AssessResult from emitting a profile that one language verifies and the
    # other rejects.
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
    cap = next(c for c in profile.capabilities if c.name == AGENTSCORE_UCP_CAPABILITY)
    # Typed `account_verification.kyc_level == 'verified'` wins over the
    # `none` value carried in `data.raw`.
    assert cap.extras["claims"]["kyc_level"] == "verified"


def test_raw_fallback_used_when_typed_missing() -> None:
    # When typed `operator_verification` / `account_verification` are absent,
    # the builder falls back to `data.raw`. This is the production path:
    # `AgentScoreClient` populates both, but legacy or ad-hoc callers may
    # only set raw.
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
    cap = next(c for c in profile.capabilities if c.name == AGENTSCORE_UCP_CAPABILITY)
    # `kyc_level` falls back to raw `account_verification.kyc_level`.
    assert cap.extras["claims"]["kyc_level"] == "enhanced"


# Per-element to_dict reserved-key collision guard. Mirrors the parent
# UCPProfile.to_dict guard so vendor extras can't silently overwrite a canonical
# field on UCPService / UCPCapability / UCPSigningKey via `out.update(extras)`.


def test_ucp_service_extras_collision_with_type_rejected() -> None:
    svc = UCPService(type="rest", extras={"type": "different"})
    with pytest.raises(ValueError, match=r"UCPService\.extras key 'type' collides"):
        svc.to_dict()


def test_ucp_service_extras_collision_with_url_rejected() -> None:
    svc = UCPService(type="rest", url="https://x.example", extras={"url": "https://attacker.example"})
    with pytest.raises(ValueError, match=r"UCPService\.extras key 'url' collides"):
        svc.to_dict()


def test_ucp_service_extras_non_reserved_pass_through() -> None:
    svc = UCPService(type="rest", url="https://x.example", extras={"region": "us-west-1"})
    assert svc.to_dict() == {"type": "rest", "url": "https://x.example", "region": "us-west-1"}


def test_ucp_capability_extras_collision_with_name_rejected() -> None:
    cap = UCPCapability(name="checkout", extras={"name": "different"})
    with pytest.raises(ValueError, match=r"UCPCapability\.extras key 'name' collides"):
        cap.to_dict()


def test_ucp_capability_extras_collision_with_schema_rejected() -> None:
    cap = UCPCapability(name="checkout", schema="https://x/y", extras={"schema": "https://attacker"})
    with pytest.raises(ValueError, match=r"UCPCapability\.extras key 'schema' collides"):
        cap.to_dict()


def test_ucp_capability_extras_non_reserved_pass_through() -> None:
    cap = UCPCapability(name="checkout", extras={"claims": {"k": "v"}})
    assert cap.to_dict() == {"name": "checkout", "claims": {"k": "v"}}


def test_ucp_signing_key_extras_collision_with_kid_rejected() -> None:
    sk = UCPSigningKey(kid="me", kty="EC", extras={"kid": "attacker"})
    with pytest.raises(ValueError, match=r"UCPSigningKey\.extras key 'kid' collides"):
        sk.to_dict()


def test_ucp_signing_key_extras_collision_with_kty_rejected() -> None:
    sk = UCPSigningKey(kid="me", kty="EC", extras={"kty": "RSA"})
    with pytest.raises(ValueError, match=r"UCPSigningKey\.extras key 'kty' collides"):
        sk.to_dict()


def test_ucp_signing_key_extras_non_reserved_pass_through() -> None:
    sk = UCPSigningKey(kid="me", kty="EC", alg="ES256", crv="P-256", extras={"x": "abc", "y": "def"})
    out = sk.to_dict()
    assert out == {"kid": "me", "kty": "EC", "alg": "ES256", "crv": "P-256", "x": "abc", "y": "def"}


# UCPPaymentHandler.to_dict omits `config` when empty. Node's
# `UCPPaymentHandler.config` is optional (`Record<string, unknown>?`), so a Node
# caller writing `{name: 'tempo'}` ships a wire profile WITHOUT the `config` key.
# Python must do the same or the same logical input produces different canonical
# bytes between SDKs. Explicit `config={}` is semantically identical to absent
# and follows the same omit rule.


def test_ucp_payment_handler_to_dict_omits_default_empty_config() -> None:
    assert UCPPaymentHandler(name="tempo").to_dict() == {"name": "tempo"}


def test_ucp_payment_handler_to_dict_omits_explicit_empty_config() -> None:
    assert UCPPaymentHandler(name="tempo", config={}).to_dict() == {"name": "tempo"}


def test_ucp_payment_handler_to_dict_preserves_populated_config() -> None:
    assert UCPPaymentHandler(name="tempo", config={"recipient": "0xabc"}).to_dict() == {
        "name": "tempo",
        "config": {"recipient": "0xabc"},
    }


# Typed-vs-raw read order: `data.account_verification == {}` means "API
# explicitly returned an empty block" and must win over `data.raw`. Only when
# the typed field is `None` does the builder fall back to raw. Mirrors the Node
# sibling, which reads the typed field directly without consulting raw.


def test_typed_empty_account_verification_wins_over_raw() -> None:
    result = AssessResult(
        allow=True,
        resolved_operator="op_xyz",
        account_verification={},
        raw={"account_verification": {"kyc_level": "verified"}},
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    cap = next(c for c in profile.capabilities if c.name == AGENTSCORE_UCP_CAPABILITY)
    # Empty typed dict suppresses the raw fallback; kyc_level falls through to
    # the schema default "none" instead of bleeding the raw "verified" value.
    assert cap.extras["claims"]["kyc_level"] == "none"


def test_typed_empty_operator_verification_wins_over_raw() -> None:
    result = AssessResult(
        allow=True,
        resolved_operator="op_xyz",
        # Empty dict is a valid typed value (means "operator block returned empty").
        operator_verification=cast("OperatorVerification", {}),
        raw={"operator_verification": {"level": "enhanced", "verified_at": "2026-01-01T00:00:00Z"}},
    )
    profile = build_ucp_profile(**_base_kwargs(), data=result)
    cap = next(c for c in profile.capabilities if c.name == AGENTSCORE_UCP_CAPABILITY)
    # Empty typed dict suppresses raw fallback; verified_at falls through to None.
    assert cap.extras["claims"]["verified_at"] is None
