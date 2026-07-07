"""Tests for the AIOHTTP adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agentscore_commerce.identity.aiohttp import (
    GATE_STATE_KEY,
    agentscore_gate_middleware,
    capture_wallet,
    get_agentscore_data,
)
from agentscore_commerce.identity.sessions import CreateSessionOnMissing

ASSESS_URL = "https://api.agentscore.com/v1/assess"
SESSIONS_URL = "https://api.agentscore.com/v1/sessions"
CAPTURE_URL = "https://api.agentscore.com/v1/credentials/wallets"


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
    async def test_get_agentscore_data_returns_assess_after_pass(self):
        _mock_assess("allow")

        async def handler(request: web.Request) -> web.Response:
            return web.json_response({"assess": get_agentscore_data(request)})

        client = await _client(_make_app(handler=handler))
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 200
            data = await resp.json()
            assert data["assess"] == {"decision": "allow", "decision_reasons": []}

    @pytest.mark.asyncio
    @respx.mock
    async def test_denies_untrusted_wallet(self):
        _mock_assess("deny", reasons=["kyc_required"])
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 403
            data = await resp.json()
            assert data["error"]["code"] == "wallet_not_trusted"
            assert data["reasons"] == ["kyc_required"]

    @pytest.mark.asyncio
    async def test_missing_identity_returns_403(self):
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/")
            assert resp.status == 403
            data = await resp.json()
            assert data["error"]["code"] == "missing_identity"

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
            assert data["error"]["code"] == "payment_required"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_503_api_error_on_500(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500, text="oops"))
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 503
            data = await resp.json()
            assert data["error"]["code"] == "api_error"

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

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_gate_degraded_state_returns_default_for_normal_allow(self):
        from agentscore_commerce.identity.aiohttp import get_gate_degraded_state

        _mock_assess("allow")

        captured: dict = {}

        async def _snoop(request: web.Request) -> web.Response:
            captured.update(get_gate_degraded_state(request))
            return web.json_response({"ok": True})

        client = await _client(_make_app(handler=_snoop))
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 200
            assert captured == {"degraded": False}

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_gate_degraded_state_returns_infra_reason_when_degraded(self):
        from agentscore_commerce.identity.aiohttp import get_gate_degraded_state

        respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))

        captured: dict = {}

        async def _snoop(request: web.Request) -> web.Response:
            captured.update(get_gate_degraded_state(request))
            return web.json_response({"ok": True})

        client = await _client(_make_app(handler=_snoop, fail_open=True))
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 200
            assert captured == {"degraded": True, "infra_reason": "quota_exceeded"}

    @pytest.mark.asyncio
    @respx.mock
    async def test_quota_exceeded_returns_503_when_fail_closed(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))
        client = await _client(_make_app())
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 503
            body = await resp.json()
            assert body["error"]["code"] == "api_error"
            instructions = json.loads(body["agent_instructions"])
            assert instructions["action"] == "contact_merchant"
            assert "merchant-side issue" in instructions["steps"][0]

    @pytest.mark.asyncio
    @respx.mock
    async def test_quota_exceeded_marks_degraded_when_fail_open(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))

        async def _snoop(request: web.Request) -> web.Response:
            state = request.get(GATE_STATE_KEY) or {}
            return web.json_response({k: v for k, v in state.items() if k != "client"})

        client = await _client(_make_app(handler=_snoop, fail_open=True))
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 200
            data = await resp.json()
            assert data.get("degraded") is True
            assert data.get("infra_reason") == "quota_exceeded"

    @pytest.mark.asyncio
    async def test_timeout_marks_degraded_when_fail_open(self):
        async def _snoop(request: web.Request) -> web.Response:
            state = request.get(GATE_STATE_KEY) or {}
            return web.json_response({k: v for k, v in state.items() if k != "client"})

        with patch(
            "agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity",
            side_effect=httpx.TimeoutException("read timeout"),
        ):
            client = await _client(_make_app(handler=_snoop, fail_open=True))
            async with client:
                resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
                assert resp.status == 200
                data = await resp.json()
                assert data.get("degraded") is True
                assert data.get("infra_reason") == "network_timeout"

    @pytest.mark.asyncio
    async def test_generic_exception_marks_degraded_when_fail_open(self):
        async def _snoop(request: web.Request) -> web.Response:
            state = request.get(GATE_STATE_KEY) or {}
            return web.json_response({k: v for k, v in state.items() if k != "client"})

        with patch(
            "agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity",
            side_effect=RuntimeError("oops"),
        ):
            client = await _client(_make_app(handler=_snoop, fail_open=True))
            async with client:
                resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
                assert resp.status == 200
                data = await resp.json()
                assert data.get("degraded") is True
                assert data.get("infra_reason") == "api_error"


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
                    "verify_url": "https://www.agentscore.com/verify/sess_abc123",
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
            assert data["error"]["code"] == "identity_verification_required"
            assert data["session_id"] == "sess_abc123"
            assert data["verify_url"] == "https://www.agentscore.com/verify/sess_abc123"
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
            assert data["error"]["code"] == "missing_identity"

    @pytest.mark.asyncio
    @respx.mock
    async def test_fixable_wallet_denial_bootstraps_session(self):
        _mock_assess("deny", reasons=["kyc_required"])
        respx.post(SESSIONS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "session_id": "sess_kyc",
                    "verify_url": "https://www.agentscore.com/verify/sess_kyc",
                    "poll_secret": "ps_kyc",
                    "next_steps": {"action": "deliver_verify_url_and_poll"},
                },
            )
        )
        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        client = await _client(app)
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 403
            data = await resp.json()
            assert data["error"]["code"] == "identity_verification_required"
            assert data["session_id"] == "sess_kyc"

    @pytest.mark.asyncio
    @respx.mock
    async def test_unfixable_wallet_denial_returns_bare_wallet_not_trusted(self):
        _mock_assess("deny", reasons=["sanctions_flagged"])
        sessions_route = respx.post(SESSIONS_URL)
        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        client = await _client(app)
        async with client:
            resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status == 403
            data = await resp.json()
            assert data["error"]["code"] == "wallet_not_trusted"
            assert sessions_route.call_count == 0


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
        with patch("agentscore_commerce.identity.core.AgentScoreCore.acapture_wallet", new=AsyncMock()) as mock_cap:
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
    respx.post("https://api.agentscore.com/v1/assess").mock(
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
        assert body["error"]["code"] == "token_expired"
        assert json.loads(body["agent_instructions"]) == {"action": "deliver_verify_url_and_poll"}


@pytest.mark.asyncio
@respx.mock
async def test_aiohttp_api_error_on_unexpected_exception():
    respx.post("https://api.agentscore.com/v1/assess").mock(
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
        assert body["error"]["code"] == "api_error"


@respx.mock
async def test_aiohttp_handler_exception_is_not_swallowed_by_gate():
    """Regression: gate's try-block must NOT wrap downstream handler. If the user's
    handler raises, the exception must propagate up — NOT be misclassified as an
    AgentScore infra failure (which under fail_open would re-invoke the handler)."""
    respx.post("https://api.agentscore.com/v1/assess").mock(
        return_value=httpx.Response(200, json={"decision": "allow", "decision_reasons": []}),
    )
    invocations = {"count": 0}

    async def boom_handler(_req):
        invocations["count"] += 1
        msg = "downstream handler failure"
        raise RuntimeError(msg)

    app = web.Application(
        middlewares=[agentscore_gate_middleware(api_key="ak", fail_open=True)],
    )
    app.router.add_get("/", boom_handler)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/", headers={"x-wallet-address": "0xabc"})
        # aiohttp surfaces an unhandled exception as 500 — the important thing is the
        # handler ran exactly once (no fail-open retry), and the gate didn't claim
        # the exception was an AgentScore infra failure.
        assert resp.status == 500
    assert invocations["count"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_aiohttp_propagates_quota_from_assess_response_headers() -> None:
    """API X-Quota-* headers → SDK populates AssessResponse.quota → adapter stashes it."""
    from agentscore_commerce.identity.aiohttp import get_gate_quota_info

    respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"X-Quota-Limit": "1500", "X-Quota-Used": "1200", "X-Quota-Reset": "2026-06-01T00:00:00Z"},
            json={"decision": "allow", "decision_reasons": []},
        ),
    )

    captured: dict = {}

    async def handler(request):
        captured["quota"] = get_gate_quota_info(request)
        return web.json_response({"ok": True})

    app = _make_app(handler=handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/", headers={"x-wallet-address": "0xabc"})
        assert resp.status == 200
    assert captured["quota"] is not None
    assert captured["quota"].limit == 1500
    assert captured["quota"].used == 1200


class TestAiohttpBranchGaps:
    """Covers the remaining shared adapter branches for the aiohttp gate."""

    @pytest.mark.asyncio
    async def test_on_denied_three_tuple_sets_headers(self):
        def custom(_req, reason):
            return ({"code": reason.code}, 418, {"X-Custom": "teapot"})

        client = await _client(_make_app(on_denied=custom))
        async with client:
            resp = await client.get("/")  # no identity -> denied
            assert resp.status == 418
            assert resp.headers["X-Custom"] == "teapot"
            assert (await resp.json())["code"] == "missing_identity"

    @pytest.mark.asyncio
    async def test_recovered_signer_forwarded_to_assess(self):
        import base64

        signer_addr = "0xabcdef0123456789abcdef0123456789abcdef01"
        x402_header = base64.b64encode(
            json.dumps(
                {"accepted": {"network": "eip155:8453"}, "payload": {"authorization": {"from": signer_addr}}}
            ).encode()
        ).decode()
        from agentscore_commerce.identity.types import AssessResult

        mock = AsyncMock(return_value=AssessResult(allow=True, decision="allow", reasons=[], raw={}))
        with patch("agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity", new=mock):
            client = await _client(_make_app())
            async with client:
                resp = await client.get("/", headers={"X-Wallet-Address": "0xwallet", "X-Payment": x402_header})
                assert resp.status == 200
            assert mock.call_args.kwargs["signer"] == {"address": signer_addr, "network": "evm"}

    @pytest.mark.asyncio
    async def test_condition_false_short_circuits(self):
        mock = AsyncMock()
        with patch("agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity", new=mock):
            client = await _client(_make_app(condition=lambda _req: False))
            async with client:
                resp = await client.get("/")  # no identity, condition skips
                assert resp.status == 200
            mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_credential_returns_401(self):
        from agentscore_commerce.identity.core import InvalidCredentialError

        with patch(
            "agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity",
            side_effect=InvalidCredentialError(),
        ):
            client = await _client(_make_app())
            async with client:
                resp = await client.get("/", headers={"X-Operator-Token": "opc_bad"})
                assert resp.status == 401
                assert (await resp.json())["error"]["code"] == "invalid_credential"

    @pytest.mark.asyncio
    async def test_timeout_fail_closed_returns_api_error(self):
        with patch(
            "agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity",
            side_effect=httpx.TimeoutException("read timeout"),
        ):
            client = await _client(_make_app(fail_open=False))
            async with client:
                resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
                assert resp.status == 503
                assert (await resp.json())["error"]["code"] == "api_error"

    @pytest.mark.asyncio
    async def test_fixable_denial_falls_back_to_bare_when_session_returns_none(self):
        from agentscore_commerce.identity.types import AssessResult

        deny = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw={})
        with (
            patch(
                "agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity",
                new=AsyncMock(return_value=deny),
            ),
            patch(
                "agentscore_commerce.identity.aiohttp.try_create_session_denial_reason",
                new=AsyncMock(return_value=None),
            ),
        ):
            client = await _client(_make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session")))
            async with client:
                resp = await client.get("/", headers={"X-Wallet-Address": "0xabc"})
                assert resp.status == 403
                assert (await resp.json())["error"]["code"] == "wallet_not_trusted"

    def test_get_signer_verdict_none_when_no_client(self):
        from agentscore_commerce.identity.aiohttp import get_signer_verdict

        req = {GATE_STATE_KEY: {"wallet_address": "0xabc", "client": None}}
        assert get_signer_verdict(req) is None  # type: ignore[arg-type]

    def test_get_signer_verdict_none_when_no_state(self):
        from agentscore_commerce.identity.aiohttp import get_signer_verdict

        assert get_signer_verdict({}) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_get_signer_verdict_reads_from_client(self):
        from agentscore_commerce.identity.aiohttp import get_signer_verdict
        from agentscore_commerce.identity.types import AssessResult

        captured: dict = {}

        async def _sv(request: web.Request) -> web.Response:
            captured["verdict"] = get_signer_verdict(request)
            return web.json_response({"ok": True})

        mock = AsyncMock(return_value=AssessResult(allow=True, decision="allow", reasons=[], raw={}))
        with patch("agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity", new=mock):
            client = await _client(_make_app(handler=_sv))
            async with client:
                resp = await client.get("/", headers={"X-Wallet-Address": "0xsvread"})
                assert resp.status == 200
        assert captured["verdict"] is None

    @pytest.mark.asyncio
    async def test_conditional_middleware_discovery_leg_flows_through(self):
        from agentscore_commerce.identity.aiohttp import conditional_agentscore_gate_middleware

        mock = AsyncMock()
        with patch("agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity", new=mock):
            app = web.Application()
            app.middlewares.append(conditional_agentscore_gate_middleware(api_key="ask_test", require_kyc=True))
            app.router.add_post("/", _ok_handler)
            client = await _client(app)
            async with client:
                resp = await client.post("/")  # no payment header
                assert resp.status == 200
            mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_conditional_middleware_settle_leg_gates(self):
        from agentscore_commerce.identity.aiohttp import conditional_agentscore_gate_middleware
        from agentscore_commerce.identity.types import AssessResult

        deny = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw={})
        with patch(
            "agentscore_commerce.identity.aiohttp.AgentScoreCore.acheck_identity",
            new=AsyncMock(return_value=deny),
        ):
            app = web.Application()
            app.middlewares.append(conditional_agentscore_gate_middleware(api_key="ask_test", require_kyc=True))
            app.router.add_post("/", _ok_handler)
            client = await _client(app)
            async with client:
                resp = await client.post("/", headers={"X-Wallet-Address": "0xabc", "X-Payment": "abc"})
                assert resp.status == 403
