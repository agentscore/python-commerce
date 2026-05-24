"""Tests for UCP profile signing helpers."""

from __future__ import annotations

import pytest

from agentscore_commerce.identity.ucp import UCPSigningKey
from agentscore_commerce.identity.ucp_jwks import (
    GeneratedUCPKey,
    UCPVerificationError,
    build_jwks_response,
    generate_ucp_signing_key,
    sign_ucp_profile,
    verify_ucp_profile,
)


def _base_profile(signing_keys: list[dict]) -> dict:
    return {
        "version": "2026-04-08",
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
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")

        # Hand-construct the same profile with keys in REVERSE insertion order
        # so canonicalization actually has work to do. ``json.loads(json.dumps(...))``
        # preserves the source order on Python 3.7+, which is a vacuous round-trip.
        reordered = {k: signed[k] for k in sorted(signed.keys(), reverse=True)}
        assert next(iter(reordered)) != next(iter(sorted(signed)))  # sanity
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
            {"alg": "EdDSA", "typ": "agentscore-profile+jws"},
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
            {"alg": "HS256", "kid": "real", "typ": "agentscore-profile+jws"},
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


class TestUnsafeNumberRejection:
    def test_rejects_float_in_profile(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"rate": 0.0125}}
        with pytest.raises(ValueError, match="rejects float"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_nan(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"value": float("nan")}}
        with pytest.raises(ValueError, match="rejects float"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_positive_infinity(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"value": float("inf")}}
        with pytest.raises(ValueError, match="rejects float"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_negative_infinity(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"value": float("-inf")}}
        with pytest.raises(ValueError, match="rejects float"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_accepts_int_and_string(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"count": 7, "label": "wine"}}
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        assert verify_ucp_profile(signed, build_jwks_response([signer.public_jwk])) is True

    def test_rejects_set_values_outright(self) -> None:
        # `set` is not representable in JSON; the canonicalizer rejects it with a
        # typed message before any element-level checks run. Mirrors node's
        # `stableStringify: Set values are not allowed`.
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"vals": {0.5}}}
        with pytest.raises(ValueError, match="set values are not allowed"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_frozenset_values_outright(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"vals": frozenset({0.25})}}
        with pytest.raises(ValueError, match="frozenset values are not allowed"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_empty_set_with_typed_message(self) -> None:
        # Empty set + set-of-valid-strings would fall through `_reject_unsafe_numbers`
        # cleanly and surface a raw `TypeError` from `json.dumps` later. The typed
        # reject ensures callers get a guiding ValueError instead.
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"vals": set()}}
        with pytest.raises(ValueError, match="set values are not allowed"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_set_of_valid_strings_with_typed_message(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"vals": {"valid", "strings"}}}
        with pytest.raises(ValueError, match="set values are not allowed"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_bytes_values_outright(self) -> None:
        # `bytes` is not representable in JSON; the canonicalizer rejects it with a
        # typed message before `json.dumps` can raise its raw
        # `TypeError: Object of type bytes is not JSON serializable`. Mirrors
        # node's `stableStringify: typed arrays are not allowed`.
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"blob": b"hello"}}
        with pytest.raises(ValueError, match="bytes values are not allowed"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_bytearray_values_outright(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"blob": bytearray(b"hello")}}
        with pytest.raises(ValueError, match="bytearray values are not allowed"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_empty_bytes_with_typed_message(self) -> None:
        # Empty bytes would fall through `_reject_unsafe_numbers` cleanly and
        # surface a raw `TypeError` from `json.dumps` later. The typed reject
        # ensures callers get a guiding ValueError instead.
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"blob": b""}}
        with pytest.raises(ValueError, match="bytes values are not allowed"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_accepts_max_safe_int_boundary(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"big": 2**53 - 1}}
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        assert verify_ucp_profile(signed, build_jwks_response([signer.public_jwk])) is True

    def test_accepts_min_safe_int_boundary(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"big": -(2**53 - 1)}}
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        assert verify_ucp_profile(signed, build_jwks_response([signer.public_jwk])) is True

    def test_rejects_int_above_max_safe_boundary(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"big": 2**53}}
        with pytest.raises(ValueError, match=r"2\^53"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_int_well_above_max_safe(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"big": 2**60}}
        with pytest.raises(ValueError, match=r"2\^53"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_int_below_min_safe(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"neg": -(2**53)}}
        with pytest.raises(ValueError, match=r"2\^53"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_oversized_int_in_nested_list(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"a": [{"b": 2**60}]}}
        with pytest.raises(ValueError, match=r"2\^53"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_accepts_bool_values(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"flag": True, "other": False}}
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
        as_dict = sk.to_dict()
        assert as_dict["kid"] == "merchant-2026-05"
        assert as_dict["x"] == gen.public_jwk["x"]

    def test_round_trip_es256(self) -> None:
        gen = generate_ucp_signing_key(kid="es", alg="ES256")
        sk = UCPSigningKey.from_jwk(gen.public_jwk)
        assert sk.kty == "EC"
        assert sk.crv == "P-256"
        assert "x" in sk.extras and "y" in sk.extras

    def test_rejects_oct_symmetric_key(self) -> None:
        with pytest.raises(ValueError, match=r"oct.*rejected|not a supported asymmetric key type"):
            UCPSigningKey.from_jwk({"kid": "k", "kty": "oct", "k": "AAAA"})

    def test_rejects_jwk_missing_kid(self) -> None:
        with pytest.raises(ValueError, match="missing required field `kid`"):
            UCPSigningKey.from_jwk({"kty": "OKP"})

    def test_rejects_jwk_missing_kty(self) -> None:
        with pytest.raises(ValueError, match="missing required field `kty`"):
            UCPSigningKey.from_jwk({"kid": "k"})

    def test_rejects_non_dict_input(self) -> None:
        with pytest.raises(ValueError, match="expected a dict"):
            UCPSigningKey.from_jwk("not a jwk")  # type: ignore[arg-type]


class TestAdditionalHardening:
    def test_sign_ucp_profile_rejects_kid_not_in_signing_keys(self) -> None:
        key = generate_ucp_signing_key(kid="real")
        profile = _base_profile([key.public_jwk])
        with pytest.raises(ValueError, match=r"not present in profile.signing_keys"):
            sign_ucp_profile(profile, signing_key=key.private_key, kid="wrong")

    def test_verify_rejects_malformed_jwks_missing_keys(self) -> None:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, {})
        assert exc.value.code == "malformed_jwks"

    def test_verify_rejects_non_dict_jwks(self) -> None:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, [key.public_jwk])  # type: ignore[arg-type]
        assert exc.value.code == "malformed_jwks"

    def test_verify_rejects_non_dict_profile(self) -> None:
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile("not a profile", {"keys": []})  # type: ignore[arg-type]
        assert exc.value.code == "no_signature"

    def test_verify_rejects_unusable_key_use_enc(self) -> None:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")
        enc_jwk = {**key.public_jwk, "use": "enc"}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([enc_jwk]))
        assert exc.value.code == "unusable_key"

    def test_verify_rejects_unusable_key_alg_mismatch(self) -> None:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")
        # JWKS advertises the same kid but with a wrong `alg` (RFC 7517 §4.4 violation):
        # JWS header carries alg=EdDSA, JWK declares alg=ES256.
        wrong_alg_jwk = {**key.public_jwk, "alg": "ES256"}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([wrong_alg_jwk]))
        assert exc.value.code == "unusable_key"
        assert "ES256" in str(exc.value)
        assert "EdDSA" in str(exc.value)

    @pytest.mark.parametrize("bad_sig", [42, None, [], {}])
    def test_verify_rejects_non_string_signature(self, bad_sig: object) -> None:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        tampered = {**profile, "signature": bad_sig}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(tampered, build_jwks_response([key.public_jwk]))
        assert exc.value.code == "no_signature"

    @pytest.mark.parametrize("bad_entry", [None, "string"])
    def test_verify_rejects_non_dict_jwks_entry(self, bad_entry: object) -> None:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, {"keys": [bad_entry]})
        assert exc.value.code == "kid_not_found"

    def test_verify_rejects_protected_header_decoding_to_json_array(self) -> None:
        import base64

        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        header_array_b64 = (
            base64.urlsafe_b64encode(__import__("json").dumps(["EdDSA", "kid"]).encode()).rstrip(b"=").decode()
        )
        bogus_jws = f"{header_array_b64}.payload.sig"
        signed = {**profile, "signature": bogus_jws}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert exc.value.code == "malformed_jws"

    def test_verify_wraps_unrecognized_critical_header(self) -> None:
        import base64

        from joserfc import jws
        from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        # Hand-craft a JWS with crit (use the raw underlying key to bypass joserfc's sign-time check).
        canonical = (
            __import__("json").dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        header = {"alg": "EdDSA", "kid": "k", "typ": "agentscore-profile+jws", "crit": ["fakething"], "fakething": "x"}
        header_b64 = (
            base64.urlsafe_b64encode(__import__("json").dumps(header, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )
        payload_b64 = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode()
        signing_input = f"{header_b64}.{payload_b64}".encode()
        sig = key.private_key.private_key.sign(signing_input)
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        jws_compact = f"{header_b64}.{payload_b64}.{sig_b64}"

        signed = {**profile, "signature": jws_compact}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert exc.value.code == "unrecognized_critical_header"
        # Silence unused-import warnings — registry is referenced for the joserfc namespace.
        _ = jws, JWSRegistry

    def test_verify_crit_with_missing_kid_emits_unrecognized_critical_header(self) -> None:
        """JWS with both crit violation AND missing kid emits unrecognized_critical_header,
        matching node-commerce's typ -> alg -> kid -> crit precedence."""
        import base64

        key = generate_ucp_signing_key(kid="real")
        profile = _base_profile([key.public_jwk])
        # Hand-craft a JWS with header carrying both crit AND a kid that the JWKS does NOT contain.
        canonical = (
            __import__("json").dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        header = {
            "alg": "EdDSA",
            "kid": "nonexistent",
            "typ": "agentscore-profile+jws",
            "crit": ["fakething"],
            "fakething": "x",
        }
        header_b64 = (
            base64.urlsafe_b64encode(__import__("json").dumps(header, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )
        payload_b64 = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode()
        signing_input = f"{header_b64}.{payload_b64}".encode()
        sig = key.private_key.private_key.sign(signing_input)
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        jws_compact = f"{header_b64}.{payload_b64}.{sig_b64}"

        signed = {**profile, "signature": jws_compact}
        # JWKS contains 'real' but the JWS advertises kid='nonexistent'. Without the
        # crit-before-kid-lookup check the verifier would emit kid_not_found, diverging
        # from node-commerce.
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert exc.value.code == "unrecognized_critical_header"

    def _hand_craft_jws_with_crit(self, key: GeneratedUCPKey, profile: dict, crit_value: object) -> str:
        """Build a JWS whose protected header carries an arbitrary `crit` value
        (including JSON null / non-list shapes) by signing the raw bytes directly.
        joserfc's high-level sign API would reject these on the way in."""
        import base64

        canonical = (
            __import__("json").dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        header = {"alg": "EdDSA", "kid": "real", "typ": "agentscore-profile+jws", "crit": crit_value}
        header_b64 = (
            base64.urlsafe_b64encode(__import__("json").dumps(header, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )
        payload_b64 = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode()
        signing_input = f"{header_b64}.{payload_b64}".encode()
        sig = key.private_key.private_key.sign(signing_input)
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        return f"{header_b64}.{payload_b64}.{sig_b64}"

    def test_verify_crit_null_emits_malformed_jws(self) -> None:
        """JWS protected header with crit=null is malformed (RFC 7515 §4.1.11
        requires a non-empty array). Regression guard: the previous `is not None`
        gate let JSON null fall through to joserfc's iterate-crit, which crashed
        with a raw TypeError instead of the typed UCPVerificationError. Node
        sibling already maps crit=null to malformed_jws."""
        key = generate_ucp_signing_key(kid="real")
        profile = _base_profile([key.public_jwk])
        jws_compact = self._hand_craft_jws_with_crit(key, profile, None)
        signed = {**profile, "signature": jws_compact}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert exc.value.code == "malformed_jws"

    def test_verify_crit_empty_array_emits_malformed_jws(self) -> None:
        """RFC 7515 §4.1.11 requires `crit` be a non-empty array."""
        key = generate_ucp_signing_key(kid="real")
        profile = _base_profile([key.public_jwk])
        jws_compact = self._hand_craft_jws_with_crit(key, profile, [])
        signed = {**profile, "signature": jws_compact}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert exc.value.code == "malformed_jws"

    def test_verify_crit_string_emits_malformed_jws(self) -> None:
        """`crit` must be an array per RFC 7515 §4.1.11; a string is malformed."""
        key = generate_ucp_signing_key(kid="real")
        profile = _base_profile([key.public_jwk])
        jws_compact = self._hand_craft_jws_with_crit(key, profile, "fakething")
        signed = {**profile, "signature": jws_compact}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert exc.value.code == "malformed_jws"

    @pytest.mark.parametrize(
        "bad_crit",
        [
            [42],
            [None],
            [{}],
            [42, "valid"],
            ["valid", 42],
        ],
    )
    def test_verify_crit_with_non_string_element_emits_malformed_jws(self, bad_crit: object) -> None:
        """RFC 7515 §4.1.11: crit array entries MUST be strings. Non-string elements
        (including mixed arrays) are malformed. Cross-language parity with node-commerce,
        which rejects [42] etc. with malformed_jws."""
        key = generate_ucp_signing_key(kid="real")
        profile = _base_profile([key.public_jwk])
        jws_compact = self._hand_craft_jws_with_crit(key, profile, bad_crit)
        signed = {**profile, "signature": jws_compact}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
        assert exc.value.code == "malformed_jws"


class TestVerifierCanonicalizationTypedErrors:
    """Verifier-side canonicalize must NEVER leak raw ValueError; always UCPVerificationError(body_mismatch)."""

    def _make_signed(self) -> tuple[dict, dict]:
        key = generate_ucp_signing_key(kid="k")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="k")
        return signed, build_jwks_response([key.public_jwk])

    def test_received_profile_with_float_raises_typed_body_mismatch(self) -> None:
        signed, jwks = self._make_signed()
        tampered = {**signed, "extras": {"n": 1.5}}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(tampered, jwks)
        assert exc.value.code == "body_mismatch"

    def test_received_profile_with_oversized_int_raises_typed_body_mismatch(self) -> None:
        signed, jwks = self._make_signed()
        tampered = {**signed, "extras": {"n": 2**60}}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(tampered, jwks)
        assert exc.value.code == "body_mismatch"

    def test_received_profile_with_nan_raises_typed_body_mismatch(self) -> None:
        signed, jwks = self._make_signed()
        tampered = {**signed, "extras": {"n": float("nan")}}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(tampered, jwks)
        assert exc.value.code == "body_mismatch"


class TestRejectUnsafeNumbersDictKeys:
    def test_sign_rejects_oversized_int_dict_key(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {2**60: "a"}}
        with pytest.raises(ValueError, match=r"2\^53"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_sign_rejects_float_dict_key(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {1.5: "a"}}
        with pytest.raises(ValueError, match="rejects float"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_sign_accepts_string_dict_keys_that_look_like_numbers(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"1.5": "a", "1152921504606846976": "b"}}
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        assert verify_ucp_profile(signed, build_jwks_response([signer.public_jwk])) is True

    def test_sign_accepts_bool_dict_keys(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {True: "x"}}
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        assert verify_ucp_profile(signed, build_jwks_response([signer.public_jwk])) is True


# U+2028 / U+2029 named via escape so the RUF001 ambiguous-character lint
# doesn't fire on the test inputs (the codepoints are intentional, not typos).
_U2028 = "\u2028"
_U2029 = "\u2029"


class TestLineParagraphSeparatorRejection:
    """U+2028 / U+2029 are escaped by pre-ES2019 V8 (``JSON.stringify`` emits
    the escaped sequences) but emitted raw by ``json.dumps(ensure_ascii=False)``.

    Modern V8 emits them raw too, so the divergence is theoretical on today's
    Node, but the rejection mirrors core/api/src/lib/canonicalize.ts so the
    contract stays symmetric for any pre-ES2019 verifier path (older V8,
    browser-side verifier code).
    """

    def test_rejects_u2028_at_top_level(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"note": f"before{_U2028}after"}}
        with pytest.raises(ValueError, match="U\\+2028"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_u2029_at_top_level(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"note": f"before{_U2029}after"}}
        with pytest.raises(ValueError, match="U\\+2029"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_u2028_nested_in_list(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"items": ["ok", f"bad{_U2028}tail"]}}
        with pytest.raises(ValueError, match="U\\+2028"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_u2029_nested_in_list(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"items": ["ok", f"bad{_U2029}tail"]}}
        with pytest.raises(ValueError, match="U\\+2029"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_u2028_nested_in_dict_value(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"deep": {"inner": f"before{_U2028}after"}}}
        with pytest.raises(ValueError, match="U\\+2028"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_u2029_nested_in_dict_value(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"deep": {"inner": f"before{_U2029}after"}}}
        with pytest.raises(ValueError, match="U\\+2029"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_rejects_u2028_in_dict_key(self) -> None:
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {f"bad{_U2028}key": "value"}}
        with pytest.raises(ValueError, match="U\\+2028"):
            sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")

    def test_accepts_u2027_sanity_case(self) -> None:
        # U+2027 (HYPHENATION POINT) is a different codepoint, not a target of
        # the rejection. Confirms we're matching exactly U+2028 / U+2029.
        signer = generate_ucp_signing_key(kid="k")
        profile = {**_base_profile([signer.public_jwk]), "extras": {"note": "before\u2027after"}}
        signed = sign_ucp_profile(profile, signing_key=signer.private_key, kid="k")
        assert verify_ucp_profile(signed, build_jwks_response([signer.public_jwk])) is True


class TestJWKUseAlgNullTreatedAsAbsent:
    """RFC 7517 lists ``use`` and ``alg`` as optional. Explicit JSON null is
    out-of-spec but harmless; treat null as absent (skip-on-null) so a JWK
    carrying ``"use": null`` or ``"alg": null`` matches the Node sibling's
    ``!= null`` semantics in ucp-jwks.ts and the two languages stay
    symmetric.
    """

    def test_verify_succeeds_when_matched_jwk_has_null_use(self) -> None:
        key = generate_ucp_signing_key(kid="null-use")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="null-use")
        jwks_with_null_use = build_jwks_response([{**key.public_jwk, "use": None}])
        assert verify_ucp_profile(signed, jwks_with_null_use) is True

    def test_verify_succeeds_when_matched_jwk_has_null_alg(self) -> None:
        key = generate_ucp_signing_key(kid="null-alg", alg="EdDSA")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="null-alg")
        jwks_with_null_alg = build_jwks_response([{**key.public_jwk, "alg": None}])
        assert verify_ucp_profile(signed, jwks_with_null_alg) is True

    def test_verify_still_rejects_use_enc_with_unusable_key(self) -> None:
        # Sanity: non-null wrong values continue to fail with unusable_key.
        key = generate_ucp_signing_key(kid="enc-sanity")
        profile = _base_profile([key.public_jwk])
        signed = sign_ucp_profile(profile, signing_key=key.private_key, kid="enc-sanity")
        enc_jwk = {**key.public_jwk, "use": "enc"}
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(signed, build_jwks_response([enc_jwk]))
        assert exc.value.code == "unusable_key"


class TestVerifierErrorPrecedence:
    def test_null_profile_with_malformed_jwks_returns_no_signature(self) -> None:
        with pytest.raises(UCPVerificationError) as exc:
            verify_ucp_profile(None, "not a jwks")  # type: ignore[arg-type]
        assert exc.value.code == "no_signature"
