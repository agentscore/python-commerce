"""Checkout x AIP.

Ports node-commerce ``tests/checkout_aip.test.ts`` + ``tests/checkout_aip_forward.test.ts``.

The AIP gate pre-step is now wired into :meth:`Checkout._run_gate` (parity with node's
``Checkout.runGate``): when ``gate.aip`` is configured and a settle-leg request carries an
``Agent-Identity`` header, the gate verifies the AIT at the edge BEFORE the assess call. A
present-but-invalid AIT is a hard ``application/problem+json`` deny; a valid one forwards
``aip_token`` + the RFC 9421 signature material to ``/v1/assess`` (via the SDK's
``assess(aip_token=, aip_signature=)``) so the API re-verifies PoP authoritatively, then trust
gating + per-issuer policy overrides apply.

The full happy-path verify (issuer JWKS + RFC 9421 PoP) is covered at the ``verify_ait_parts``
level in test_aip_verify / test_aip_gate. Here we assert the orchestrator contract: the invalid
cases (which fail before any JWKS fetch — no network needed), the offline (no-api_key) policy /
trust enforcement, issuer-conditional policy, and the assess-forwarding of token + signature.

The gate runs only on the settle leg (a payment credential attached), so each request carries an
``x-payment`` header — otherwise ``handle`` treats it as anonymous discovery and emits a 402.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from joserfc import jwt
from joserfc.jwk import OKPKey

from agentscore_commerce.aip import AGENTSCORE_CANONICAL_ISSUER, JwksCache, sign_message
from agentscore_commerce.checkout import (
    AipGateConfig,
    AipIssuerPolicy,
    Checkout,
    CheckoutGateConfig,
    CheckoutRailSpec,
    CheckoutRequest,
    PricingResult,
    build_aip_trusted_issuers,
)
from agentscore_commerce.payment.rail_spec import X402BaseRailSpec

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


# ── build_aip_trusted_issuers: the checkout-owned AIP-acceptance advertisement ──


class TestCheckoutBuildAipTrustedIssuers:
    def test_canonical_only_when_no_externals(self) -> None:
        assert build_aip_trusted_issuers() == [AGENTSCORE_CANONICAL_ISSUER]
        assert build_aip_trusted_issuers([]) == [AGENTSCORE_CANONICAL_ISSUER]

    def test_prepends_canonical_and_dedupes_after_canonicalization(self) -> None:
        out = build_aip_trusted_issuers(["https://issuer.example", "https://www.agentscore.com/"])
        assert out[0] == AGENTSCORE_CANONICAL_ISSUER
        assert any(i == "https://issuer.example" for i in out)
        # trailing-slash duplicate of the canonical issuer collapses on its canonical form.
        assert len([i for i in out if i == AGENTSCORE_CANONICAL_ISSUER]) == 1

    def test_keeps_first_seen_original_string_for_each_canonical_key(self) -> None:
        # An external issuer passed verbatim is preserved exactly (not re-canonicalized in the output).
        out = build_aip_trusted_issuers(["https://Issuer.Example"])
        assert any(i == "https://Issuer.Example" for i in out)


# ── CheckoutGateConfig now exposes the AIP wiring (flip of the prior "not yet" pin) ──


class TestCheckoutGateConfigAipWiring:
    def test_gate_config_exposes_an_aip_block(self) -> None:
        fields = {f.name for f in dataclasses.fields(CheckoutGateConfig)}
        # Node's CheckoutGateConfig carries a nested `aip`; python now mirrors it.
        assert "aip" in fields

    def test_aip_gate_config_carries_the_nested_knobs(self) -> None:
        afields = {f.name for f in dataclasses.fields(AipGateConfig)}
        # trustedIssuers / issuerPolicies / requireTrustLevel / requireAmr (+ authority / maxSkew).
        assert {
            "trusted_issuers",
            "issuer_policies",
            "require_trust_level",
            "require_amr",
            "authority",
            "max_skew_seconds",
        } <= afields

    def test_sdk_assess_accepts_aip_token_and_aip_signature(self) -> None:
        import inspect

        from agentscore import AgentScore

        # agentscore-py >= 2.4.4 forwards aip_token + aip_signature to /v1/assess (node parity).
        for method in (AgentScore.assess, AgentScore.aassess):
            params = set(inspect.signature(method).parameters)
            assert "aip_token" in params
            assert "aip_signature" in params


# ── AIT crypto fixtures (mirror node's jose mint + RFC 9421 sign) ──

ISS = "https://issuer.example"
OURS = "https://www.agentscore.com"
KID = "partner-key"
AUTHORITY = "wine.example"
URL = "https://wine.example/purchase"

_idp = OKPKey.import_key(Ed25519PrivateKey.generate())
IDP_PUBLIC_JWK = {**_idp.as_dict(private=False), "kid": KID, "use": "sig", "alg": "EdDSA"}
_agent = OKPKey.import_key(Ed25519PrivateKey.generate())
AGENT_PRIVATE_JWK = _agent.as_dict(private=True)
AGENT_PUBLIC_JWK = _agent.as_dict(private=False)


def _x402_rail() -> dict[str, CheckoutRailSpec]:
    return {"x402_base": X402BaseRailSpec(recipient="0xTREASURY", network="eip155:8453")}


def _make_checkout(gate: CheckoutGateConfig) -> Checkout:
    return Checkout(
        rails=_x402_rail(),
        url=URL,
        compute_pricing=lambda _ctx: PricingResult(amount_usd=50),
        gate=gate,
    )


def _req(headers: dict[str, str]) -> CheckoutRequest:
    # Settle-leg marker: `x-payment` makes `handle` run the gate (and thus the AIP pre-step)
    # instead of short-circuiting to an anonymous-discovery 402.
    return CheckoutRequest(
        method="POST",
        url=URL,
        headers={"x-payment": "eyJzdHViIjogdHJ1ZX0=", **headers},
        body={"product_id": "p1", "quantity": 1},
    )


def _jwks_for(keys_by_issuer: dict[str, dict[str, Any]]) -> JwksCache:
    """A JwksCache whose injected fetcher serves the right issuer's public JWK by URL host."""

    class _Resp:
        ok = True
        status = 200

        def __init__(self, jwk: dict[str, Any]) -> None:
            self._jwk = jwk

        @property
        def headers(self) -> Any:
            class _H:
                @staticmethod
                def get(name: str, default: Any = None) -> Any:
                    return "max-age=300" if str(name).lower() == "cache-control" else default

            return _H()

        def json(self) -> Any:
            return {"keys": [self._jwk]}

    async def fetch(url: str, _headers: dict[str, str]) -> _Resp:
        for iss, jwk in keys_by_issuer.items():
            host = urlsplit(iss).netloc
            if host and host in url:
                return _Resp(jwk)
        # Default to the first key when no host matches (single-issuer tests).
        return _Resp(next(iter(keys_by_issuer.values())))

    return JwksCache(trusted_issuers=list(keys_by_issuer), fetch_impl=fetch)


def _mint_ait(
    iss: str = ISS,
    *,
    kid: str = KID,
    signing_key: OKPKey | None = None,
    identity: dict[str, Any] | None = None,
    trust_level: str = "human_present",
    auth: dict[str, Any] | None = None,
    now_sec: int | None = None,
) -> str:
    # Live clock: Checkout's internal verify uses the real clock (AipGateOptions.now is None), so
    # the AIT must be minted with current timestamps (mirrors node's Math.floor(Date.now()/1000)).
    now_sec = int(time.time()) if now_sec is None else now_sec
    claims: dict[str, Any] = {
        "aip_version": "0.1",
        "sub": "user_x",
        "cnf": {"jwk": AGENT_PUBLIC_JWK},
        "agent": {"provider": "anthropic"},
        "trust_level": trust_level,
        "identity": identity if identity is not None else {"id_verified": True},
        "iss": iss,
        "iat": now_sec,
        "exp": now_sec + 300,
    }
    if auth is not None:
        claims["auth"] = auth
    return jwt.encode(
        {"alg": "EdDSA", "typ": "jwt", "kid": kid},
        claims,
        signing_key if signing_key is not None else _idp,
        algorithms=["EdDSA"],
    )


def _signed_headers(token: str, *, now_sec: int | None = None) -> dict[str, str]:
    now_sec = int(time.time()) if now_sec is None else now_sec
    sm = sign_message(
        method="POST",
        authority=AUTHORITY,
        path="/purchase",
        agent_identity=token,
        private_jwk=AGENT_PRIVATE_JWK,
        public_jwk=AGENT_PUBLIC_JWK,
        created=now_sec,
        # PoP verifier requires `expires` (replay-window hardening); 60s window like pay.
        expires=now_sec + 60,
    )
    return {
        "host": AUTHORITY,
        "agent-identity": token,
        "signature-input": sm.signature_input,
        "signature": sm.signature,
    }


# ── invalid cases: fail before any JWKS fetch / SDK call (no network needed) ──

AIP = AipGateConfig(trusted_issuers=[ISS, OURS])


class TestCheckoutAipInvalidCases:
    async def test_hard_denies_present_but_unsigned_ait_with_problem_json(self) -> None:
        # Agent-Identity present but NO Signature-Input/Signature → PoP missing → hard deny.
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", require_kyc=True, aip=AIP))
        res = await checkout.handle(_req({"agent-identity": "eyJhbGciOiJFZERTQSJ9.e30.sig"}))
        assert res.status == 401
        assert res.headers["content-type"] == "application/problem+json"
        assert res.body["type"] == "urn:aip:error:agent_identity_required"

    async def test_hard_denies_malformed_ait(self) -> None:
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", require_kyc=True, aip=AIP))
        res = await checkout.handle(
            _req(
                {
                    "agent-identity": "not-a-jwt",
                    "signature-input": (
                        'ait=("@method" "@authority" "@path" "agent-identity");keyid="x";tag="agent-identity"'
                    ),
                    "signature": "ait=:AAAA:",
                }
            )
        )
        # Malformed token → 401 invalid_signature class (never reaches the SDK assess call).
        assert res.status == 401
        assert res.headers["content-type"] == "application/problem+json"
        assert str(res.body["type"]).startswith("urn:aip:error:")

    async def test_hard_denies_invalid_ait_even_with_no_api_key(self) -> None:
        # A gate.aip merchant without api_key must still verify + hard-deny a present-but-invalid
        # AIT, not silently skip AIP via the wallet-OFAC-only fallback (pre-step runs first).
        checkout = _make_checkout(CheckoutGateConfig(api_key="", require_kyc=True, aip=AIP))
        res = await checkout.handle(_req({"agent-identity": "eyJhbGciOiJFZERTQSJ9.e30.sig"}))
        assert res.status == 401
        assert res.headers["content-type"] == "application/problem+json"
        assert res.body["type"] == "urn:aip:error:agent_identity_required"

    async def test_does_not_engage_aip_when_gate_aip_unset(self) -> None:
        # No aip config → the Agent-Identity header is ignored; the gate proceeds to the normal
        # wallet/operator assess path. That path requires CheckoutRequest.raw (the framework
        # request), which this bare request omits → RuntimeError. The point: an engaged AIP
        # pre-step would have returned an `application/problem+json` deny BEFORE reaching the raw
        # check, so raising here proves AIP was NOT engaged for the present Agent-Identity header.
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", require_kyc=True))
        with pytest.raises(RuntimeError, match=r"requires CheckoutRequest\.raw"):
            await checkout.handle(_req({"agent-identity": "eyJhbGciOiJFZERTQSJ9.e30.sig"}))


# ── offline (no api_key): a verified AIT cannot be policy-evaluated → fail closed; identity-only ok ──


def _offline_gate(**extra: Any) -> Checkout:
    co = _make_checkout(CheckoutGateConfig(api_key="", aip=AipGateConfig(trusted_issuers=[ISS]), **extra))
    co._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})
    return co


class TestCheckoutAipOffline:
    async def test_fails_closed_when_policy_bearing_gate_has_no_api_key(self) -> None:
        res = await _offline_gate(min_age=21).handle(_req(_signed_headers(_mint_ait(identity={"age_over_18": True}))))
        assert res.status == 403
        assert res.body["error"]["code"] == "aip_policy_requires_api_key"

    async def test_allows_valid_ait_on_identity_only_gate(self) -> None:
        # Identity-only gate (no policy fields) + verified AIT + no api_key → the gate returns
        # None (allow) and Checkout proceeds to x402 settle. The stub x-payload then fails
        # verification (400 verify_failed) — but reaching settle at all proves the gate did NOT
        # block with aip_policy_requires_api_key.
        res = await _offline_gate().handle(_req(_signed_headers(_mint_ait(identity={"id_verified": True}))))
        assert res.body.get("error", {}).get("code") != "aip_policy_requires_api_key"
        assert res.headers.get("content-type") != "application/problem+json"
        assert res.settle_phase == "verify_failed"

    async def test_denies_weak_auth_when_trust_level_below_requirement(self) -> None:
        gate = _make_checkout(
            CheckoutGateConfig(
                api_key="",
                aip=AipGateConfig(trusted_issuers=[ISS], require_trust_level="human_confirmed"),
            )
        )
        gate._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})
        res = await gate.handle(
            _req(_signed_headers(_mint_ait(identity={"id_verified": True}, trust_level="human_present")))
        )
        assert res.status == 403
        assert res.headers["content-type"] == "application/problem+json"
        assert res.body["type"] == "urn:aip:error:weak_auth"
        assert res.body["required_trust_level"] == "human_confirmed"

    async def test_denies_weak_auth_when_no_amr_matches(self) -> None:
        gate = _make_checkout(
            CheckoutGateConfig(
                api_key="",
                aip=AipGateConfig(trusted_issuers=[ISS], require_amr=["face", "fpt", "hwk"]),
            )
        )
        gate._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})
        res = await gate.handle(
            _req(
                _signed_headers(
                    _mint_ait(identity={"id_verified": True}, trust_level="human_confirmed", auth={"amr": ["pwd"]})
                )
            )
        )
        assert res.status == 403
        assert res.body["type"] == "urn:aip:error:weak_auth"
        assert res.body["required_amr"] == ["face", "fpt", "hwk"]

    async def test_passes_trust_gate_when_trust_level_and_amr_satisfy(self) -> None:
        gate = _make_checkout(
            CheckoutGateConfig(
                api_key="",
                aip=AipGateConfig(trusted_issuers=[ISS], require_trust_level="human_confirmed", require_amr=["face"]),
            )
        )
        gate._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})
        res = await gate.handle(
            _req(
                _signed_headers(
                    _mint_ait(identity={"id_verified": True}, trust_level="human_confirmed", auth={"amr": ["face"]})
                )
            )
        )
        # Trust gate passed (identity-only, no policy) → gate allows → reaches settle (verify_failed
        # on the stub), NOT a weak_auth problem+json.
        assert res.headers.get("content-type") != "application/problem+json"
        assert res.settle_phase == "verify_failed"


# ── issuer-conditional policy: full default for own issuer, relaxed override for a partner ──


@pytest.fixture
def _two_issuer_keys() -> dict[str, OKPKey]:
    return {ISS: _idp, OURS: OKPKey.import_key(Ed25519PrivateKey.generate())}


class TestCheckoutAipIssuerConditionalPolicy:
    """Uses the no-api_key path as a deterministic probe.

    A policy-bearing request that can't be evaluated FAILS CLOSED
    (``aip_policy_requires_api_key``); a request whose effective policy is EMPTY passes the gate
    (returns None → reaches x402 settle → ``verify_failed`` on the stub x-payload). So "fails
    closed" = policy was applied to this issuer; "verify_failed" = policy was empty.
    """

    def _gate(self, ours_key: OKPKey, issuer_policies: dict[str, AipIssuerPolicy]) -> Checkout:
        co = _make_checkout(
            CheckoutGateConfig(
                api_key="",
                require_kyc=True,
                require_sanctions_clear=True,
                min_age=21,
                allowed_jurisdictions=["US"],
                aip=AipGateConfig(trusted_issuers=[ISS, OURS], issuer_policies=issuer_policies),
            )
        )
        co._aip_jwks = _jwks_for(
            {
                ISS: IDP_PUBLIC_JWK,
                OURS: {**ours_key.as_dict(private=False), "kid": "as-key", "use": "sig", "alg": "EdDSA"},
            }
        )
        return co

    def _signed_from(self, iss: str, signing_key: OKPKey, kid: str, identity: dict[str, Any]) -> CheckoutRequest:
        token = _mint_ait(iss, kid=kid, signing_key=signing_key, identity=identity)
        return _req(_signed_headers(token))

    async def test_full_default_policy_on_own_issuer_fails_closed(self, _two_issuer_keys: dict[str, OKPKey]) -> None:
        gate = self._gate(_two_issuer_keys[OURS], {ISS: AipIssuerPolicy(require_kyc=True, min_age=21)})
        res = await gate.handle(
            self._signed_from(OURS, _two_issuer_keys[OURS], "as-key", {"id_verified": True, "age_over_21": True})
        )
        assert res.status == 403
        assert res.body["error"]["code"] == "aip_policy_requires_api_key"

    async def test_relaxed_override_still_policy_bearing_fails_closed(
        self, _two_issuer_keys: dict[str, OKPKey]
    ) -> None:
        # Relaxed ≠ empty: the partner still requires KYC + 21, so a no-api_key gate still can't
        # evaluate it and fails closed. Proves the override is policy-BEARING, not a bypass.
        gate = self._gate(_two_issuer_keys[OURS], {ISS: AipIssuerPolicy(require_kyc=True, min_age=21)})
        res = await gate.handle(self._signed_from(ISS, _idp, KID, {"id_verified": True, "age_over_21": True}))
        assert res.status == 403
        assert res.body["error"]["code"] == "aip_policy_requires_api_key"

    async def test_empty_issuer_override_drops_policy_and_allows(self, _two_issuer_keys: dict[str, OKPKey]) -> None:
        # A merchant can relax an issuer all the way to identity-only with `{}`. Then the effective
        # policy is empty → the no-api_key gate returns None (allow) → reaches settle.
        gate = self._gate(_two_issuer_keys[OURS], {ISS: AipIssuerPolicy()})
        res = await gate.handle(self._signed_from(ISS, _idp, KID, {"email_verified": True}))
        assert res.body.get("error", {}).get("code") != "aip_policy_requires_api_key"
        assert res.settle_phase == "verify_failed"

    async def test_matches_issuer_override_after_canonicalization(self, _two_issuer_keys: dict[str, OKPKey]) -> None:
        # Key has a trailing slash; verified iss is 'https://issuer.example' — must still match.
        gate = self._gate(_two_issuer_keys[OURS], {"https://issuer.example/": AipIssuerPolicy()})
        res = await gate.handle(self._signed_from(ISS, _idp, KID, {"email_verified": True}))
        assert res.body.get("error", {}).get("code") != "aip_policy_requires_api_key"
        assert res.settle_phase == "verify_failed"


# ── assess forwarding: aip_token + RFC 9421 signature material reach /v1/assess ──


class TestCheckoutAipForward:
    async def test_forwards_aip_token_and_signature_material_to_assess(self) -> None:
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", aip=AipGateConfig(trusted_issuers=[ISS])))
        checkout._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})

        captured: dict[str, Any] = {}

        async def fake_aassess(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "decision": "allow",
                "decision_reasons": ["no_policy_applied"],
                "identity_method": "aip_token",
            }

        # assess (mocked) returns allow → checkout proceeds toward x402 settle (verify_failed on the
        # stub). We only assert that assess was reached with the forwarded material.
        with patch("agentscore.AgentScore.aassess", new=AsyncMock(side_effect=fake_aassess)):
            await checkout.handle(_req(_signed_headers(_mint_ait(identity={"id_verified": True}))))

        assert isinstance(captured.get("aip_token"), str)
        sig = captured.get("aip_signature")
        assert isinstance(sig, dict)
        assert sig["method"] == "POST"
        assert sig["authority"] == AUTHORITY
        assert sig["path"] == "/purchase"
        assert isinstance(sig["signature_input"], str)
        assert isinstance(sig["signature"], str)

    async def test_assess_deny_maps_to_wallet_not_trusted_superset(self) -> None:
        # A verified AIT that /v1/assess DENIES on compliance (e.g. sanctions) emits the RFC 9457 +
        # AIP-spec SUPERSET (application/problem+json): BOTH the spec's
        # type=urn:aip:error:insufficient_claims / status 403 AND the rich AgentScore
        # `{ error.code, agent_instructions, reasons }` verbatim.
        checkout = _make_checkout(
            CheckoutGateConfig(
                api_key="as_test_key",
                require_sanctions_clear=True,
                min_age=21,
                aip=AipGateConfig(trusted_issuers=[ISS]),
            )
        )
        checkout._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})

        async def deny(**_kwargs: Any) -> dict[str, Any]:
            return {"decision": "deny", "decision_reasons": ["sanctions_flagged"]}

        with patch("agentscore.AgentScore.aassess", new=AsyncMock(side_effect=deny)):
            res = await checkout.handle(_req(_signed_headers(_mint_ait(identity={"id_verified": True}))))
        assert res.status == 403
        assert res.settle_phase == "gate_denied"
        assert res.headers["content-type"] == "application/problem+json"
        # RFC 9457 + AIP-spec envelope.
        assert res.body["type"] == "urn:aip:error:insufficient_claims"
        assert res.body["title"] == "insufficient claims"
        assert res.body["status"] == 403
        assert "sanctions_flagged" in res.body["detail"]
        # Escalation hint derived from the gate's effective policy.
        assert res.body["required_claims"] == ["sanctions_clear", "age_over_21"]
        # Rich AgentScore scheme preserved verbatim — still the agent's source of truth.
        assert res.body["error"]["code"] == "wallet_not_trusted"
        assert res.body["reasons"] == ["sanctions_flagged"]
        assert "agent_instructions" in res.body

    async def test_assess_deny_kyc_maps_required_claim_id_verified(self) -> None:
        # A kyc_required compliance deny is still insufficient_claims; required_claims = id_verified.
        checkout = _make_checkout(
            CheckoutGateConfig(api_key="as_test_key", require_kyc=True, aip=AipGateConfig(trusted_issuers=[ISS]))
        )
        checkout._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})

        async def deny(**_kwargs: Any) -> dict[str, Any]:
            return {"decision": "deny", "decision_reasons": ["kyc_required"]}

        with patch("agentscore.AgentScore.aassess", new=AsyncMock(side_effect=deny)):
            res = await checkout.handle(_req(_signed_headers(_mint_ait(identity={"id_verified": True}))))
        assert res.status == 403
        assert res.body["type"] == "urn:aip:error:insufficient_claims"
        assert res.body["required_claims"] == ["id_verified"]
        assert res.body["error"]["code"] == "wallet_not_trusted"

    async def test_assess_outage_fails_closed_with_api_error(self) -> None:
        # API outage / network failure on the AIT path → fail-closed 503 api_error (strict
        # liability: never allow an unverifiable settle).
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", aip=AipGateConfig(trusted_issuers=[ISS])))
        checkout._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})

        async def boom(**_kwargs: Any) -> dict[str, Any]:
            msg = "connection reset"
            raise RuntimeError(msg)

        with patch("agentscore.AgentScore.aassess", new=AsyncMock(side_effect=boom)):
            res = await checkout.handle(_req(_signed_headers(_mint_ait(identity={"id_verified": True}))))
        assert res.status == 503
        assert res.body["error"]["code"] == "api_error"

    async def test_on_denied_reshapes_the_ait_denial_body(self) -> None:
        # The gate's on_denied callback runs AFTER the canonical DenialReason is built on the AIT
        # path too (node parity), letting merchants reshape the denial body / status.
        captured_reason: dict[str, Any] = {}

        async def on_denied(_ctx: Any, reason: Any) -> dict[str, Any]:
            captured_reason["code"] = reason.code
            return {"status": 451, "body": {"custom": "blocked"}}

        checkout = _make_checkout(
            CheckoutGateConfig(
                api_key="as_test_key",
                require_sanctions_clear=True,
                aip=AipGateConfig(trusted_issuers=[ISS]),
                on_denied=on_denied,
            )
        )
        checkout._aip_jwks = _jwks_for({ISS: IDP_PUBLIC_JWK})

        async def deny(**_kwargs: Any) -> dict[str, Any]:
            return {"decision": "deny", "decision_reasons": ["sanctions_flagged"]}

        with patch("agentscore.AgentScore.aassess", new=AsyncMock(side_effect=deny)):
            res = await checkout.handle(_req(_signed_headers(_mint_ait(identity={"id_verified": True}))))
        assert captured_reason["code"] == "wallet_not_trusted"
        assert res.status == 451
        assert res.body == {"custom": "blocked"}


# ── framework renderer honors problem+json content-type (edge-deny + policy-deny) ──


def _starlette_request(headers: dict[str, str], *, body: dict[str, Any] | None = None) -> Any:
    """Build an app-bound-enough Starlette Request for handle_fastapi (mirrors signer-match tests)."""
    from starlette.requests import Request

    raw_headers = [(b"content-type", b"application/json")]
    raw_headers += [(k.encode(), v.encode()) for k, v in headers.items()]
    body_bytes = json.dumps(body if body is not None else {"product_id": "p1", "quantity": 1}).encode()
    received = False

    async def _receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/purchase",
        "raw_path": b"/purchase",
        "query_string": b"",
        "headers": raw_headers,
        "scheme": "https",
        "server": ("wine.example", 443),
        "client": ("127.0.0.1", 12345),
        "app": None,
    }
    return Request(scope, receive=_receive)


class TestCheckoutAipRendererHonorsProblemJson:
    """The framework renderers used to strip the content-type + force application/json, silently
    downgrading the AIP problem+json deny. They must now surface it (edge-deny here; policy-deny is
    covered through handle() in TestCheckoutAipForward)."""

    async def test_edge_deny_renders_problem_json_through_fastapi_adapter(self) -> None:
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", require_kyc=True, aip=AIP))
        request = _starlette_request(
            {"x-payment": "eyJzdHViIjogdHJ1ZX0=", "agent-identity": "eyJhbGciOiJFZERTQSJ9.e30.sig"}
        )
        resp = await checkout.handle_fastapi(request)
        assert resp.status_code == 401
        assert resp.media_type == "application/problem+json"
        body = json.loads(resp.body)
        assert body["type"] == "urn:aip:error:agent_identity_required"

    async def test_non_aip_denial_stays_application_json_through_fastapi_adapter(self) -> None:
        # The override is AIP-only: a missing-identity denial (no Agent-Identity header) through the
        # same renderer must stay on the application/json default, untouched.
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", require_kyc=True, aip=AIP))
        request = _starlette_request({"x-payment": "eyJzdHViIjogdHJ1ZX0="})
        resp = await checkout.handle_fastapi(request)
        assert resp.media_type == "application/json"
        body = json.loads(resp.body)
        assert "type" not in body


# ── EMITTED-body AIP advertisement: 402 challenge + missing-identity (the regression) ──


def _discovery_req() -> CheckoutRequest:
    """Anonymous-discovery leg: NO payment header → `handle` emits a 402 challenge."""
    return CheckoutRequest(
        method="POST",
        url=URL,
        headers={},
        body={"product_id": "p1", "quantity": 1},
    )


def _missing_identity_app(gate: Any) -> Any:
    """Mount an AgentScoreGate on a minimal FastAPI app via Depends — the same chain
    Checkout's missing-identity path drives (build_gate_from_policy → AgentScoreGate)."""
    from fastapi import Depends, FastAPI

    from agentscore_commerce.identity.fastapi import get_agentscore_data

    app = FastAPI()

    @app.get("/", dependencies=[Depends(gate)])
    async def index(_assess: Any = Depends(get_agentscore_data)) -> dict[str, Any]:
        return {"ok": True}

    return app


class TestCheckoutAipEmittedBodyAdvertisesAip:
    """Regression: the EMITTED 402 + missing-identity bodies must advertise AIP.

    The memory-hint builder (``build_agent_memory_hint``) is unit-tested in isolation in
    test_aip_agent_memory, but neither emit site was wired to pass ``aip_trusted_issuers`` —
    so the advertisement silently dropped on the wire. These exercise the real emit paths and
    assert ``agent_memory`` carries ``aip_trusted_issuers`` + the ``agent_identity`` path. Ports
    node-commerce ``tests/agent_memory_aip.test.ts`` to the orchestrator level.
    """

    async def test_emitted_402_body_advertises_aip_when_gate_accepts_it(self) -> None:
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", require_kyc=True, aip=AIP))
        res = await checkout.handle(_discovery_req())
        assert res.status == 402
        memory = res.body["agent_memory"]
        # AgentScore's canonical issuer is always trusted, so it shows up even with externals listed.
        assert AGENTSCORE_CANONICAL_ISSUER in memory["aip_trusted_issuers"]
        assert ISS in memory["aip_trusted_issuers"]
        assert "Agent-Identity" in memory["identity_paths"]["agent_identity"]
        assert "RFC 9421" in memory["identity_paths"]["agent_identity"]
        # AIP is additive — the wallet + operator_token paths remain present.
        assert memory["identity_paths"]["wallet"]
        assert memory["identity_paths"]["operator_token"]

    async def test_emitted_402_body_omits_aip_when_gate_has_no_aip(self) -> None:
        # Same policy-bearing gate (so agent_memory is emitted) but no `aip` block → no AIP guidance.
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", require_kyc=True))
        res = await checkout.handle(_discovery_req())
        assert res.status == 402
        memory = res.body["agent_memory"]
        # The dataclass always serializes the field; opting out means it's null, not a list.
        assert memory["aip_trusted_issuers"] is None
        assert "agent_identity" not in memory["identity_paths"]

    def test_emitted_missing_identity_body_advertises_aip(self) -> None:
        from fastapi.testclient import TestClient

        from agentscore_commerce.identity.policy import build_gate_from_policy

        # build_gate_from_policy is exactly how Checkout's missing-identity path constructs the gate.
        gate = build_gate_from_policy(
            {"require_kyc": True, "enforcement": "hard"},
            api_key="as_test_key",
            aip_trusted_issuers=build_aip_trusted_issuers([ISS]),
        )
        assert gate is not None
        client = TestClient(_missing_identity_app(gate))
        # No identity header → bare missing_identity denial (no create_session_on_missing configured).
        resp = client.get("/")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "missing_identity"
        memory = body["agent_memory"]
        assert AGENTSCORE_CANONICAL_ISSUER in memory["aip_trusted_issuers"]
        assert ISS in memory["aip_trusted_issuers"]
        assert "Agent-Identity" in memory["identity_paths"]["agent_identity"]
        assert "RFC 9421" in memory["identity_paths"]["agent_identity"]

    def test_emitted_missing_identity_body_omits_aip_when_not_configured(self) -> None:
        from fastapi.testclient import TestClient

        from agentscore_commerce.identity.policy import build_gate_from_policy

        gate = build_gate_from_policy(
            {"require_kyc": True, "enforcement": "hard"},
            api_key="as_test_key",
        )
        assert gate is not None
        client = TestClient(_missing_identity_app(gate))
        resp = client.get("/")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "missing_identity"
        memory = body["agent_memory"]
        # The dataclass always serializes the field; opting out means it's null, not a list.
        assert memory["aip_trusted_issuers"] is None
        assert "agent_identity" not in memory["identity_paths"]


class TestCheckoutPerRequestPolicyEnforcement:
    """Regression: Checkout's per-request-policy gate must FIRE, not be silently bypassed.

    ``_run_gate`` previously popped ``enforcement`` out of the merged policy before calling
    ``build_gate_from_policy`` — which keys off ``enforcement`` to decide whether to build a
    gate at all — so the gate came back ``None`` and a settle-leg request bypassed compliance
    and proceeded to settle. (Not AIP-specific; surfaced during the AIP parity audit.) A settle
    leg with no resolvable identity must now be DENIED with ``missing_identity``.
    """

    async def test_per_request_policy_gate_is_built_not_bypassed(self) -> None:
        gate = CheckoutGateConfig(
            api_key="as_test_key",
            per_request_policy=lambda _ctx: {"require_kyc": True, "enforcement": "hard"},
        )
        checkout = _make_checkout(gate)
        # Proof the gate now FIRES: once built, the framework-agnostic handle() demands the
        # native request object the per-request FastAPI gate needs. Under the bug (enforcement
        # popped -> build_gate_from_policy returns None) there was no gate, no such demand, and
        # the settle leg proceeded — silently bypassing compliance. (The built gate's actual
        # missing-identity denial is covered by TestCheckoutAipEmittedBodyAdvertisesAip.)
        with pytest.raises(RuntimeError, match="requires CheckoutRequest"):
            await checkout.handle(_req({}))


class TestCheckoutStaticGateEnforcement:
    """Regression (BUG 1): a STATIC-policy ``Checkout(gate=...)`` (no per_request_policy) must
    FIRE the gate, not silently bypass ALL compliance.

    ``_run_gate`` builds ``merged_policy`` from the static gate fields (require_kyc / sanctions /
    min_age / jurisdictions) but those fields NEVER include an ``enforcement`` key — that only
    ever comes from a ``per_request_policy`` hook. So ``enforcement`` resolved to ``None`` →
    ``build_gate_from_policy`` returned ``None`` (no enforcement => no gate) →
    ``run_gate_with_enforcement(None, None)`` short-circuited to status="anonymous" (ALLOW),
    silently bypassing KYC / age / jurisdiction / sanctions. The shipped
    ``examples/compliance_merchant.py`` is exactly this static-gate shape. node-commerce builds
    the core + calls ``evaluate`` whenever policy fields are present (no enforcement abstraction);
    the fix restores that always-fire behavior by defaulting ``enforcement="hard"`` for the
    static-gate path.
    """

    async def test_static_gate_defaults_enforcement_hard_and_builds_the_gate(self) -> None:
        # Capture exactly what _run_gate hands run_gate_with_enforcement. Under the bug this is
        # (gate=None, enforcement=None) -> anonymous allow; after the fix it's (real gate,
        # enforcement="hard") -> the gate runs. Patching the symbol the local import binds.
        import agentscore_commerce.identity.policy as policy_mod
        from agentscore_commerce.identity.policy import GateResult

        captured: dict[str, Any] = {}

        async def _fake_run(_request: Any, gate: Any, *, enforcement: Any) -> GateResult:
            captured["gate_is_none"] = gate is None
            captured["enforcement"] = enforcement
            return GateResult(status="verified")

        gate = CheckoutGateConfig(api_key="as_test_key", require_kyc=True, min_age=21)
        checkout = _make_checkout(gate)
        # Non-None raw sentinel so we reach run_gate_with_enforcement (skips the raw-None guard);
        # the fake stands in for the FastAPI gate so no app/network is needed.
        req = CheckoutRequest(
            method="POST",
            url=URL,
            headers={"x-payment": "eyJzdHViIjogdHJ1ZX0="},
            body={"product_id": "p1", "quantity": 1},
            raw=object(),
        )
        with patch.object(policy_mod, "run_gate_with_enforcement", _fake_run):
            await checkout.handle(req)
        assert captured["enforcement"] == "hard"
        assert captured["gate_is_none"] is False

    async def test_static_gate_engages_the_gate_machinery(self) -> None:
        # Mirrors TestCheckoutPerRequestPolicyEnforcement: a static require_kyc gate now reaches
        # the gate-build path, which (with no native request) demands CheckoutRequest.raw. Under
        # the bypass bug this static config short-circuited to an anonymous allow at settle.
        checkout = _make_checkout(CheckoutGateConfig(api_key="as_test_key", require_kyc=True))
        with pytest.raises(RuntimeError, match="requires CheckoutRequest"):
            await checkout.handle(_req({}))
