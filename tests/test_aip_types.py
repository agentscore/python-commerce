"""AIT claim contract + structural validation. Ports node-commerce ``tests/aip_types.test.ts``.

Node's ``AitPayload`` is a TS interface; here it is a ``TypedDict`` (a plain ``dict`` at runtime),
so payloads are built/mutated as dicts and ``validate_ait_payload`` returns an
``AitValidationResult`` dataclass (``.ok`` / ``.reason`` / ``.payload``) mirroring node's
discriminated union.
"""

from __future__ import annotations

import pytest

from agentscore_commerce.aip import is_ait_shape, validate_ait_payload

VALID_PAYLOAD: dict = {
    "aip_version": "0.1",
    "iss": "https://issuer.example",
    "sub": "user_abc123",
    "iat": 1715400000,
    "exp": 1715400300,
    "cnf": {"jwk": {"kty": "OKP", "crv": "Ed25519", "x": "abc"}},
    "agent": {"provider": "anthropic", "instance": "session-xyz"},
    "trust_level": "human_present",
}


def _without(field: str) -> dict:
    clone = dict(VALID_PAYLOAD)
    clone.pop(field, None)
    return clone


class TestIsAitShape:
    def test_true_when_cnf_and_agent_are_present_objects(self) -> None:
        assert is_ait_shape(VALID_PAYLOAD) is True

    def test_false_without_cnf(self) -> None:
        assert is_ait_shape(_without("cnf")) is False

    def test_false_without_agent(self) -> None:
        assert is_ait_shape(_without("agent")) is False

    def test_false_for_non_objects(self) -> None:
        assert is_ait_shape(None) is False
        assert is_ait_shape("x") is False
        assert is_ait_shape([]) is False


class TestValidateRequiredClaims:
    def test_accepts_a_well_formed_payload(self) -> None:
        r = validate_ait_payload(VALID_PAYLOAD)
        assert r.ok is True

    @pytest.mark.parametrize(
        ("field", "reason"),
        [
            ("aip_version", "missing_aip_version"),
            ("iss", "missing_iss"),
            ("sub", "missing_sub"),
            ("iat", "missing_iat"),
            ("exp", "missing_exp"),
            ("cnf", "missing_cnf"),
            ("agent", "missing_agent_provider"),
        ],
    )
    def test_rejects_a_payload_missing_a_required_claim(self, field: str, reason: str) -> None:
        r = validate_ait_payload(_without(field))
        assert (r.ok, r.reason) == (False, reason)

    def test_rejects_a_non_object(self) -> None:
        assert (validate_ait_payload(None).ok, validate_ait_payload(None).reason) == (False, "not_an_object")
        assert (validate_ait_payload("jwt").ok, validate_ait_payload("jwt").reason) == (False, "not_an_object")

    def test_rejects_a_cnf_without_a_jwk(self) -> None:
        r = validate_ait_payload({**VALID_PAYLOAD, "cnf": {"notjwk": True}})
        assert (r.ok, r.reason) == (False, "missing_cnf")

    def test_rejects_an_agent_without_provider(self) -> None:
        r = validate_ait_payload({**VALID_PAYLOAD, "agent": {"instance": "x"}})
        assert (r.ok, r.reason) == (False, "missing_agent_provider")

    def test_rejects_a_non_numeric_iat_exp(self) -> None:
        r_iat = validate_ait_payload({**VALID_PAYLOAD, "iat": "1715400000"})
        assert (r_iat.ok, r_iat.reason) == (False, "missing_iat")
        r_exp = validate_ait_payload({**VALID_PAYLOAD, "exp": None})
        assert (r_exp.ok, r_exp.reason) == (False, "missing_exp")

    def test_rejects_a_boolean_iat_python_bool_is_a_subclass_of_int(self) -> None:
        # Extra python-specific guard (no node analog): bool subclasses int, so a JSON `true`
        # decoded into iat must still fail the numeric check, matching node's `typeof === 'number'`.
        r = validate_ait_payload({**VALID_PAYLOAD, "iat": True})
        assert (r.ok, r.reason) == (False, "missing_iat")


class TestHumanConfirmedRequiresAmr:
    def test_rejects_human_confirmed_with_no_auth_at_all(self) -> None:
        r = validate_ait_payload({**VALID_PAYLOAD, "trust_level": "human_confirmed"})
        assert (r.ok, r.reason) == (False, "human_confirmed_without_amr")

    def test_rejects_human_confirmed_with_an_empty_amr_array(self) -> None:
        r = validate_ait_payload({**VALID_PAYLOAD, "trust_level": "human_confirmed", "auth": {"amr": [], "time": 1}})
        assert (r.ok, r.reason) == (False, "human_confirmed_without_amr")

    def test_accepts_human_confirmed_with_at_least_one_amr_value(self) -> None:
        r = validate_ait_payload(
            {**VALID_PAYLOAD, "trust_level": "human_confirmed", "auth": {"amr": ["face"], "time": 1715399900}}
        )
        assert r.ok is True

    def test_does_not_require_amr_for_autonomous_or_human_present(self) -> None:
        assert validate_ait_payload({**VALID_PAYLOAD, "trust_level": "autonomous"}).ok is True
        assert validate_ait_payload({**VALID_PAYLOAD, "trust_level": "human_present"}).ok is True


class TestExtensionClaimsPassThrough:
    def test_preserves_identity_and_payment_extension_claims_on_success(self) -> None:
        with_extensions = {
            **VALID_PAYLOAD,
            "identity": {
                "email": "b@example.com",
                "email_verified": True,
                "age_over_21": True,
                "jurisdiction": "US-CA",
                "sanctions_clear": True,
                "sanctions_providers": ["ofac_sdn", "opensanctions"],
                "linked_wallets": [{"address": "0xabc", "network": "evm"}],
                "merchants_paid": 4,
            },
            "payment": {"signer": {"address": "0xabc", "network": "evm", "match": "linked_operator"}},
        }
        r = validate_ait_payload(with_extensions)
        assert r.ok is True
        assert r.payload is not None
        assert r.payload["identity"]["jurisdiction"] == "US-CA"
        assert r.payload["payment"]["signer"]["match"] == "linked_operator"
