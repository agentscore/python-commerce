"""Tests for the ASGI middleware (Starlette/FastAPI)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import respx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentscore_commerce.identity.middleware import AgentScoreGate, CreateSessionOnMissing, get_assess_data

if TYPE_CHECKING:
    from starlette.requests import Request

ASSESS_URL = "https://api.agentscore.sh/v1/assess"
SESSIONS_URL = "https://api.agentscore.sh/v1/sessions"

SESSION_RESPONSE = {
    "session_id": "sess_abc123",
    "verify_url": "https://agentscore.sh/verify/sess_abc123",
    "poll_secret": "ps_secret_456",
    # API emits structured next_steps; gate stringifies into agent_instructions.
    "next_steps": {
        "action": "deliver_verify_url_and_poll",
        "user_message": "Please complete identity verification at the verify_url.",
    },
}


def _homepage(request: Request) -> JSONResponse:
    agentscore_data = request.state.agentscore if hasattr(request.state, "agentscore") else None
    return JSONResponse({"ok": True, "agentscore": agentscore_data})


def _homepage_via_getter(request: Request) -> JSONResponse:
    return JSONResponse({"assess": get_assess_data(request)})


def _make_app(**gate_kwargs: object) -> Starlette:
    app = Starlette(routes=[Route("/", _homepage)])
    return AgentScoreGate(app, api_key="ask_test_key", **gate_kwargs)


def _mock_assess(decision: str = "allow", reasons: list[str] | None = None) -> respx.Route:
    return respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "decision": decision,
                "decision_reasons": reasons or [],
            },
        )
    )


class TestCreateSessionOnMissing:
    @respx.mock
    def test_creates_session_and_returns_403_with_session_data(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session_key"),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")

        assert resp.status_code == 403
        data = resp.json()
        assert data["error"]["code"] == "identity_verification_required"
        assert data["verify_url"] == "https://agentscore.sh/verify/sess_abc123"
        assert data["session_id"] == "sess_abc123"
        assert data["poll_secret"] == "ps_secret_456"
        # agent_instructions is the JSON-stringified next_steps from the API.
        parsed = json.loads(data["agent_instructions"])
        assert parsed["action"] == "deliver_verify_url_and_poll"
        assert parsed["user_message"] == "Please complete identity verification at the verify_url."

    @respx.mock
    def test_session_request_uses_correct_api_key(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session_key"),
        )
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/")

        assert route.call_count == 1
        request = route.calls[0].request
        assert request.headers["X-API-Key"] == "ask_session_key"

    @respx.mock
    def test_uses_custom_base_url(self):
        custom_url = "https://custom.api.example.com/v1/sessions"
        route = respx.post(custom_url).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(
                api_key="ask_session_key",
                base_url="https://custom.api.example.com",
            ),
        )
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/")

        assert route.call_count == 1

    @respx.mock
    def test_falls_back_to_missing_identity_on_session_api_error(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(500))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session_key"),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")

        assert resp.status_code == 403
        data = resp.json()
        assert data["error"]["code"] == "missing_identity"

    @respx.mock
    def test_falls_back_to_missing_identity_on_network_error(self):
        respx.post(SESSIONS_URL).mock(side_effect=httpx.ConnectError("connection refused"))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session_key"),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")

        assert resp.status_code == 403
        data = resp.json()
        assert data["error"]["code"] == "missing_identity"

    @respx.mock
    def test_get_assess_data_returns_assess_after_pass(self):
        _mock_assess()
        app = Starlette(routes=[Route("/", _homepage_via_getter)])
        gated = AgentScoreGate(app, api_key="ask_test_key")
        client = TestClient(gated, raise_server_exceptions=False)
        resp = client.get("/", headers={"x-wallet-address": "0xabc"})
        assert resp.status_code == 200
        assert resp.json()["assess"] == {"decision": "allow", "decision_reasons": []}

    @respx.mock
    def test_does_not_create_session_when_identity_is_present(self):
        assess_route = _mock_assess()
        session_route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session_key"),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", headers={"x-wallet-address": "0xabc"})

        assert resp.status_code == 200
        assert assess_route.call_count == 1
        assert session_route.call_count == 0

    @respx.mock
    def test_sends_first_class_fields_in_session_request(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(
                api_key="ask_session_key",
                context="Wine purchase verification",
                product_name="Cabernet Reserve 2023",
            ),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")

        assert resp.status_code == 403
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert body["context"] == "Wine purchase verification"
        assert body["product_name"] == "Cabernet Reserve 2023"

    @respx.mock
    def test_omits_unset_fields_from_session_request(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(
                api_key="ask_session_key",
                context="Quick check",
            ),
        )
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/")

        body = json.loads(route.calls[0].request.content)
        assert body["context"] == "Quick check"
        assert "product_name" not in body

    @respx.mock
    def test_fail_open_takes_precedence(self):
        session_route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        app = _make_app(
            fail_open=True,
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session_key"),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")

        assert resp.status_code == 200
        assert session_route.call_count == 0

    @respx.mock
    def test_fixable_wallet_denial_bootstraps_session(self):
        _mock_assess("deny", reasons=["kyc_required"])
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session_key"),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", headers={"x-wallet-address": "0xabc"})

        assert resp.status_code == 403
        data = resp.json()
        assert data["error"]["code"] == "identity_verification_required"
        assert data["session_id"] == "sess_abc123"

    @respx.mock
    def test_unfixable_wallet_denial_returns_bare_wallet_not_trusted(self):
        _mock_assess("deny", reasons=["sanctions_flagged"])
        sessions_route = respx.post(SESSIONS_URL)

        app = _make_app(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session_key"),
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", headers={"x-wallet-address": "0xabc"})

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "wallet_not_trusted"
        assert sessions_route.call_count == 0


CAPTURE_URL = "https://api.agentscore.sh/v1/credentials/wallets"


def _capture_app() -> Starlette:
    """Build a Starlette app whose handler invokes capture_wallet so we can verify the
    gate's context-stashing works end-to-end."""
    from starlette.responses import JSONResponse as SResp

    from agentscore_commerce.identity.middleware import capture_wallet

    async def purchase(request):
        await capture_wallet(request, "0xsigner", "evm", idempotency_key="pi_abc")
        return SResp({"ok": True})

    app = Starlette(routes=[Route("/purchase", purchase, methods=["POST"])])
    return AgentScoreGate(app, api_key="ask_test_key")


class TestCaptureWallet:
    @respx.mock
    def test_captures_when_operator_token_present(self):
        _mock_assess()
        capture_route = respx.post(CAPTURE_URL).mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )
        app = _capture_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/purchase", headers={"X-Operator-Token": "opc_abc"})

        assert resp.status_code == 200
        assert capture_route.called
        body = json.loads(capture_route.calls[0].request.content.decode())
        assert body == {
            "operator_token": "opc_abc",
            "wallet_address": "0xsigner",
            "network": "evm",
            "idempotency_key": "pi_abc",
        }

    @respx.mock
    def test_no_ops_when_wallet_authenticated(self):
        _mock_assess()
        capture_route = respx.post(CAPTURE_URL).mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )
        app = _capture_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/purchase", headers={"X-Wallet-Address": "0xwallet"})

        assert resp.status_code == 200
        assert capture_route.call_count == 0

    @respx.mock
    def test_no_ops_when_gate_did_not_run(self):
        """Calling capture_wallet on a request that never went through AgentScoreGate
        (no middleware installed) should silently no-op rather than crash."""
        import httpx as httpx_client  # avoid shadowing the module-level import
        from starlette.requests import Request as SReq

        from agentscore_commerce.identity.middleware import capture_wallet

        capture_route = respx.post(CAPTURE_URL).mock(
            return_value=httpx_client.Response(200, json={"associated": True, "first_seen": True}),
        )

        # Build a Request with a scope that has no state from our gate.
        scope: dict = {"type": "http", "headers": [], "method": "POST", "path": "/"}

        async def _receive() -> dict:
            return {"type": "http.request"}

        request = SReq(scope, _receive)

        import asyncio

        asyncio.get_event_loop().run_until_complete(
            capture_wallet(request, "0xsigner", "evm"),
        )
        assert capture_route.call_count == 0


@respx.mock
def test_middleware_surfaces_generic_api_error_on_unexpected_exception():
    """When the assess client throws something other than PaymentRequired/TokenDenied,
    the middleware emits a generic api_error denial (no payload/stack leaks)."""
    respx.post(ASSESS_URL).mock(side_effect=httpx.ConnectError("dns lookup failed"))

    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "api_error"


@respx.mock
def test_middleware_fail_open_on_unexpected_exception_lets_request_through():
    respx.post(ASSESS_URL).mock(side_effect=httpx.ConnectError("dns lookup failed"))

    app = _make_app(fail_open=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@respx.mock
def test_middleware_get_gate_degraded_state_returns_default_for_normal_allow():
    from agentscore_commerce.identity.middleware import get_gate_degraded_state

    _mock_assess("allow")

    captured: dict = {}

    def _snoop(request: Request) -> JSONResponse:
        captured.update(get_gate_degraded_state(request))
        return JSONResponse({"ok": True})

    app = AgentScoreGate(
        Starlette(routes=[Route("/", _snoop)]),
        api_key="ask_test_key",
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})
    assert resp.status_code == 200
    assert captured == {"degraded": False}


@respx.mock
def test_middleware_get_gate_degraded_state_returns_infra_reason_when_degraded():
    from agentscore_commerce.identity.middleware import get_gate_degraded_state

    respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))

    captured: dict = {}

    def _snoop(request: Request) -> JSONResponse:
        captured.update(get_gate_degraded_state(request))
        return JSONResponse({"ok": True})

    app = AgentScoreGate(
        Starlette(routes=[Route("/", _snoop)]),
        api_key="ask_test_key",
        fail_open=True,
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})
    assert resp.status_code == 200
    assert captured == {"degraded": True, "infra_reason": "quota_exceeded"}


@respx.mock
def test_middleware_quota_exceeded_returns_503_when_fail_closed():
    """429 from /v1/assess gets dedicated handling; with fail_open=False (default) it
    surfaces as 503 api_error to the buyer with quota-specific contact_merchant
    instructions (NOT retry_with_backoff — quota won't recover from retry)."""
    respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))

    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "api_error"
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "contact_merchant"
    assert "merchant-side issue" in instructions["steps"][0]


@respx.mock
def test_middleware_quota_exceeded_marks_degraded_when_fail_open():
    """fail_open=True + 429 → request flows through; ASGI scope state carries
    degraded=True + infra_reason='quota_exceeded'."""
    from agentscore_commerce.identity.middleware import GATE_STATE_KEY

    respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))

    captured: dict = {}

    def _snoop(request: Request) -> JSONResponse:
        state = (request.scope.get("state") or {}).get(GATE_STATE_KEY) or {}
        captured.update(state)
        return JSONResponse({k: v for k, v in state.items() if k != "client"})

    app = AgentScoreGate(
        Starlette(routes=[Route("/", _snoop)]),
        api_key="ask_test_key",
        fail_open=True,
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("degraded") is True
    assert data.get("infra_reason") == "quota_exceeded"


def test_middleware_timeout_marks_degraded_when_fail_open():
    """fail_open=True + httpx.TimeoutException → request flows through; scope state
    carries degraded=True + infra_reason='network_timeout'."""
    from unittest.mock import AsyncMock, patch

    from agentscore_commerce.identity.middleware import GATE_STATE_KEY

    def _snoop(request: Request) -> JSONResponse:
        state = (request.scope.get("state") or {}).get(GATE_STATE_KEY) or {}
        return JSONResponse({k: v for k, v in state.items() if k != "client"})

    app = AgentScoreGate(
        Starlette(routes=[Route("/", _snoop)]),
        api_key="ask_test_key",
        fail_open=True,
    )

    with patch(
        "agentscore_commerce.identity.middleware.GateClient.acheck_identity",
        new=AsyncMock(side_effect=httpx.TimeoutException("read timeout")),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("degraded") is True
    assert data.get("infra_reason") == "network_timeout"


@respx.mock
def test_middleware_passes_through_token_expired_with_auto_session():
    # Revoked and expired credentials both surface as token_expired from the API with an
    # auto-minted session in the 401 body. Middleware forwards all session fields so the
    # 403 downstream carries verify_url + session_id + poll_secret for agent recovery.
    respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {"code": "token_expired", "message": "invalid"},
                "session_id": "sess_auto",
                "poll_secret": "poll_auto",
                "verify_url": "https://agentscore.sh/verify?session=sess_auto",
                "poll_url": "https://api.agentscore.sh/v1/sessions/sess_auto",
                "next_steps": {"action": "deliver_verify_url_and_poll"},
            },
        )
    )

    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-operator-token": "opc_revoked"})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "token_expired"
    assert body["session_id"] == "sess_auto"
    assert body["poll_secret"] == "poll_auto"
    assert body["verify_url"] == "https://agentscore.sh/verify?session=sess_auto"
    assert json.loads(body["agent_instructions"]) == {"action": "deliver_verify_url_and_poll"}


@respx.mock
def test_middleware_emits_invalid_credential_no_session():
    # `invalid_credential` is permanent — the API returns 401 with NO auto-session
    # (distinct from token_expired). Middleware must classify it as a 403 with
    # action='switch_token_or_restart_session', NOT fall through to api_error 503
    # which would tell the agent to retry forever on a permanent state.
    respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": {"code": "invalid_credential", "message": "Operator credential not found"}},
        )
    )

    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-operator-token": "opc_typo_does_not_exist"})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "invalid_credential"
    # Agent_instructions guides the agent to switch tokens or restart the session flow.
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "switch_token_or_restart_session"
    msg = instructions["user_message"].lower()
    assert "switch tokens" in msg or "different stored token" in msg
    # No session fields — the API didn't mint one for this case.
    assert "session_id" not in body
    assert "verify_url" not in body
    assert "poll_secret" not in body


@respx.mock
def test_middleware_passes_through_token_expired_without_next_steps():
    respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": {"code": "token_expired", "message": "expired"}},
        )
    )

    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-operator-token": "opc_expired"})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "token_expired"
    # API didn't supply next_steps → fallback agent_instructions injected by
    # _response.py so agents always have a recovery action.
    assert json.loads(body["agent_instructions"])["action"] == "deliver_verify_url_and_poll"


@respx.mock
def test_middleware_emits_wallet_not_trusted_on_policy_deny():
    respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "decision": "deny",
                "decision_reasons": ["kyc_required"],
                "verify_url": "https://agentscore.sh/verify",
            },
        )
    )
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "wallet_not_trusted"
    assert body["decision"] == "deny"
    assert body["reasons"] == ["kyc_required"]
    assert body["verify_url"] == "https://agentscore.sh/verify"


@respx.mock
def test_middleware_emits_payment_required_on_402():
    respx.post(ASSESS_URL).mock(return_value=httpx.Response(402, json={}))

    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "payment_required"


@respx.mock
def test_middleware_fail_open_on_402_lets_request_through():
    respx.post(ASSESS_URL).mock(return_value=httpx.Response(402, json={}))

    app = _make_app(fail_open=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@respx.mock
def test_middleware_handler_exception_is_not_swallowed_by_gate():
    """Regression: gate's try-block must NOT wrap the downstream ASGI app. If the user's
    app raises, the exception must propagate up — NOT be misclassified as an AgentScore
    infra failure (which under fail_open would re-invoke the app)."""
    _mock_assess(decision="allow")

    invocations = {"count": 0}

    def boom_route(_request: Request) -> JSONResponse:
        invocations["count"] += 1
        msg = "downstream app failure"
        raise RuntimeError(msg)

    inner = Starlette(routes=[Route("/", boom_route)])
    app = AgentScoreGate(inner, api_key="ask_test_key", fail_open=True)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/", headers={"x-wallet-address": "0xabc"})
    # Starlette surfaces unhandled exceptions as 500 — the important thing is the route
    # ran exactly once (no fail-open retry) and the gate didn't claim the exception was
    # an AgentScore infra failure.
    assert resp.status_code == 500
    assert invocations["count"] == 1


@respx.mock
def test_middleware_propagates_quota_from_assess_response_headers():
    """API X-Quota-* → SDK populates AssessResponse.quota → adapter stashes onto scope state."""
    from agentscore_commerce.identity.middleware import get_gate_quota_info

    respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"X-Quota-Limit": "1500", "X-Quota-Used": "1200", "X-Quota-Reset": "2026-06-01T00:00:00Z"},
            json={"decision": "allow", "decision_reasons": []},
        ),
    )

    captured: dict = {}

    def quota_route(request: Request) -> JSONResponse:
        captured["quota"] = get_gate_quota_info(request)
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/", quota_route)])
    app = AgentScoreGate(inner, api_key="ask_test_key")
    client = TestClient(app)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})
    assert resp.status_code == 200
    assert captured["quota"] is not None
    assert captured["quota"].limit == 1500
