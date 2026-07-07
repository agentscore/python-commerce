"""RFC 9421 HTTP Message Signature (AIP subset) — sign / verify + cross-language conformance.

Ports node-commerce ``tests/aip_http_signature.test.ts``. Two things are pinned here:

1. The local sign + verify round trip and every typed failure mode, mirroring the node suite.
2. **The byte-compatibility gate.** ``@agent-score/pay`` is the AIP signer (a separate
   ``node:crypto`` reimplementation); ``core/api`` is the authoritative verifier; node-commerce
   and python-commerce are the SDK verifiers. Ed25519 is deterministic (RFC 8032), so a fixed key
   + fixed request yields fixed bytes. We lift the SAME pinned vectors that
   ``pay/tests/aip_http_signature.test.ts`` and
   ``core/api/tests/aip-http-signature-conformance.test.ts`` pin, and assert (a) python's verify
   ACCEPTS pay's real signer output, and (b) python's own sign emits the node/api signMessage
   vector byte-for-byte. If any implementation's wire format drifts, one of these breaks.
"""

from __future__ import annotations

import warnings

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentscore_commerce.aip import (
    AIP_COVERED_COMPONENTS,
    AIP_SIGNATURE_TAG,
    build_signature_base,
    normalize_authority,
    parse_signature_input,
    parse_signature_value,
    sign_message,
    verify_message_signature,
)
from agentscore_commerce.aip.http_signature import SignatureParams, _calculate_jwk_thumbprint

# Filter joserfc's EdDSA deprecation SecurityWarning (RFC 9864) for the whole module — AIP pins
# Ed25519 as its only signing curve, so the warning is expected and not actionable here.
pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


# A fresh Ed25519 keypair for the local round-trip suite (mirrors node's beforeAll generateKeyPair).
def _make_key() -> tuple[dict, dict, str]:
    raw = Ed25519PrivateKey.generate()
    from joserfc.jwk import OKPKey

    key = OKPKey.import_key(raw)
    priv = key.as_dict(private=True)
    pub = key.as_dict(private=False)
    return priv, pub, _calculate_jwk_thumbprint(pub)


PRIVATE_JWK, PUBLIC_JWK, THUMBPRINT = _make_key()

BASE_REQ = {
    "method": "POST",
    "authority": "wine-merchant.com",
    "path": "/checkout",
    "agent_identity": "eyJhbGciOiJFZERTQSJ9.payload.sig",
}


def _round_trip(**overrides: object):
    # The verifier now REQUIRES `expires` (replay-window hardening), so default to a 60s window
    # (matching pay's signer) unless a test overrides it. `sign_message` itself omits `expires` by
    # default — that's only the serialization-format default, exercised explicitly below.
    created = overrides.get("created")
    expires_default = (created + 60) if isinstance(created, int) else None
    args = {
        **BASE_REQ,
        "private_jwk": PRIVATE_JWK,
        "public_jwk": PUBLIC_JWK,
        **({"expires": expires_default} if expires_default is not None else {}),
        **overrides,
    }
    sm = sign_message(**args)  # type: ignore[arg-type]
    return sm.signature_input, sm.signature


def _verify(over: dict | None = None, sig: tuple[str, str] | None = None):
    si, s = sig if sig is not None else _round_trip(created=1715400000)
    kwargs = {
        **BASE_REQ,
        "signature_input": si,
        "signature": s,
        "cnf_jwk": PUBLIC_JWK,
        "now": 1715400010,
        **(over or {}),
    }
    return verify_message_signature(**kwargs)  # type: ignore[arg-type]


# ── normalize_authority ──


class TestNormalizeAuthority:
    def test_lowercases_the_host(self) -> None:
        assert normalize_authority("Wine-Merchant.COM") == "wine-merchant.com"

    def test_drops_default_ports_80_and_443(self) -> None:
        assert normalize_authority("host.com:443") == "host.com"
        assert normalize_authority("host.com:80") == "host.com"

    def test_keeps_non_default_ports(self) -> None:
        assert normalize_authority("host.com:3003") == "host.com:3003"

    def test_leaves_ipv6_literals_without_a_port_intact(self) -> None:
        assert normalize_authority("[::1]") == "[::1]"


# ── build_signature_base ──


class TestBuildSignatureBase:
    def test_emits_one_line_per_component_plus_signature_params_no_trailing_newline(self) -> None:
        base = build_signature_base(
            SignatureParams(
                components=["@method", "@authority", "@path", "agent-identity"],
                created=1715400000,
                keyid="kid",
                tag="agent-identity",
            ),
            method=BASE_REQ["method"],
            authority=BASE_REQ["authority"],
            path=BASE_REQ["path"],
            agent_identity=BASE_REQ["agent_identity"],
        )
        lines = base.split("\n")
        assert lines[0] == '"@method": POST'
        assert lines[1] == '"@authority": wine-merchant.com'
        assert lines[2] == '"@path": /checkout'
        assert lines[3] == f'"agent-identity": {BASE_REQ["agent_identity"]}'
        assert lines[4] == (
            '"@signature-params": ("@method" "@authority" "@path" "agent-identity")'
            ';created=1715400000;keyid="kid";tag="agent-identity"'
        )
        assert not base.endswith("\n")

    def test_raises_when_a_covered_component_has_no_value(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 — _MissingComponentError is private
            build_signature_base(
                SignatureParams(components=["@method", "x-missing"]),
                method=BASE_REQ["method"],
                authority=BASE_REQ["authority"],
                path=BASE_REQ["path"],
                agent_identity=BASE_REQ["agent_identity"],
            )


# ── parse_signature_input ──


class TestParseSignatureInput:
    def test_parses_components_and_params_from_a_single_member(self) -> None:
        header = (
            'ait=("@method" "@authority" "@path" "agent-identity")'
            ';created=1715400000;expires=1715400060;keyid="abc";tag="agent-identity"'
        )
        parsed = parse_signature_input(header)
        assert parsed is not None
        assert parsed.label == "ait"
        assert parsed.params.components == ["@method", "@authority", "@path", "agent-identity"]
        assert parsed.params.created == 1715400000
        assert parsed.params.expires == 1715400060
        assert parsed.params.keyid == "abc"
        assert parsed.params.tag == "agent-identity"

    def test_selects_the_agent_identity_member_when_a_web_bot_auth_member_coexists(self) -> None:
        header = (
            'web-bot=("@authority");created=1;tag="web-bot-auth", '
            'ait=("@method" "@authority" "@path" "agent-identity");created=2;keyid="k";tag="agent-identity"'
        )
        parsed = parse_signature_input(header)
        assert parsed is not None
        assert parsed.label == "ait"
        assert parsed.params.created == 2

    def test_rejects_a_sole_untagged_member(self) -> None:
        # The spec requires tag="agent-identity"; an untagged member is skipped like any
        # wrong-tagged member, even when it is the only one.
        header = 'sig1=("@method" "@path");created=5;keyid="k"'
        assert parse_signature_input(header) is None

    def test_returns_none_when_only_non_aip_tagged_members_are_present(self) -> None:
        header = 'web-bot=("@authority");created=1;tag="web-bot-auth"'
        assert parse_signature_input(header) is None

    def test_returns_none_on_malformed_input(self) -> None:
        assert parse_signature_input("garbage") is None


# ── parse_signature_value ──


class TestParseSignatureValue:
    def test_extracts_a_base64_byte_sequence_for_the_label(self) -> None:
        b = parse_signature_value("ait=:AQID:", "ait")
        assert b == bytes([1, 2, 3])

    def test_returns_none_for_a_missing_label(self) -> None:
        assert parse_signature_value("ait=:AQID:", "other") is None

    def test_does_not_split_a_byte_sequence_containing_a_comma_like_char(self) -> None:
        # base64 never contains commas, but ensure colon-delimited parsing is intact across members.
        header = "web-bot=:AAAA:, ait=:AQID:"
        assert parse_signature_value(header, "ait") == bytes([1, 2, 3])


# ── sign + verify round trip ──


class TestSignVerifyRoundTrip:
    def test_verifies_a_freshly_signed_request(self) -> None:
        si, s = _round_trip(created=1715400000)
        r = verify_message_signature(
            **BASE_REQ,  # type: ignore[arg-type]
            signature_input=si,
            signature=s,
            cnf_jwk=PUBLIC_JWK,
            now=1715400010,
        )
        assert r.ok is True

    def test_sets_keyid_to_the_public_key_thumbprint_and_tag_to_agent_identity(self) -> None:
        si, _ = _round_trip()
        parsed = parse_signature_input(si)
        assert parsed is not None
        assert parsed.params.keyid == THUMBPRINT
        assert parsed.params.tag == AIP_SIGNATURE_TAG

    def test_covers_exactly_the_aip_minimum_components_by_default(self) -> None:
        si, _ = _round_trip()
        parsed = parse_signature_input(si)
        assert parsed is not None
        assert parsed.params.components == list(AIP_COVERED_COMPONENTS)


# ── failure modes ──


class TestVerifyFailureModes:
    def test_rejects_a_tampered_method(self) -> None:
        r = _verify({"method": "GET"})
        assert (r.ok, r.reason) == (False, "signature_invalid")

    def test_rejects_a_tampered_path(self) -> None:
        r = _verify({"path": "/admin"})
        assert (r.ok, r.reason) == (False, "signature_invalid")

    def test_rejects_a_tampered_authority(self) -> None:
        r = _verify({"authority": "evil.com"})
        assert (r.ok, r.reason) == (False, "signature_invalid")

    def test_rejects_a_swapped_agent_identity_value(self) -> None:
        r = _verify({"agent_identity": "different.token.here"})
        assert (r.ok, r.reason) == (False, "signature_invalid")

    def test_rejects_when_cnf_jwk_is_a_different_key_keyid_mismatch(self) -> None:
        other_priv, other_pub, _ = _make_key()
        sm = sign_message(
            **BASE_REQ,  # type: ignore[arg-type]
            private_jwk=other_priv,
            public_jwk=other_pub,
            created=1715400000,
            # created+expires present so the sig reaches the keyid check, not the time-bound gates.
            expires=1715400060,
        )
        # present the wrong cnf (our original key) — keyid in the sig won't match its thumbprint
        r = verify_message_signature(
            **BASE_REQ,  # type: ignore[arg-type]
            signature_input=sm.signature_input,
            signature=sm.signature,
            cnf_jwk=PUBLIC_JWK,
            now=1715400010,
        )
        assert (r.ok, r.reason) == (False, "keyid_mismatch")

    def test_rejects_an_expired_signature_beyond_skew(self) -> None:
        sig = _round_trip(created=1715400000, expires=1715400060)
        r = _verify({"now": 1715400200}, sig)
        assert (r.ok, r.reason) == (False, "expired")

    def test_accepts_an_expired_signature_within_skew_tolerance(self) -> None:
        sig = _round_trip(created=1715400000, expires=1715400060)
        r = _verify({"now": 1715400080, "max_skew_seconds": 30}, sig)
        assert r.ok is True

    def test_rejects_a_created_timestamp_too_far_in_the_future(self) -> None:
        sig = _round_trip(created=1715400000)
        r = _verify({"now": 1715300000}, sig)
        assert (r.ok, r.reason) == (False, "created_in_future")

    def test_rejects_when_the_aip_minimum_components_are_not_all_covered(self) -> None:
        sig = _round_trip(created=1715400000, components=["@method", "@authority"])
        r = _verify({}, sig)
        assert (r.ok, r.reason) == (False, "missing_covered_component")

    def test_rejects_a_signature_missing_created(self) -> None:
        # Hand-build a Signature-Input with neither created nor expires, signed over that exact base,
        # so the missing-created branch fires before byte verification. An unbounded PoP is replayable.
        sm = sign_message(
            **BASE_REQ,  # type: ignore[arg-type]
            private_jwk=PRIVATE_JWK,
            public_jwk=PUBLIC_JWK,
            created=1715400000,
        )
        header = f'ait=("@method" "@authority" "@path" "agent-identity");keyid="{THUMBPRINT}";tag="agent-identity"'
        r = _verify({"signature_input": header}, (header, sm.signature))
        assert (r.ok, r.reason) == (False, "created_missing")

    def test_rejects_a_signature_missing_expires(self) -> None:
        # sign_message omits `expires` by default — exactly the spec-loose shape the hardening rejects.
        sm = sign_message(
            **BASE_REQ,  # type: ignore[arg-type]
            private_jwk=PRIVATE_JWK,
            public_jwk=PUBLIC_JWK,
            created=1715400000,
        )
        r = _verify({}, (sm.signature_input, sm.signature))
        assert (r.ok, r.reason) == (False, "expires_missing")

    def test_rejects_a_signature_whose_declared_window_exceeds_the_120s_ceiling(self) -> None:
        # created+expires alone only bound replay to whatever window the SIGNER chose; the HTTP-sig
        # layer caps it at 120s. A 300s window (an attacker matching the AIT's 300s ceiling) is
        # rejected even though created/expires are present and `now` sits inside the window.
        sig = _round_trip(created=1715400000, expires=1715400300)  # 300s > 120s
        r = _verify({"now": 1715400005}, sig)  # well inside the declared window
        assert (r.ok, r.reason) == (False, "pop_window_too_long")

    def test_accepts_a_signature_whose_declared_window_equals_the_120s_ceiling(self) -> None:
        sig = _round_trip(created=1715400000, expires=1715400120)  # exactly 120s
        r = _verify({"now": 1715400005}, sig)
        assert r.ok is True

    def test_verifies_a_valid_60s_window_pop_the_first_party_pay_signer_shape(self) -> None:
        # pay signs created + expires = created + 60. This must still verify post-hardening.
        sig = _round_trip(created=1715400000, expires=1715400060)
        r = _verify({"now": 1715400010}, sig)
        assert r.ok is True

    def test_rejects_a_non_ed25519_alg_param(self) -> None:
        header = (
            'ait=("@method" "@authority" "@path" "agent-identity")'
            ';created=1715400000;keyid="k";alg="rsa";tag="agent-identity"'
        )
        r = _verify({"signature_input": header}, (header, "ait=:AA:"))
        assert (r.ok, r.reason) == (False, "unsupported_alg")

    def test_accepts_the_jws_alg_spelling_eddsa_at_the_alg_gate_case_insensitive(self) -> None:
        # ed25519 is the RFC 9421 label; EdDSA is the JWS label. We accept both, so an external
        # signer emitting alg="EdDSA" must pass the alg gate and fail later for a real reason.
        # (created+expires present so the alg-spelling reaches the keyid check, not the time gates.)
        header = (
            'ait=("@method" "@authority" "@path" "agent-identity")'
            ';created=1715400000;expires=1715400060;keyid="k";alg="EdDSA";tag="agent-identity"'
        )
        r = _verify({"signature_input": header}, (header, "ait=:AA:"))
        assert (r.ok, r.reason) == (False, "keyid_mismatch")

    def test_returns_no_aip_signature_when_no_member_matches(self) -> None:
        header = 'web-bot=("@authority");created=1;tag="web-bot-auth"'
        r = _verify({"signature_input": header}, (header, "web-bot=:AA:"))
        assert (r.ok, r.reason) == (False, "no_aip_signature")

    def test_returns_no_aip_signature_for_a_sole_untagged_member(self) -> None:
        header = (
            f'ait=("@method" "@authority" "@path" "agent-identity");created=1715400000'
            f';expires=1715400060;keyid="{THUMBPRINT}"'
        )
        r = _verify({"signature_input": header}, (header, "ait=:AA:"))
        assert (r.ok, r.reason) == (False, "no_aip_signature")

    def test_rejects_a_negative_window_expires_before_created(self) -> None:
        sig = _round_trip(created=1715400000, expires=1715399940)  # expires < created
        r = _verify({"now": 1715400005}, sig)
        assert (r.ok, r.reason) == (False, "pop_window_too_long")

    def test_verifies_a_signature_with_non_canonical_param_order(self) -> None:
        # A spec-legal signer may emit the @signature-params members in any order; verification
        # must run over the RAW received serialization, not a fixed-order re-serialization.
        import base64 as _b64

        from joserfc.jwk import OKPKey

        raw_params = (
            '("@method" "@authority" "@path" "agent-identity")'
            f';keyid="{THUMBPRINT}";created=1715400000;expires=1715400060;tag="agent-identity"'
        )
        base = "\n".join(
            [
                '"@method": POST',
                '"@authority": wine-merchant.com',
                '"@path": /checkout',
                f'"agent-identity": {BASE_REQ["agent_identity"]}',
                f'"@signature-params": {raw_params}',
            ]
        )
        sig_bytes = OKPKey.import_key(PRIVATE_JWK).raw_value.sign(base.encode("utf-8"))
        header = f"ait={raw_params}"
        signature = f"ait=:{_b64.b64encode(sig_bytes).decode('ascii')}:"
        r = _verify({"now": 1715400010, "signature_input": header}, (header, signature))
        assert r.ok is True

    def test_returns_malformed_signature_when_the_dict_lacks_the_selected_label(self) -> None:
        sig = _round_trip(created=1715400000)
        r = _verify({"signature": "wronglabel=:AQID:"}, (sig[0], "wronglabel=:AQID:"))
        assert (r.ok, r.reason) == (False, "malformed_signature")

    def test_rejects_a_p256_cnf_key_with_a_typed_failure_no_throw(self) -> None:
        r = _verify({"cnf_jwk": {"kty": "EC", "crv": "P-256", "x": "abc", "y": "def"}})
        assert (r.ok, r.reason) == (False, "unsupported_cnf_key")

    def test_rejects_a_malformed_okp_cnf_with_non_string_x_without_throwing(self) -> None:
        r = _verify({"cnf_jwk": {"kty": "OKP", "crv": "Ed25519", "x": 123}})
        assert (r.ok, r.reason) == (False, "unsupported_cnf_key")

    def test_rejects_an_okp_cnf_missing_x_without_throwing(self) -> None:
        r = _verify({"cnf_jwk": {"kty": "OKP", "crv": "Ed25519"}})
        assert (r.ok, r.reason) == (False, "unsupported_cnf_key")


# ──────────────────────────────────────────────────────────────────────────────────────────────
# CROSS-LANGUAGE BYTE-COMPATIBILITY GATE
#
# These vectors are LIFTED VERBATIM from:
#   - pay/tests/aip_http_signature.test.ts  (pay's node:crypto signer; always sets `expires`)
#   - core/api/tests/aip-http-signature-conformance.test.ts  (the authoritative API verifier;
#     node-commerce signMessage shares this vector, which OMITS `expires`)
# Same fixed Ed25519 TEST key, same request. If python's wire format drifts from node/pay/api,
# one of these assertions fails. This is the conformance lock across all four implementations.
# ──────────────────────────────────────────────────────────────────────────────────────────────

# Fixed Ed25519 TEST key shared across repos (NOT a secret). Ed25519 is deterministic, so the
# bytes below are stable.
_VECTOR_PRIVATE_JWK = {
    "kty": "OKP",
    "crv": "Ed25519",
    "x": "5vqAF8eRpE9bBrvNDfMcl4s1YKKEj_IjkC1Sb9RX7zQ",
    "d": "uZdreBtmZKhj5plteN2V8We6uI8o4hkNbJX_hbcCJUk",
}
_VECTOR_PUBLIC_JWK = {"kty": "OKP", "crv": "Ed25519", "x": "5vqAF8eRpE9bBrvNDfMcl4s1YKKEj_IjkC1Sb9RX7zQ"}
_VECTOR_THUMBPRINT = "mkD85lyXqPsAQ7obXi4KLYtotBqEZP7j0U23VKZc8EI"
_VECTOR_REQ = {"method": "POST", "authority": "merchant.example.com", "path": "/checkout"}
_VECTOR_TOKEN = "AIT.fixture.token"
_VECTOR_CREATED = 1715400000

# node-commerce / core-api signMessage output (NO `expires`).
_NODE_SIGNATURE_INPUT = (
    'ait=("@method" "@authority" "@path" "agent-identity")'
    ';created=1715400000;keyid="mkD85lyXqPsAQ7obXi4KLYtotBqEZP7j0U23VKZc8EI";tag="agent-identity"'
)
_NODE_SIGNATURE = "ait=:TYseWf1kre1MSojIjlM+eXaLmZEe7gXHYLGydW2fMek48hJX4q/6oJJ4G8VpVC8MvqYWpbqkd5uP8VOGxuVOAw==:"

# pay's signAitRequest output (ALWAYS sets `expires = created + 60`).
_PAY_SIGNATURE_INPUT = (
    'ait=("@method" "@authority" "@path" "agent-identity")'
    ';created=1715400000;expires=1715400060;keyid="mkD85lyXqPsAQ7obXi4KLYtotBqEZP7j0U23VKZc8EI";tag="agent-identity"'
)
_PAY_SIGNATURE = "ait=:1CE7njbRqJUuxYtNcTFjTax1mg+52Rqc633BwdWqBraCur+PUmX8v0VN5y2QiiTl+22rD4f4RkSSINSrVI5pBQ==:"


class TestCrossLanguageConformance:
    def test_cnf_thumbprint_rfc7638_is_stable_and_matches_the_pinned_value(self) -> None:
        assert _calculate_jwk_thumbprint(_VECTOR_PUBLIC_JWK) == _VECTOR_THUMBPRINT

    def test_build_signature_base_produces_the_exact_canonical_base(self) -> None:
        expected_base = "\n".join(
            [
                '"@method": POST',
                '"@authority": merchant.example.com',
                '"@path": /checkout',
                '"agent-identity": AIT.fixture.token',
                '"@signature-params": ("@method" "@authority" "@path" "agent-identity")'
                ';created=1715400000;keyid="mkD85lyXqPsAQ7obXi4KLYtotBqEZP7j0U23VKZc8EI";tag="agent-identity"',
            ]
        )
        base = build_signature_base(
            SignatureParams(
                components=list(AIP_COVERED_COMPONENTS),
                created=_VECTOR_CREATED,
                keyid=_VECTOR_THUMBPRINT,
                tag="agent-identity",
            ),
            method=_VECTOR_REQ["method"],
            authority=_VECTOR_REQ["authority"],
            path=_VECTOR_REQ["path"],
            agent_identity=_VECTOR_TOKEN,
        )
        assert base == expected_base

    def test_python_sign_message_emits_the_exact_node_api_vector_byte_for_byte(self) -> None:
        # Python's own signer (joserfc/cryptography Ed25519) MUST produce the identical
        # deterministic bytes that node-commerce + core/api signMessage produce.
        sm = sign_message(
            **_VECTOR_REQ,  # type: ignore[arg-type]
            agent_identity=_VECTOR_TOKEN,
            private_jwk=_VECTOR_PRIVATE_JWK,
            public_jwk=_VECTOR_PUBLIC_JWK,
            created=_VECTOR_CREATED,
        )
        assert sm.signature_input == _NODE_SIGNATURE_INPUT
        assert sm.signature == _NODE_SIGNATURE

    def test_python_verify_accepts_pays_real_signer_output_cross_repo(self) -> None:
        # THE GATE: a node/pay-signed proof-of-possession MUST verify in python.
        r = verify_message_signature(
            **_VECTOR_REQ,  # type: ignore[arg-type]
            agent_identity=_VECTOR_TOKEN,
            signature_input=_PAY_SIGNATURE_INPUT,
            signature=_PAY_SIGNATURE,
            cnf_jwk=_VECTOR_PUBLIC_JWK,
            now=1715400030,
        )
        assert r.ok is True

    def test_python_verify_rejects_the_node_api_vector_missing_expires(self) -> None:
        # The byte-pinned node/api signMessage vector carries `created` but OMITS `expires` — exactly
        # the spec-loose shape the replay-window hardening rejects. (Mirrors core/api's conformance
        # test, which now asserts `expires_missing` for this same vector.) The WITH-`expires` accept
        # path is covered by the pay vector above and the explicit accept test below.
        r = verify_message_signature(
            **_VECTOR_REQ,  # type: ignore[arg-type]
            agent_identity=_VECTOR_TOKEN,
            signature_input=_NODE_SIGNATURE_INPUT,
            signature=_NODE_SIGNATURE,
            cnf_jwk=_VECTOR_PUBLIC_JWK,
            now=_VECTOR_CREATED + 5,
        )
        assert (r.ok, r.reason) == (False, "expires_missing")

    def test_python_verify_accepts_the_node_api_signer_with_expires(self) -> None:
        # The node/api signMessage signer with `expires` set (a 60s window) MUST verify. We sign a
        # fresh token here (rather than byte-pin) because the byte-pinned vector deliberately omits
        # `expires`; this exercises the WITH-time-bound accept path for the node/api signer shape.
        sm = sign_message(
            **_VECTOR_REQ,  # type: ignore[arg-type]
            agent_identity=_VECTOR_TOKEN,
            private_jwk=_VECTOR_PRIVATE_JWK,
            public_jwk=_VECTOR_PUBLIC_JWK,
            created=_VECTOR_CREATED,
            expires=_VECTOR_CREATED + 60,
        )
        r = verify_message_signature(
            **_VECTOR_REQ,  # type: ignore[arg-type]
            agent_identity=_VECTOR_TOKEN,
            signature_input=sm.signature_input,
            signature=sm.signature,
            cnf_jwk=_VECTOR_PUBLIC_JWK,
            now=_VECTOR_CREATED + 5,
        )
        assert r.ok is True

    def test_python_own_sign_plus_verify_round_trips_for_the_pinned_key(self) -> None:
        sm = sign_message(
            **_VECTOR_REQ,  # type: ignore[arg-type]
            agent_identity=_VECTOR_TOKEN,
            private_jwk=_VECTOR_PRIVATE_JWK,
            public_jwk=_VECTOR_PUBLIC_JWK,
            created=_VECTOR_CREATED,
            # PoP verifier requires `expires` (replay-window hardening); 60s window like pay.
            expires=_VECTOR_CREATED + 60,
        )
        r = verify_message_signature(
            **_VECTOR_REQ,  # type: ignore[arg-type]
            agent_identity=_VECTOR_TOKEN,
            signature_input=sm.signature_input,
            signature=sm.signature,
            cnf_jwk=_VECTOR_PUBLIC_JWK,
            now=_VECTOR_CREATED + 5,
        )
        assert r.ok is True

    def test_verify_rejects_pays_vector_when_the_request_is_tampered(self) -> None:
        for tamper in ({"method": "GET"}, {"authority": "evil.example.com"}, {"path": "/admin"}):
            r = verify_message_signature(
                **{**_VECTOR_REQ, **tamper},  # type: ignore[arg-type]
                agent_identity=_VECTOR_TOKEN,
                signature_input=_PAY_SIGNATURE_INPUT,
                signature=_PAY_SIGNATURE,
                cnf_jwk=_VECTOR_PUBLIC_JWK,
                now=1715400030,
            )
            assert r.ok is False

    def test_verify_rejects_a_swapped_agent_identity_for_the_pinned_vector(self) -> None:
        r = verify_message_signature(
            **_VECTOR_REQ,  # type: ignore[arg-type]
            agent_identity="DIFFERENT.ait.token",
            signature_input=_PAY_SIGNATURE_INPUT,
            signature=_PAY_SIGNATURE,
            cnf_jwk=_VECTOR_PUBLIC_JWK,
            now=1715400030,
        )
        assert r.ok is False


# Belt-and-suspenders: a stray import so the module-level `warnings` use is real (joserfc emits
# the EdDSA SecurityWarning at key-import time; pytestmark filters it, this keeps linters honest).
_ = warnings
