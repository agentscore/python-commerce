"""Tests for UCP profile signing helpers (cross-language parity with node-commerce)."""

from __future__ import annotations

import pytest

from agentscore_commerce.identity.ucp import UCPSigningKey
from agentscore_commerce.identity.ucp_jwks import (
    UCPVerificationError,
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

        with pytest.raises(UCPVerificationError) as exc_info:
            verify_ucp_profile(signed, build_jwks_response([other.public_jwk]))
        assert exc_info.value.code == "kid_not_found"

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


class TestSecurity:
    """Coverage for alg-confusion + kid + typ + dup-kid + tampering attacks."""

    def _hand_sign_compact(self, header: dict, payload_bytes: bytes, key: object, registry: object) -> str:
        from joserfc import jws
        from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

        # Cast for ty
        reg = registry if isinstance(registry, JWSRegistry) else JWSRegistry(algorithms=["EdDSA", "ES256", "HS256"])
        return jws.serialize_compact(header, payload_bytes, key, registry=reg)

    def test_rejects_kid_less_jws(self) -> None:
        """A JWS with no kid header is rejected even if the JWKS has a key that would verify."""
        from joserfc import jws
        from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

        signer = generate_ucp_signing_key(kid="real-kid")
        profile = _base_profile([signer.public_jwk])
        # Hand-craft a JWS with NO kid in the header.
        canonical = (
            __import__("json").dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        registry = JWSRegistry(algorithms=["EdDSA", "ES256"])
        kid_less_sig = jws.serialize_compact(
            {"alg": "EdDSA", "typ": "ucp-profile+jws"},
            canonical,
            signer.private_key,
            registry=registry,
        )
        signed = {**profile, "signature": kid_less_sig}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([signer.public_jwk]))
        assert exc.value.code == "missing_kid"

    def test_rejects_wrong_typ(self) -> None:
        from joserfc import jws
        from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

        signer = generate_ucp_signing_key(kid="k")
        profile = _base_profile([signer.public_jwk])
        canonical = (
            __import__("json").dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        registry = JWSRegistry(algorithms=["EdDSA"])
        wrong_typ_sig = jws.serialize_compact(
            {"alg": "EdDSA", "kid": "k", "typ": "JWT"},
            canonical,
            signer.private_key,
            registry=registry,
        )
        signed = {**profile, "signature": wrong_typ_sig}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([signer.public_jwk]))
        assert exc.value.code == "wrong_typ"

    def test_rejects_unsupported_alg(self) -> None:
        from joserfc import jws
        from joserfc.jwk import OctKey  # type: ignore[import-not-found]
        from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

        # Build a hostile oct key + HS256 sig over the canonical body of a real profile.
        signer = generate_ucp_signing_key(kid="real")
        profile = _base_profile([signer.public_jwk])
        canonical = (
            __import__("json").dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        oct_key = OctKey.generate_key(parameters={"kid": "real", "alg": "HS256", "use": "sig"})
        registry = JWSRegistry(algorithms=["HS256"])
        evil_sig = jws.serialize_compact(
            {"alg": "HS256", "kid": "real", "typ": "ucp-profile+jws"},
            canonical,
            oct_key,
            registry=registry,
        )
        signed = {**profile, "signature": evil_sig}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([signer.public_jwk]))
        assert exc.value.code == "unsupported_alg"

    def test_rejects_duplicate_kid_in_jwks(self) -> None:
        a = generate_ucp_signing_key(kid="dup")
        b = generate_ucp_signing_key(kid="dup")
        profile = _base_profile([a.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=a.private_key, kid="dup")
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([a.public_jwk, b.public_jwk]))
        assert exc.value.code == "duplicate_kid"

    def test_emits_typed_error_for_body_mismatch(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = _base_profile([signer.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        tampered = {**signed, "name": "Different"}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(tampered, build_jwks_response([signer.public_jwk]))
        assert exc.value.code == "body_mismatch"

    def test_emits_typed_error_for_no_signature(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = _base_profile([signer.public_jwk])
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(profile, build_jwks_response([signer.public_jwk]))
        assert exc.value.code == "no_signature"

    def test_rejects_tampered_signature_segment(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = _base_profile([signer.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        # Flip last char of the signature segment.
        h, p, s = signed["signature"].split(".")
        flipped_s = s[:-1] + ("B" if s.endswith("A") else "A")
        tampered = {**signed, "signature": f"{h}.{p}.{flipped_s}"}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(tampered, build_jwks_response([signer.public_jwk]))
        # joserfc may classify as either signature_invalid or malformed_jws depending on the flip.
        assert exc.value.code in ("signature_invalid", "malformed_jws")

    def test_rejects_malformed_jws(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = _base_profile([signer.public_jwk])
        garbage = {**profile, "signature": "not.a.jws"}
        with pytest.raises(UCPVerificationError):
            verify_ucp_profile(garbage, build_jwks_response([signer.public_jwk]))

    def test_eddsa_signing_is_deterministic(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = _base_profile([signer.public_jwk])
        a = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        b = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        assert a["signature"] == b["signature"]

    def test_es256_signing_is_non_deterministic_but_both_verify(self) -> None:
        signer = generate_ucp_signing_key(kid="k", alg="ES256")
        profile = _base_profile([signer.public_jwk])
        a = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k", alg="ES256")
        b = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k", alg="ES256")
        assert a["signature"] != b["signature"]
        assert verify_ucp_profile(a, build_jwks_response([signer.public_jwk])) is True
        assert verify_ucp_profile(b, build_jwks_response([signer.public_jwk])) is True


class TestFloatRejection:
    def test_rejects_float_in_profile(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"rate": 0.0125}}
        with pytest.raises(ValueError, match="rejects float"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_accepts_int_and_string(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"count": 7, "label": "wine"}}
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        assert verify_ucp_profile(signed, build_jwks_response([signer.public_jwk])) is True


class TestUCPSigningKeyFromJWK:
    def test_round_trip_eddsa(self) -> None:
        gen = generate_ucp_signing_key(kid="merchant-2026-05")
        sk = UCPSigningKey.from_jwk(gen.public_jwk)
        assert sk.kid == "merchant-2026-05"
        assert sk.kty == "OKP"
        assert sk.alg == "EdDSA"
        assert sk.use == "sig"
        assert sk.crv == "Ed25519"
        assert "x" in sk.extras
        # Re-emit and confirm the JWK round-trips.
        as_dict = sk.to_dict()
        assert as_dict["kid"] == "merchant-2026-05"
        assert as_dict["x"] == gen.public_jwk["x"]

    def test_round_trip_es256(self) -> None:
        gen = generate_ucp_signing_key(kid="es", alg="ES256")
        sk = UCPSigningKey.from_jwk(gen.public_jwk)
        assert sk.kty == "EC"
        assert sk.crv == "P-256"
        assert "x" in sk.extras and "y" in sk.extras
