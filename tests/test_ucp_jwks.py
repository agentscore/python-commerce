"""Tests for UCP profile signing helpers (cross-language parity with node-commerce)."""

from __future__ import annotations

import pytest

from agentscore_commerce.identity.ucp_jwks import (
    build_jwks_response,
    generate_ucp_signing_key,
    sign_ucp_profile,
    verify_ucp_profile,
)


def _base_profile(signing_keys: list[dict]) -> dict:
    return {
        "version": "2026-04-17",
        "spec": "https://ucp.dev/",
        "name": "Test Merchant",
        "services": [{"type": "rest", "url": "https://agents.example.com"}],
        "capabilities": [],
        "payment_handlers": [{"name": "tempo", "config": {"recipient": "0x1234"}}],
        "signing_keys": signing_keys,
    }


class TestGenerateUCPSigningKey:
    def test_generates_eddsa_keypair_by_default(self) -> None:
        key = generate_ucp_signing_key(kid="test-key-1")
        assert key.private_key is not None
        assert key.public_jwk["kid"] == "test-key-1"
        assert key.public_jwk["alg"] == "EdDSA"
        assert key.public_jwk["use"] == "sig"
        assert key.public_jwk["kty"] == "OKP"
        assert key.public_jwk["crv"] == "Ed25519"
        assert isinstance(key.public_jwk.get("x"), str)
        # private parts must NOT be in the exported public JWK
        assert "d" not in key.public_jwk

    def test_generates_es256_keypair(self) -> None:
        key = generate_ucp_signing_key(kid="es256-key", alg="ES256")
        assert key.public_jwk["alg"] == "ES256"
        assert key.public_jwk["kty"] == "EC"
        assert key.public_jwk["crv"] == "P-256"
        assert isinstance(key.public_jwk.get("x"), str)
        assert isinstance(key.public_jwk.get("y"), str)
        assert "d" not in key.public_jwk

    def test_distinct_kid_and_material(self) -> None:
        a = generate_ucp_signing_key(kid="a")
        b = generate_ucp_signing_key(kid="b")
        assert a.public_jwk["kid"] == "a"
        assert b.public_jwk["kid"] == "b"
        assert a.public_jwk["x"] != b.public_jwk["x"]

    def test_unsupported_alg_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported UCP signing algorithm"):
            generate_ucp_signing_key(kid="bad", alg="RS256")  # type: ignore[arg-type]


class TestSignAndVerifyRoundTrip:
    def test_eddsa_sign_verify_round_trip(self) -> None:
        key = generate_ucp_signing_key(kid="merchant-2026-05")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="merchant-2026-05")

        assert "signature" in signed
        assert isinstance(signed["signature"], str)
        # JWS Compact has 3 segments separated by dots
        assert len(signed["signature"].split(".")) == 3

        ok = verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert ok is True

    def test_es256_sign_verify_round_trip(self) -> None:
        key = generate_ucp_signing_key(kid="es256-key", alg="ES256")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="es256-key", alg="ES256")
        ok = verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert ok is True

    def test_multi_key_jwks_resolves_by_kid(self) -> None:
        old_key = generate_ucp_signing_key(kid="old-key")
        new_key = generate_ucp_signing_key(kid="new-key")
        profile = _base_profile([old_key.public_jwk, new_key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=new_key.private_key, kid="new-key")
        ok = verify_ucp_profile(signed, build_jwks_response([old_key.public_jwk, new_key.public_jwk]))
        assert ok is True

    def test_rejects_tampered_profile_body(self) -> None:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")

        tampered = {**signed, "name": "Different Name"}
        with pytest.raises(ValueError, match="does not match the signed payload"):
            verify_ucp_profile(tampered, build_jwks_response([key.public_jwk]))

    def test_rejects_when_jwks_missing_signing_key(self) -> None:
        signer = generate_ucp_signing_key(kid="signer")
        other = generate_ucp_signing_key(kid="other")
        profile = _base_profile([signer.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="signer")

        from joserfc.errors import InvalidKeyIdError

        with pytest.raises(InvalidKeyIdError):
            verify_ucp_profile(signed, build_jwks_response([other.public_jwk]))

    def test_rejects_profile_without_signature(self) -> None:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        with pytest.raises(ValueError, match="no `signature` field"):
            verify_ucp_profile(profile, build_jwks_response([key.public_jwk]))


class TestCanonicalization:
    def test_key_order_in_json_does_not_affect_verification(self) -> None:
        import json

        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")

        # Round-trip through JSON (loses original key order)
        reordered = json.loads(json.dumps(signed))
        ok = verify_ucp_profile(reordered, build_jwks_response([key.public_jwk]))
        assert ok is True


class TestBuildJWKSResponse:
    def test_wraps_keys_in_keys_array(self) -> None:
        k1 = {"kid": "a", "kty": "OKP", "crv": "Ed25519", "x": "xxx", "use": "sig", "alg": "EdDSA"}
        k2 = {"kid": "b", "kty": "EC", "crv": "P-256", "x": "xxx", "y": "yyy", "use": "sig", "alg": "ES256"}
        jwks = build_jwks_response([k1, k2])
        assert jwks == {"keys": [k1, k2]}

    def test_handles_empty_key_set(self) -> None:
        assert build_jwks_response([]) == {"keys": []}
