"""Tests for build_ucp_profile (spec-compliant shape)."""

import pytest

from agentscore_commerce.identity import (
    AGENTSCORE_UCP_CAPABILITY,
    AgentScoreGatePolicy,
    UCPCapabilityBinding,
    UCPPaymentHandlerBinding,
    UCPProfileBody,
    UCPServiceBinding,
    UCPSigningKey,
    build_ucp_profile,
)


def _sample_service() -> UCPServiceBinding:
    return UCPServiceBinding(
        version="2026-04-08",
        spec="https://ucp.dev/2026-04-08/specification/overview",
        transport="mcp",
        endpoint="https://agents.example/api/ucp/mcp",
        schema="https://ucp.dev/services/shopping/mcp.openrpc.json",
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


def test_skips_agentscore_capability_when_gate_not_provided():
    profile = build_ucp_profile(**_base_kwargs())
    d = profile.to_dict()
    assert AGENTSCORE_UCP_CAPABILITY not in d["ucp"]["capabilities"]


def test_appends_agentscore_capability_when_gate_provided():
    profile = build_ucp_profile(
        **_base_kwargs(),
        agentscore_gate=AgentScoreGatePolicy(
            require_kyc=True,
            require_sanctions_clear=True,
            min_age=21,
            allowed_jurisdictions=["US"],
        ),
    )
    d = profile.to_dict()
    cap = _agentscore_cap(d)
    # Date-format version (UCP convention; matches every other binding's version field).
    assert cap["version"] == "2026-04-08"
    assert "sh-agentscore-identity-v1.json" in cap["schema"]
    # Multi-parent extends — matches Shopify's dev.shopify.catalog.storefront pattern
    # and UCP-canonical dev.ucp.shopping.discount (extends [checkout, cart]).
    assert cap["extends"] == ["dev.ucp.shopping.checkout", "dev.ucp.shopping.cart"]
    # Config is the merchant's policy declaration, NOT per-operator data. Public
    # /.well-known/ucp profiles must never carry per-operator KYC claims.
    assert cap["config"] == {
        "require_kyc": True,
        "require_sanctions_clear": True,
        "min_age": 21,
        "allowed_jurisdictions": ["US"],
    }


def test_capability_present_with_omitted_config_when_caller_passes_empty_policy():
    """When the caller passes AgentScoreGatePolicy() with no fields set, the capability
    binding is still injected (signals that the merchant is AgentScore-gated), but the
    `config` field is omitted from serialization for cross-lang parity (the underlying
    UCPCapabilityBinding.to_dict skips empty config consistently with how
    UCPPaymentHandlerBinding skips empty config)."""
    profile = build_ucp_profile(
        **_base_kwargs(),
        agentscore_gate=AgentScoreGatePolicy(),
    )
    d = profile.to_dict()
    cap = _agentscore_cap(d)
    # Capability IS present
    assert cap["version"] == "2026-04-08"
    # Config IS omitted (no policy fields set)
    assert "config" not in cap


def test_emits_only_the_policy_fields_caller_set():
    profile = build_ucp_profile(
        **_base_kwargs(),
        agentscore_gate=AgentScoreGatePolicy(require_kyc=True),
    )
    cap_config = _agentscore_cap(profile.to_dict())["config"]
    assert cap_config == {"require_kyc": True}
    assert "min_age" not in cap_config
    assert "allowed_jurisdictions" not in cap_config


def test_blocked_jurisdictions_serializes_as_inverse_of_allowed():
    profile = build_ucp_profile(
        **_base_kwargs(),
        agentscore_gate=AgentScoreGatePolicy(blocked_jurisdictions=["KP", "IR", "CU"]),
    )
    cap_config = _agentscore_cap(profile.to_dict())["config"]
    assert cap_config == {"blocked_jurisdictions": ["KP", "IR", "CU"]}


def test_preserves_caller_capabilities_and_appends_agentscore():
    checkout_binding = UCPCapabilityBinding(
        version="2026-04-08",
        spec="https://ucp.dev/2026-04-08/specification/checkout",
        schema="https://ucp.dev/2026-04-08/schemas/shopping/checkout.json",
    )
    profile = build_ucp_profile(
        **_base_kwargs(),
        capabilities={"dev.ucp.shopping.checkout": [checkout_binding]},
        agentscore_gate=AgentScoreGatePolicy(require_kyc=True),
    )
    d = profile.to_dict()
    assert d["ucp"]["capabilities"]["dev.ucp.shopping.checkout"][0]["version"] == "2026-04-08"
    assert _agentscore_cap(d)["version"] == "2026-04-08"


def test_passes_through_name_payment_handlers_extras():
    tempo_handler = UCPPaymentHandlerBinding(
        id="tempo",
        version="2026-04-08",
        spec="https://agentscore.com/specification/payment-handlers/tempo",
        schema="https://agentscore.com/schemas/payment-handlers/tempo.json",
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
        spec="https://agentscore.com/specification/payment-handlers/tempo",
        schema="https://agentscore.com/schemas/payment-handlers/tempo.json",
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
        agentscore_gate=AgentScoreGatePolicy(),
        agentscore_schema_url="https://custom.example/schema.json",
    )
    cap = profile.ucp.capabilities[AGENTSCORE_UCP_CAPABILITY][0]
    assert cap.schema == "https://custom.example/schema.json"


def test_respects_agentscore_spec_url_override():
    profile = build_ucp_profile(
        **_base_kwargs(),
        agentscore_gate=AgentScoreGatePolicy(),
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


# Per-element to_dict reserved-key collision guard. Vendor extras can't silently
# overwrite a canonical field on the new binding dataclasses.


def test_ucp_service_binding_extras_collision_rejected() -> None:
    svc = UCPServiceBinding(
        version="2026-04-08",
        spec="https://ucp.dev/spec",
        transport="rest",
        endpoint="https://x.example",
        extras={"transport": "different"},
    )
    with pytest.raises(ValueError, match=r"UCPServiceBinding\.extras key 'transport' collides"):
        svc.to_dict()


@pytest.mark.parametrize("transport", ["rest", "mcp", "a2a"])
def test_ucp_service_binding_rejects_missing_endpoint_for_required_transports(transport: str) -> None:
    """Per UCP spec service.json: rest/mcp/a2a transports MUST carry an endpoint URL."""
    svc = UCPServiceBinding(
        version="2026-04-08",
        spec="https://ucp.dev/spec",
        transport=transport,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="requires `endpoint`"):
        svc.to_dict()


def test_ucp_service_binding_embedded_does_not_require_endpoint() -> None:
    """Per spec service.json: embedded transport MAY omit endpoint."""
    svc = UCPServiceBinding(
        version="2026-04-08",
        spec="https://ucp.dev/spec",
        transport="embedded",
        schema="https://ucp.dev/schemas/embedded.json",
    )
    out = svc.to_dict()
    assert "endpoint" not in out


def test_ucp_payment_handler_drops_empty_available_instruments() -> None:
    """Per spec payment_handler.json: available_instruments has minItems:1.
    Drop the field when empty so callers passing `[]` don't ship an invalid profile."""
    h = UCPPaymentHandlerBinding(
        id="tempo",
        version="2026-04-08",
        spec="https://x",
        schema="https://x",
        available_instruments=[],
    )
    out = h.to_dict()
    assert "available_instruments" not in out


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
        version="2026-04-08",
        spec="https://x/spec",
        schema="https://x/schema",
        extras={"schema": "https://attacker"},
    )
    with pytest.raises(ValueError, match=r"UCPCapabilityBinding\.extras key 'schema' collides"):
        cap.to_dict()


def test_ucp_capability_binding_extras_pass_through() -> None:
    cap = UCPCapabilityBinding(
        version="2026-04-08",
        spec="https://x/spec",
        schema="https://x/schema",
        extras={"vendor_field": {"k": "v"}},
    )
    out = cap.to_dict()
    assert out["vendor_field"] == {"k": "v"}


def test_ucp_payment_handler_binding_omits_default_empty_config() -> None:
    h = UCPPaymentHandlerBinding(
        id="tempo",
        version="2026-04-08",
        spec="https://x",
        schema="https://x",
    )
    out = h.to_dict()
    assert "config" not in out
    assert out["id"] == "tempo"


def test_ucp_payment_handler_binding_omits_explicit_empty_config() -> None:
    h = UCPPaymentHandlerBinding(
        id="tempo",
        version="2026-04-08",
        spec="https://x",
        schema="https://x",
        config={},
    )
    assert "config" not in h.to_dict()


def test_ucp_payment_handler_binding_preserves_populated_config() -> None:
    h = UCPPaymentHandlerBinding(
        id="tempo",
        version="2026-04-08",
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


def test_ucp_profile_body_can_be_constructed_directly() -> None:
    """UCPProfileBody is exported so callers can pre-build the body if they want."""
    body = UCPProfileBody(version="2026-04-08")
    assert body.to_dict()["version"] == "2026-04-08"
    assert body.to_dict()["services"] == {}
