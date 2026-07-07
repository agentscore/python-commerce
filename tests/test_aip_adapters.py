"""AIP gate wiring across the framework adapters.

Ports node-commerce ``tests/aip_adapters.test.ts``. Node exercises express / fastify / web /
nextjs plus the ``verifyAitParts`` (Node header-map) entry point and ``buildVerifyContextFromParts``.
The Python adapters with an AIP gate are: the ASGI middleware (Starlette / FastAPI via
``add_middleware``), the FastAPI ``AipGate`` / ``ConditionalAipGate`` dependencies, and the aiohttp
``aip_gate_middleware``. Each is exercised end-to-end with a real signed AIT, and the parts-based
entry points (``verify_ait_parts`` / ``build_verify_context_from_parts``) are tested directly — the
same coverage shape as the node suite.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from joserfc import jwt
from joserfc.jwk import OKPKey

from agentscore_commerce.aip import (
    AipGateOptions,
    JwksCache,
    build_verify_context_from_parts,
    sign_message,
    verify_ait_parts,
)

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

ISS = "https://issuer.example"
KID = "partner-key-2026-05"
AUTHORITY = "wine-merchant.com"
PATH = "/checkout"
NOW = 1715400020

_idp = OKPKey.import_key(Ed25519PrivateKey.generate())
IDP_PUBLIC_JWK = {**_idp.as_dict(private=False), "kid": KID, "use": "sig", "alg": "EdDSA"}
_agent = OKPKey.import_key(Ed25519PrivateKey.generate())
AGENT_PRIVATE_JWK = _agent.as_dict(private=True)
AGENT_PUBLIC_JWK = _agent.as_dict(private=False)


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


def _make_jwks() -> JwksCache:
    async def fetch(url: str, headers: dict) -> _Resp:
        return _Resp()

    return JwksCache(trusted_issuers=[ISS], fetch_impl=fetch)


def mint_ait() -> str:
    return jwt.encode(
        {"alg": "EdDSA", "typ": "jwt", "kid": KID},
        {
            "aip_version": "0.1",
            "sub": "user_abc",
            "cnf": {"jwk": AGENT_PUBLIC_JWK},
            "agent": {"provider": "anthropic"},
            "trust_level": "human_present",
            "identity": {"email": "b@example.com", "email_verified": True},
            "iss": ISS,
            "iat": 1715400000,
            "exp": 1715400300,
        },
        _idp,
        algorithms=["EdDSA"],
    )


def sig_headers(token: str) -> dict[str, str]:
    sm = sign_message(
        method="POST",
        authority=AUTHORITY,
        path=PATH,
        agent_identity=token,
        private_jwk=AGENT_PRIVATE_JWK,
        public_jwk=AGENT_PUBLIC_JWK,
        created=1715400010,
        # PoP verifier requires `expires` (replay-window hardening); 60s window like pay.
        expires=1715400070,
    )
    return {
        "host": AUTHORITY,
        "agent-identity": token,
        "signature-input": sm.signature_input,
        "signature": sm.signature,
    }


# ── build_verify_context_from_parts ──


class TestBuildVerifyContextFromParts:
    def test_derives_path_plus_authority_from_a_bare_url_and_host_header(self) -> None:
        headers = sig_headers(mint_ait())
        ctx = build_verify_context_from_parts({"method": "POST", "url": "/checkout?x=1", "headers": headers})
        assert ctx.method == "POST"
        assert ctx.path == "/checkout"
        assert ctx.authority == AUTHORITY
        assert len(ctx.agent_identity_headers) == 1

    def test_handles_a_header_value_list(self) -> None:
        ctx = build_verify_context_from_parts(
            {
                "method": "POST",
                "url": "/x",
                "headers": {"agent-identity": ["aaa.bbb.ccc", "ddd.eee.fff"], "host": AUTHORITY},
            }
        )
        assert ctx.agent_identity_headers == ["aaa.bbb.ccc", "ddd.eee.fff"]


# ── verify_ait_parts ──


class TestVerifyAitParts:
    async def test_verifies_a_valid_signed_ait_from_a_header_map(self) -> None:
        headers = sig_headers(mint_ait())
        r = await verify_ait_parts(
            {"method": "POST", "url": PATH, "headers": headers},
            AipGateOptions(jwks=_make_jwks(), now=NOW),
        )
        assert r.ok is True
        assert r.ait is not None and r.ait.payload["identity"]["email"] == "b@example.com"

    async def test_fails_with_no_token_when_the_header_is_absent(self) -> None:
        r = await verify_ait_parts(
            {"method": "POST", "url": PATH, "headers": {"host": AUTHORITY}},
            AipGateOptions(jwks=_make_jwks(), now=NOW),
        )
        assert (r.ok, r.failure) == (False, "no_token")


# ── Starlette / FastAPI ASGI middleware (AipGate) ──


class TestAsgiAipGateMiddleware:
    @staticmethod
    def _app():
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        from agentscore_commerce.identity.middleware import AipGate, get_verified_ait

        async def checkout(request):  # type: ignore[no-untyped-def]
            ait = get_verified_ait(request)
            return JSONResponse({"email": ait.payload["identity"]["email"] if ait else None})

        app = Starlette(routes=[Route("/checkout", checkout, methods=["POST"])])
        app.add_middleware(AipGate, jwks=_make_jwks(), now=NOW)
        return app

    def test_allows_a_valid_ait_and_exposes_claims(self) -> None:
        from starlette.testclient import TestClient

        client = TestClient(self._app())
        res = client.post("/checkout", headers=sig_headers(mint_ait()))
        assert res.status_code == 200
        assert res.json() == {"email": "b@example.com"}

    def test_denies_with_401_problem_json_when_no_ait(self) -> None:
        from starlette.testclient import TestClient

        client = TestClient(self._app())
        res = client.post("/checkout", headers={"host": AUTHORITY})
        assert res.status_code == 401
        assert res.headers["content-type"].startswith("application/problem+json")
        assert res.json()["type"] == "urn:aip:error:agent_identity_required"

    def test_denies_with_403_for_an_untrusted_issuer(self) -> None:
        from starlette.testclient import TestClient

        evil = jwt.encode(
            {"alg": "EdDSA", "typ": "jwt", "kid": KID},
            {
                "aip_version": "0.1",
                "sub": "u",
                "cnf": {"jwk": AGENT_PUBLIC_JWK},
                "agent": {"provider": "x"},
                "iss": "https://evil.com",
                "iat": 1715400000,
                "exp": 1715400300,
            },
            _idp,
            algorithms=["EdDSA"],
        )
        client = TestClient(self._app())
        res = client.post("/checkout", headers=sig_headers(evil))
        assert res.status_code == 403
        assert res.json()["type"] == "urn:aip:error:untrusted_issuer"


# ── FastAPI dependency gate (AipGate / get_verified_ait) ──


class TestFastapiAipGateDependency:
    @staticmethod
    def _app():
        from fastapi import Depends, FastAPI

        from agentscore_commerce.identity.fastapi import AipGate, get_verified_ait

        gate = AipGate(jwks=_make_jwks(), now=NOW)
        app = FastAPI()

        @app.post("/checkout", dependencies=[Depends(gate)])
        async def checkout(ait=Depends(get_verified_ait)):  # type: ignore[no-untyped-def]
            return {"email": ait.payload["identity"]["email"] if ait else None}

        return app

    def test_allows_a_valid_ait_and_denies_a_missing_one(self) -> None:
        from starlette.testclient import TestClient

        client = TestClient(self._app())
        ok = client.post("/checkout", headers=sig_headers(mint_ait()))
        assert ok.status_code == 200
        assert ok.json() == {"email": "b@example.com"}

        denied = client.post("/checkout", headers={"host": AUTHORITY})
        assert denied.status_code == 401
        # FLAT application/problem+json document — body["type"], not nested under "detail".
        assert denied.headers["content-type"].startswith("application/problem+json")
        assert denied.json()["type"] == "urn:aip:error:agent_identity_required"


# ── FastAPI conditional gate (flows through when no Agent-Identity header) ──


class TestFastapiConditionalAipGate:
    def test_flows_through_when_no_agent_identity_header(self) -> None:
        from fastapi import Depends, FastAPI
        from starlette.testclient import TestClient

        from agentscore_commerce.identity.fastapi import ConditionalAipGate, get_verified_ait

        gate = ConditionalAipGate(jwks=_make_jwks(), now=NOW)
        app = FastAPI()

        @app.post("/checkout", dependencies=[Depends(gate)])
        async def checkout(ait=Depends(get_verified_ait)):  # type: ignore[no-untyped-def]
            return {"has_ait": ait is not None}

        client = TestClient(app)
        res = client.post("/checkout")
        assert res.status_code == 200
        assert res.json() == {"has_ait": False}


# ── aiohttp middleware (aip_gate_middleware) ──


class TestAiohttpAipGateMiddleware:
    async def test_allows_a_valid_ait_and_denies_a_missing_one(self) -> None:
        from aiohttp import web
        from aiohttp.test_utils import TestClient as AioTestClient
        from aiohttp.test_utils import TestServer

        from agentscore_commerce.identity.aiohttp import aip_gate_middleware, get_verified_ait

        async def checkout(request: web.Request) -> web.Response:
            ait = get_verified_ait(request)
            return web.json_response({"email": ait.payload["identity"]["email"] if ait else None})

        app = web.Application()
        app.middlewares.append(aip_gate_middleware(jwks=_make_jwks(), now=NOW))
        app.router.add_post("/checkout", checkout)

        client = AioTestClient(TestServer(app))
        await client.start_server()
        try:
            ok = await client.post("/checkout", headers=sig_headers(mint_ait()))
            assert ok.status == 200
            assert await ok.json() == {"email": "b@example.com"}

            denied = await client.post("/checkout", headers={"host": AUTHORITY})
            assert denied.status == 401
            body = await denied.json()
            assert body["type"] == "urn:aip:error:agent_identity_required"
        finally:
            await client.close()


_ = warnings
