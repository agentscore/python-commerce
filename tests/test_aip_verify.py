"""The AIT verification pipeline (verifier orchestrator).

Ports node-commerce ``tests/aip_verify.test.ts``. ``verify_ait`` is **async** in Python (vs node's
async too) and returns a ``VerifyAitResult`` dataclass (``.ok`` / ``.ait`` / ``.reason``). AITs are
minted with ``joserfc.jwt.encode`` (node uses jose ``SignJWT``); the IdP JWKS is served by an
injected async fetch impl on a ``JwksCache``. The RFC 9421 PoP signature is produced by
``sign_message``.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key as generate_rsa_key
from joserfc import jwt
from joserfc.jwk import ECKey, OKPKey, RSAKey

from agentscore_commerce.aip import JwksCache, VerifyRequestContext, sign_message, verify_ait

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

ISS = "https://issuer.example"
KID = "partner-key-2026-05"
NOW = 1715400020

# IdP signing keypair (Ed25519) + agent (cnf) keypair (Ed25519).
_idp = OKPKey.import_key(Ed25519PrivateKey.generate())
IDP_PUBLIC_JWK = {**_idp.as_dict(private=False), "kid": KID, "use": "sig", "alg": "EdDSA"}
_agent = OKPKey.import_key(Ed25519PrivateKey.generate())
AGENT_PRIVATE_JWK = _agent.as_dict(private=True)
AGENT_PUBLIC_JWK = _agent.as_dict(private=False)

REQ = {"method": "POST", "authority": "wine-merchant.com", "path": "/checkout"}


def _encode(header: dict, claims: dict, key: Any, alg: str) -> str:
    return jwt.encode(header, claims, key, algorithms=[alg])


def mint_ait(
    *,
    iss: str = ISS,
    kid: str = KID,
    iat: int = 1715400000,
    exp: int = 1715400300,
    trust_level: str = "human_present",
    amr: list[str] | None = None,
    cnf_jwk: dict | None = None,
    omit_agent: bool = False,
    signing_key: Any = None,
    signing_alg: str = "EdDSA",
    header_alg: str = "EdDSA",
) -> str:
    payload: dict = {
        "aip_version": "0.1",
        "sub": "user_abc123",
        "cnf": {"jwk": cnf_jwk if cnf_jwk is not None else AGENT_PUBLIC_JWK},
        "trust_level": trust_level,
        "identity": {"email": "b@example.com", "email_verified": True},
        "iss": iss,
        "iat": iat,
        "exp": exp,
    }
    if not omit_agent:
        payload["agent"] = {"provider": "anthropic", "instance": "sess-1"}
    if amr is not None:
        payload["auth"] = {"amr": amr, "time": 1715399900}
    return _encode({"alg": header_alg, "typ": "jwt", "kid": kid}, payload, signing_key or _idp, signing_alg)


class _Resp:
    ok = True
    status = 200

    def __init__(self, keys: list[dict]) -> None:
        self._keys = keys

    @property
    def headers(self) -> Any:
        class _H:
            @staticmethod
            def get(name: str, default: Any = None) -> Any:
                return "max-age=300" if str(name).lower() == "cache-control" else default

        return _H()

    def json(self) -> Any:
        return {"keys": self._keys}


def jwks_for(public_jwk: dict) -> JwksCache:
    async def fetch(url: str, headers: dict) -> _Resp:
        return _Resp([public_jwk])

    return JwksCache(trusted_issuers=[ISS], fetch_impl=fetch)


def signed_ctx(
    token: str,
    sign_with: dict | None = None,
    sign_pub: dict | None = None,
    created: int = 1715400010,
) -> VerifyRequestContext:
    sm = sign_message(
        **REQ,  # type: ignore[arg-type]
        agent_identity=token,
        private_jwk=sign_with if sign_with is not None else AGENT_PRIVATE_JWK,
        public_jwk=sign_pub if sign_pub is not None else AGENT_PUBLIC_JWK,
        created=created,
        # The PoP verifier now REQUIRES `expires` (replay-window hardening); sign a 60s window like pay.
        expires=created + 60,
    )
    return VerifyRequestContext(
        method=REQ["method"],
        authority=REQ["authority"],
        path=REQ["path"],
        agent_identity_headers=[token],
        signature_input=sm.signature_input,
        signature=sm.signature,
    )


# ── happy path ──


class TestHappyPath:
    async def test_verifies_a_well_formed_signed_ait_end_to_end(self) -> None:
        ctx = signed_ctx(mint_ait())
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert r.ok is True
        assert r.ait is not None
        assert r.ait.iss == ISS
        assert r.ait.payload["identity"]["email"] == "b@example.com"

    async def test_verifies_a_human_confirmed_ait_that_carries_auth_amr(self) -> None:
        ctx = signed_ctx(mint_ait(trust_level="human_confirmed", amr=["face"]))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert r.ok is True


# ── token presence ──


class TestTokenPresence:
    async def test_returns_no_token_when_no_agent_identity_header(self) -> None:
        ctx = VerifyRequestContext(
            method=REQ["method"],
            authority=REQ["authority"],
            path=REQ["path"],
            agent_identity_headers=[],
            signature_input="x",
            signature="y",
        )
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "no_token")

    async def test_returns_pop_signature_missing_when_the_rfc9421_headers_are_absent(self) -> None:
        ctx = VerifyRequestContext(
            method=REQ["method"],
            authority=REQ["authority"],
            path=REQ["path"],
            agent_identity_headers=[mint_ait()],
            signature_input=None,
            signature=None,
        )
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "pop_signature_missing")

    async def test_accepts_a_bearer_prefixed_agent_identity_header(self) -> None:
        # Sign over the BARE AIT (Bearer is transport stripped before the crypto), then present it
        # WITH a Bearer prefix; the verifier reconstructs over the bare JWT.
        token = mint_ait()
        sm = sign_message(
            **REQ,  # type: ignore[arg-type]
            agent_identity=token,
            private_jwk=AGENT_PRIVATE_JWK,
            public_jwk=AGENT_PUBLIC_JWK,
            created=1715400010,
            # PoP verifier requires `expires` (replay-window hardening); 60s window like pay.
            expires=1715400070,
        )
        ctx = VerifyRequestContext(
            method=REQ["method"],
            authority=REQ["authority"],
            path=REQ["path"],
            agent_identity_headers=[f"Bearer {token}"],
            signature_input=sm.signature_input,
            signature=sm.signature,
        )
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert r.ok is True


# ── issuer + key ──


class TestIssuerAndKey:
    async def test_rejects_an_untrusted_issuer(self) -> None:
        ctx = signed_ctx(mint_ait(iss="https://evil.com"))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "untrusted_issuer")

    async def test_reports_key_unavailable_when_jwks_lacks_the_kid(self) -> None:
        ctx = signed_ctx(mint_ait(kid="unknown-kid"))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "key_unavailable")

    @pytest.mark.parametrize("iss", ["https://host:abc", "https://host:99999999", "https://[::1"])
    async def test_a_malformed_iss_maps_to_untrusted_issuer_instead_of_crashing(self, iss: str) -> None:
        # ``iss`` is attacker-controlled (read from the UNVERIFIED payload before any signature
        # check); these authorities made urlsplit/.port raise ValueError → an uncaught 500.
        ctx = signed_ctx(mint_ait(iss=iss))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "untrusted_issuer")


# ── signature + expiry ──


class TestSignatureAndExpiry:
    async def test_rejects_an_ait_signed_by_a_different_idp_key(self) -> None:
        # JWKS serves an unrelated key under the same kid → IdP sig fails.
        other = OKPKey.import_key(Ed25519PrivateKey.generate())
        other_pub = {**other.as_dict(private=False), "kid": KID, "use": "sig", "alg": "EdDSA"}
        ctx = signed_ctx(mint_ait())
        r = await verify_ait(ctx, jwks=jwks_for(other_pub), now=NOW)
        assert (r.ok, r.reason) == (False, "idp_signature_invalid")

    async def test_rejects_an_expired_ait(self) -> None:
        ctx = signed_ctx(mint_ait(iat=1715300000, exp=1715300300), created=1715300010)
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "expired_token")

    async def test_rejects_an_ait_whose_iat_is_in_the_future(self) -> None:
        # Not expired (exp ahead of now) and the PoP is fresh, but iat is well beyond now+skew.
        ctx = signed_ctx(mint_ait(iat=NOW + 1000, exp=NOW + 1300))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "expired_token")

    async def test_rejects_an_ait_with_an_absurdly_long_lifetime(self) -> None:
        # Valid window around NOW but a ~2h lifetime, well over the 300s default ceiling.
        ctx = signed_ctx(mint_ait(iat=NOW - 100, exp=NOW + 7200))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "expired_token")

    async def test_rejects_an_ait_whose_lifetime_exceeds_the_300s_edge_ceiling(self) -> None:
        # A 600s-lifetime AIT (under the old 3600 default, over the new 300) is now rejected at the
        # edge, matching the authoritative API verifier. The PoP is fresh and exp is ahead of now —
        # only the exp-iat span is the problem. (Lowered 3600 -> 300.)
        ctx = signed_ctx(mint_ait(iat=NOW - 10, exp=NOW + 590))  # 600s lifetime > 300s
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "expired_token")

    async def test_accepts_an_ait_at_exactly_the_300s_lifetime_ceiling(self) -> None:
        # Our own mint sits exactly at 300s; it must pass.
        ctx = signed_ctx(mint_ait(iat=NOW - 10, exp=NOW + 290))  # exactly 300s
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert r.ok is True

    async def test_rejects_an_rs256_signed_ait_even_with_matching_rsa_key_alg_allowlist(self) -> None:
        # A trusted IdP publishing a non-Ed25519 use:sig key must NOT let an attacker present an
        # RS256 token that verifies. The alg allowlist pins EdDSA/ES256 regardless of the JWK.
        rsa = RSAKey.import_key(generate_rsa_key(public_exponent=65537, key_size=2048))
        rsa_pub = {**rsa.as_dict(private=False), "kid": KID, "use": "sig", "alg": "RS256"}
        token = mint_ait(signing_key=rsa, signing_alg="RS256", header_alg="RS256")
        ctx = signed_ctx(token)
        r = await verify_ait(ctx, jwks=jwks_for(rsa_pub), now=NOW)
        assert (r.ok, r.reason) == (False, "idp_signature_invalid")


# ── claim contract ──


class TestClaimContract:
    async def test_rejects_a_token_that_is_not_ait_shaped_no_agent_claim(self) -> None:
        ctx = signed_ctx(mint_ait(omit_agent=True))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "malformed_token")

    async def test_rejects_human_confirmed_without_auth_amr(self) -> None:
        ctx = signed_ctx(mint_ait(trust_level="human_confirmed"))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "invalid_claims")


# ── proof of possession ──


class TestProofOfPossession:
    async def test_rejects_when_signed_by_a_key_other_than_cnf_jwk(self) -> None:
        other = OKPKey.import_key(Ed25519PrivateKey.generate())
        ctx = signed_ctx(mint_ait(), sign_with=other.as_dict(private=True), sign_pub=other.as_dict(private=False))
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "pop_signature_invalid")

    async def test_rejects_a_request_whose_path_was_tampered_after_signing(self) -> None:
        ctx = signed_ctx(mint_ait())
        tampered = VerifyRequestContext(
            method=ctx.method,
            authority=ctx.authority,
            path="/admin",
            agent_identity_headers=ctx.agent_identity_headers,
            signature_input=ctx.signature_input,
            signature=ctx.signature,
        )
        r = await verify_ait(tampered, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert (r.ok, r.reason) == (False, "pop_signature_invalid")

    async def test_rejects_does_not_throw_on_an_ait_bound_to_a_p256_cnf_key(self) -> None:
        # The PoP verifier is Ed25519-only. A structurally-valid AIT whose cnf is a P-256 EC key
        # must return a typed failure, NOT crash the gate. Sign with the normal Ed25519 agent key —
        # the verifier rejects on the cnf key type first.
        ec = ECKey.import_key(generate_private_key(SECP256R1()))
        ec_pub = ec.as_dict(private=False)
        token = mint_ait(cnf_jwk=ec_pub)
        ctx = signed_ctx(token)
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert r.ok is False
        assert r.reason == "pop_signature_invalid"


# ── multiple AITs ──


class TestMultipleAits:
    async def test_verifies_when_one_of_several_headers_is_valid_and_matches_the_signature(self) -> None:
        good = mint_ait()
        bad_issuer = mint_ait(iss="https://evil.com")
        sm = sign_message(
            **REQ,  # type: ignore[arg-type]
            agent_identity=good,
            private_jwk=AGENT_PRIVATE_JWK,
            public_jwk=AGENT_PUBLIC_JWK,
            created=1715400010,
            # PoP verifier requires `expires` (replay-window hardening); 60s window like pay.
            expires=1715400070,
        )
        # present the bad-issuer one first, then the good one; only the SIGNED header passes PoP.
        ctx = VerifyRequestContext(
            method=REQ["method"],
            authority=REQ["authority"],
            path=REQ["path"],
            agent_identity_headers=[bad_issuer, good],
            signature_input=sm.signature_input,
            signature=sm.signature,
        )
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert r.ok is True
        assert r.ait is not None and r.ait.iss == ISS


# ── defense sanity (cnf key importable) ──


class TestDefenseSanity:
    async def test_the_cnf_key_returned_can_be_imported(self) -> None:
        from agentscore_commerce.aip.http_signature import _calculate_jwk_thumbprint

        ctx = signed_ctx(mint_ait())
        r = await verify_ait(ctx, jwks=jwks_for(IDP_PUBLIC_JWK), now=NOW)
        assert r.ok is True
        assert r.ait is not None
        key = OKPKey.import_key(r.ait.cnf_jwk)
        assert key is not None
        assert isinstance(_calculate_jwk_thumbprint(r.ait.cnf_jwk), str)


_ = warnings
