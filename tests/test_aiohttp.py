"""Tests for the AIOHTTP adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agentscore_commerce.identity.aiohttp import agentscore_gate_middleware, capture_wallet
from agentscore_commerce.identity.sessions import CreateSessionOnMissing

ASSESS_URL = "https://api.agentscore.sh/v1/assess"
SESSIONS_URL = "https://api.agentscore.sh/v1/sessions"
CAPTURE_URL = "https://api.agentscore.sh/v1/credentials/wallets"


async def _ok_handler(request: web.Request) -> web.Response:
    agentscore = request.get("agentscore")
    return web.json_response({"ok": True, "agentscore": agentscore})


async def _capture_handler(request: web.Request) -> web.Response:
    await capture_wallet(request, "0xsigner", "evm", idempotency_key="pi_abc")
    return web.json_response({"ok": True})


def _make_app(handler=_ok_handler, route: str = "/", **gate_kwargs) -> web.Application:
    app = web.Application()
    app.middlewares.append(agentscore_gate_middleware(api_key="ask_test", **gate_kwargs))
    app.router.add_get(route, handler)
    app.router.add_post(route, handler)
    return app


def _mock_assess(decision: str = "allow", reasons: list[str] | None = None) -> respx.Route:
    return respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(200, json={"decision": decision, "decision_reasons": reasons or []}),
    )


async def _client(app: web.Application) -> TestClient:
    server = TestServer(app)
    return TestClient(server)


class TestIdentityExtraction:
    @pytest.mark.asyncio
    @respx.mock
    async def test_allows_trusted_wallet(self):
        _mock_assess("allow")
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_denies_untrusted_wallet(self):
        _mock_assess("deny", reasons=["not_kyc"])
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 403
            data = await resp.json()
            assert data["error"] == "wallet_not_trusted"
            assert data["reasons"] == ["not_kyc"]

    @pytest.mark.asyncio
    async def test_missing_identity_returns_403(self):
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/")
            assert resp.status == 403
            data = await resp.json()
            assert data["error"] == "missing_identity"

    @pytest.mark.asyncio
    async def test_fail_open_allows_through_when_identity_missing(self):
        client = await _client(_make_app(fail_open=True))
        async with client:
            resp = await client.get("/")
            assert resp.status == 200

    @pytest.mark.asyncio
    @respx.mock
    async def test_passes_operator_token_to_assess(self):
        route = _mock_assess("allow")
        client = await _client(_make_app())
        async with client:
            await client.get("/", headers={"X-Operator-Token": "opc_abc"})
            body = json.loads(route.calls[0].request.content)
            assert body.get("operator_token") == "opc_abc"


class TestErrorPaths:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_403_payment_required_on_402(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(402))
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 403
            data = await resp.json()
            assert data["error"] == "payment_required"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_503_api_error_on_500(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500, text="oops"))
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 503
            data = await resp.json()
            assert data["error"] == "api_error"

    @pytest.mark.asyncio
    @respx.mock
    async def test_fail_open_allows_through_on_402(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(402))
        client = await _client(_make_app(fail_open=True))
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 200

    @pytest.mark.asyncio
    @respx.mock
    async def test_fail_open_allows_through_on_api_error(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500))
        client = await _client(_make_app(fail_open=True))
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 200


class TestChainOption:
    @pytest.mark.asyncio
    @respx.mock
    async def test_constructor_chain_forwarded_to_assess(self):
        route = _mock_assess("allow")
        client = await _client(_make_app(chain="solana"))
        async with client:
            await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            body = json.loads(route.calls[0].request.content)
            assert body["chain"] == "solana"

    @pytest.mark.asyncio
    @respx.mock
    async def test_extract_chain_overrides_constructor_chain(self):
        route = _mock_assess("allow")
        app = _make_app(
            chain="base",
            extract_chain=lambda _req: "ethereum",
        )
        client = await _client(app)
        async with client:
            await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            body = json.loads(route.calls[0].request.content)
            assert body["chain"] == "ethereum"

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_chain_sent_when_neither_configured(self):
        route = _mock_assess("allow")
        client = await _client(_make_app())
        async with client:
            await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            body = json.loads(route.calls[0].request.content)
            assert "chain" not in body


class TestCreateSessionOnMissing:
    @pytest.mark.asyncio
    @respx.mock
    async def test_creates_session_and_returns_403_with_session_data(self):
        respx.post(SESSIONS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "session_id": "sess_abc123",
                    "verify_url": "https://agentscore.sh/verify/sess_abc123",
                    "poll_secret": "ps_secret",
                    "next_steps": {
                        "action": "deliver_verify_url_and_poll",
                        "user_message": "please verify",
                    },
                },
            )
        )

        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        client = await _client(app)
        async with client:
            resp = await client.get("/")
            assert resp.status == 403
            data = await resp.json()
            assert data["error"] == "identity_verification_required"
            assert data["session_id"] == "sess_abc123"
            assert data["verify_url"] == "https://agentscore.sh/verify/sess_abc123"
            assert data["poll_secret"] == "ps_secret"
            import json as _json

            parsed = _json.loads(data["agent_instructions"])
            assert parsed["action"] == "deliver_verify_url_and_poll"
            assert parsed["user_message"] == "please verify"

    @pytest.mark.asyncio
    @respx.mock
    async def test_falls_back_to_missing_identity_on_session_api_failure(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(500, text="oops"))
        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        client = await _client(app)
        async with client:
            resp = await client.get("/")
            assert resp.status == 403
            data = await resp.json()
            assert data["error"] == "missing_identity"


class TestCaptureWallet:
    @pytest.mark.asyncio
    @respx.mock
    async def test_captures_when_operator_token_present(self):
        _mock_assess("allow")
        capture_route = respx.post(CAPTURE_URL).mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )

        app = _make_app(_capture_handler)
        client = await _client(app)
        async with client:
            resp = await client.post("/", headers={"X-Operator-Token": "opc_abc"})
            assert resp.status == 200
        assert capture_route.called
        body = json.loads(capture_route.calls[0].request.content)
        assert body == {
            "operator_token": "opc_abc",
            "wallet_address": "0xsigner",
            "network": "evm",
            "idempotency_key": "pi_abc",
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_ops_when_wallet_authenticated(self):
        _mock_assess("allow")
        capture_route = respx.post(CAPTURE_URL).mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )

        app = _make_app(_capture_handler)
        client = await _client(app)
        async with client:
            resp = await client.post("/", headers={"X-Wallet-Address": "0xwallet"})
            assert resp.status == 200
        assert capture_route.call_count == 0

    @pytest.mark.asyncio
    async def test_no_ops_when_gate_did_not_run(self):
        # Handler wired without the gate middleware — capture_wallet must silently no-op.
        app = web.Application()
        app.router.add_post("/", _capture_handler)
        with patch("agentscore_commerce.identity.client.GateClient.acapture_wallet", new=AsyncMock()) as mock_cap:
            client = await _client(app)
            async with client:
                resp = await client.post("/")
                assert resp.status == 200
            mock_cap.assert_not_called()


class TestUserAgent:
    @pytest.mark.asyncio
    @respx.mock
    async def test_default_user_agent_format(self):
        route = _mock_assess("allow")
        client = await _client(_make_app())
        async with client:
            await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            ua = route.calls[0].request.headers["User-Agent"]
            assert ua.startswith("agentscore-commerce/")

    @pytest.mark.asyncio
    @respx.mock
    async def test_custom_user_agent_prepended(self):
        route = _mock_assess("allow")
        client = await _client(_make_app(user_agent="myapp/2.0"))
        async with client:
            await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            ua = route.calls[0].request.headers["User-Agent"]
            assert ua.startswith("myapp/2.0 (agentscore-commerce/")


@pytest.mark.asyncio
@respx.mock
async def test_aiohttp_passes_through_token_expired():
    respx.post("https://api.agentscore.sh/v1/assess").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {"code": "token_expired", "message": "expired"},
                "next_steps": {"action": "deliver_verify_url_and_poll"},
            },
        )
    )
    app = web.Application(
        middlewares=[agentscore_gate_middleware(api_key="ak", fail_open=False)],
    )

    async def handler(_req):
        return web.json_response({"ok": True})

    app.router.add_get("/", handler)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/", headers={"x-operator-token": "opc_exp"})
        assert resp.status == 401
        body = await resp.json()
        assert body["error"] == "token_expired"
        assert json.loads(body["agent_instructions"]) == {"action": "deliver_verify_url_and_poll"}


@pytest.mark.asyncio
@respx.mock
async def test_aiohttp_api_error_on_unexpected_exception():
    respx.post("https://api.agentscore.sh/v1/assess").mock(
        side_effect=httpx.ConnectError("dns down"),
    )
    app = web.Application(
        middlewares=[agentscore_gate_middleware(api_key="ak", fail_open=False)],
    )

    async def handler(_req):
        return web.json_response({"ok": True})

    app.router.add_get("/", handler)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/", headers={"x-wallet-address": "0xabc"})
        assert resp.status == 503
        body = await resp.json()
        assert body["error"] == "api_error"
