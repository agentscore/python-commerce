"""Framework-agnostic AIP gate: verify_ait_request + the wire-error mapping helpers.

Ports node-commerce ``tests/aip_gate.test.ts``. ``verify_ait_request`` is **async** and takes an
``AipGateOptions`` (vs node's options object). Node feeds a WHATWG ``Request``; here we feed a tiny
``_FakeRequest`` (``method`` / ``url`` / Starlette ``Headers``). ``build_aip_error_body`` returns a
plain ``dict`` (RFC 9457 problem-details), mirroring node's index-signature body.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from joserfc import jwt
from joserfc.jwk import OKPKey
from starlette.datastructures import Headers

from agentscore_commerce.aip import (
    AipGateOptions,
    JwksCache,
    aip_error_code,
    aip_error_status,
    build_aip_error_body,
    has_agent_identity_header,
    sign_message,
    verify_ait_request,
)

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

ISS = "https://issuer.example"
KID = "partner-key-2026-05"
NOW = 1715400020

_idp = OKPKey.import_key(Ed25519PrivateKey.generate())
IDP_PUBLIC_JWK = {**_idp.as_dict(private=False), "kid": KID, "use": "sig", "alg": "EdDSA"}
_agent = OKPKey.import_key(Ed25519PrivateKey.generate())
AGENT_PRIVATE_JWK = _agent.as_dict(private=True)
AGENT_PUBLIC_JWK = _agent.as_dict(private=False)


@dataclass
class _FakeRequest:
    method: str
    url: str
    headers: Headers


class _Resp:
    ok = True
    status = 200

    @property
    def headers(self) -> Any:
        class _H:
            @staticmethod
            def get(name: str, default: Any = None) -> Any:
                return "max-age=300" if str(name).lower() == "cache-control" else default

        return _H()

    def json(self) -> Any:
        return {"keys": [IDP_PUBLIC_JWK]}


def _jwks() -> JwksCache:
    async def fetch(url: str, headers: dict) -> _Resp:
        return _Resp()

    return JwksCache(trusted_issuers=[ISS], fetch_impl=fetch)


def _opts() -> AipGateOptions:
    return AipGateOptions(jwks=_jwks(), now=NOW)


def mint_ait(iss: str = ISS) -> str:
    return jwt.encode(
        {"alg": "EdDSA", "typ": "jwt", "kid": KID},
        {
            "aip_version": "0.1",
            "sub": "user_abc",
            "cnf": {"jwk": AGENT_PUBLIC_JWK},
            "agent": {"provider": "anthropic"},
            "trust_level": "human_present",
            "identity": {"email": "b@example.com", "email_verified": True, "age_over_21": True},
            "iss": iss,
            "iat": 1715400000,
            "exp": 1715400300,
        },
        _idp,
        algorithms=["EdDSA"],
    )


def signed_request(token: str, url: str = "https://wine-merchant.com/checkout") -> _FakeRequest:
    from urllib.parse import urlsplit

    u = urlsplit(url)
    sm = sign_message(
        method="POST",
        authority=u.netloc,
        path=u.path,
        agent_identity=token,
        private_jwk=AGENT_PRIVATE_JWK,
        public_jwk=AGENT_PUBLIC_JWK,
        created=1715400010,
        # PoP verifier requires `expires` (replay-window hardening); 60s window like pay.
        expires=1715400070,
    )
    headers = Headers(
        {
            "host": u.netloc,
            "agent-identity": token,
            "signature-input": sm.signature_input,
            "signature": sm.signature,
        }
    )
    return _FakeRequest(method="POST", url=url, headers=headers)


# ── verify_ait_request ──


class TestVerifyAitRequest:
    async def test_verifies_a_valid_signed_request_and_returns_the_claims(self) -> None:
        r = await verify_ait_request(signed_request(mint_ait()), _opts())
        assert r.ok is True
        assert r.ait is not None
        assert r.ait.iss == ISS
        assert r.ait.payload["identity"]["age_over_21"] is True

    async def test_fails_with_no_token_when_there_is_no_agent_identity_header(self) -> None:
        req = _FakeRequest(method="POST", url="https://wine-merchant.com/checkout", headers=Headers({}))
        r = await verify_ait_request(req, _opts())
        assert (r.ok, r.failure) == (False, "no_token")

    async def test_fails_with_untrusted_issuer_for_an_unknown_idp(self) -> None:
        r = await verify_ait_request(signed_request(mint_ait("https://evil.com")), _opts())
        assert (r.ok, r.failure) == (False, "untrusted_issuer")

    async def test_fails_with_pop_signature_invalid_when_the_path_was_tampered(self) -> None:
        # sign for /checkout but send to /admin (reuse the signed headers, change the URL/host).
        token = mint_ait()
        signed = signed_request(token, "https://wine-merchant.com/checkout")
        tampered = _FakeRequest(method="POST", url="https://wine-merchant.com/admin", headers=signed.headers)
        r = await verify_ait_request(tampered, _opts())
        assert (r.ok, r.failure) == (False, "pop_signature_invalid")


class TestHasAgentIdentityHeader:
    def test_detects_the_header(self) -> None:
        req = _FakeRequest(method="POST", url="https://m.com/x", headers=Headers({"agent-identity": "a.b.c"}))
        assert has_agent_identity_header(req) is True


# ── aip_error_status ──


class TestAipErrorStatus:
    def test_returns_403_for_trust_claims_failures(self) -> None:
        assert aip_error_status("untrusted_issuer") == 403
        assert aip_error_status("invalid_claims") == 403

    def test_returns_401_for_presence_signature_failures(self) -> None:
        for f in (
            "no_token",
            "pop_signature_missing",
            "expired_token",
            "malformed_token",
            "idp_signature_invalid",
            "pop_signature_invalid",
        ):
            assert aip_error_status(f) == 401

    def test_returns_503_for_key_unavailable(self) -> None:
        assert aip_error_status("key_unavailable") == 503


# ── aip_error_code ──


class TestAipErrorCode:
    def test_maps_presence_failures_to_agent_identity_required(self) -> None:
        assert aip_error_code("no_token") == "agent_identity_required"
        assert aip_error_code("pop_signature_missing") == "agent_identity_required"

    def test_maps_signature_malformed_failures_to_invalid_signature(self) -> None:
        assert aip_error_code("idp_signature_invalid") == "invalid_signature"
        assert aip_error_code("pop_signature_invalid") == "invalid_signature"
        assert aip_error_code("malformed_token") == "invalid_signature"

    def test_maps_key_unavailable_to_idp_unavailable(self) -> None:
        assert aip_error_code("key_unavailable") == "idp_unavailable"

    def test_passes_through_untrusted_issuer_and_expired_token(self) -> None:
        assert aip_error_code("untrusted_issuer") == "untrusted_issuer"
        assert aip_error_code("expired_token") == "expired_token"

    def test_maps_invalid_claims_to_insufficient_claims(self) -> None:
        assert aip_error_code("invalid_claims") == "insufficient_claims"


# ── build_aip_error_body ──


class TestBuildAipErrorBody:
    def test_produces_an_rfc9457_problem_details_body_with_a_urn_aip_error_type(self) -> None:
        body = build_aip_error_body("untrusted_issuer")
        assert body["type"] == "urn:aip:error:untrusted_issuer"
        assert body["title"] == "untrusted issuer"
        assert body["status"] == 403
        assert "trusted-issuer" in body["detail"]

    def test_status_field_matches_aip_error_status(self) -> None:
        assert build_aip_error_body("no_token")["status"] == 401
        assert build_aip_error_body("invalid_claims")["status"] == 403


class TestBuildAipPolicyDenyBody:
    def test_canonical_body_fields_ride_along_verbatim(self) -> None:
        from agentscore_commerce.aip.gate import build_aip_policy_deny_body

        body = {"error": {"code": "wallet_not_trusted", "message": "m"}, "agent_instructions": "{}"}
        superset = build_aip_policy_deny_body("wallet_not_trusted", ["sanctions_flagged"], body)
        assert superset["error"] == body["error"]
        assert superset["agent_instructions"] == body["agent_instructions"]
        assert superset["type"] == "urn:aip:error:insufficient_claims"
        assert superset["status"] == 403

    def test_merchant_extra_cannot_clobber_the_problem_json_envelope(self) -> None:
        # `body` carries merchant `extra` passthrough fields (on_before_session hook); a hook
        # echoing `status`/`type`/`title`/`detail` must not override the canonical envelope —
        # the caller derives the HTTP status from `superset["status"]`.
        from agentscore_commerce.aip.gate import build_aip_policy_deny_body

        body = {
            "error": {"code": "wallet_not_trusted", "message": "m"},
            "status": 200,
            "type": "https://evil.example/override",
            "title": "all good",
            "detail": "nothing to see",
        }
        superset = build_aip_policy_deny_body("wallet_not_trusted", None, body)
        assert superset["status"] == 403
        assert superset["type"] == "urn:aip:error:insufficient_claims"
        assert superset["title"] == "insufficient claims"
        assert "AgentScore decision" in superset["detail"]


_ = warnings
