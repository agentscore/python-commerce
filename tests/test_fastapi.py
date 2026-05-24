"""Tests for the FastAPI native dependency adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import respx
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from agentscore_commerce.identity.fastapi import (
    AgentScoreGate,
    capture_wallet,
    get_agentscore_data,
)
from agentscore_commerce.identity.sessions import CreateSessionOnMissing

ASSESS_URL = "https://api.agentscore.sh/v1/assess"
SESSIONS_URL = "https://api.agentscore.sh/v1/sessions"
CAPTURE_URL = "https://api.agentscore.sh/v1/credentials/wallets"


def _mock_assess(decision: str = "allow", reasons: list[str] | None = None) -> respx.Route:
    return respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(200, json={"decision": decision, "decision_reasons": reasons or []}),
    )


def _make_app(gate: AgentScoreGate) -> FastAPI:
    app = FastAPI()

    @app.get("/", dependencies=[Depends(gate)])
    async def index(assess=Depends(get_agentscore_data)):
        return {"ok": True, "assess": assess}

    @app.post("/purchase", dependencies=[Depends(gate)])
    async def purchase(request: Request):
        await capture_wallet(request, "0xsigner", "evm", idempotency_key="pi_abc")
        return {"ok": True}

    return app


class TestDependency:
    @respx.mock
    def test_allows_trusted_wallet(self):
        _mock_assess("allow")
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["assess"] == {"decision": "allow", "decision_reasons": []}

    @respx.mock
    def test_denies_untrusted_wallet(self):
        _mock_assess("deny", reasons=["kyc_required"])
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 403
        body = resp.json()
        # FastAPI wraps HTTPException detail in {"detail": {...}}.
        assert body["detail"]["error"]["code"] == "wallet_not_trusted"
        assert body["detail"]["reasons"] == ["kyc_required"]

    def test_missing_identity_returns_403(self):
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get("/")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "missing_identity"

    def test_fail_open_allows_through_when_identity_missing(self):
        gate = AgentScoreGate(api_key="ask_test", fail_open=True)
        client = TestClient(_make_app(gate))
        resp = client.get("/")
        assert resp.status_code == 200

    @respx.mock
    def test_passes_operator_token_to_assess(self):
        route = _mock_assess("allow")
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        client.get("/", headers={"X-Operator-Token": "opc_abc"})
        body = json.loads(route.calls[0].request.content)
        assert body["operator_token"] == "opc_abc"

    @respx.mock
    def test_raises_on_402_payment_required(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(402))
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "payment_required"

    @respx.mock
    def test_api_error_returns_403_api_error(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500, text="oops"))
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"]["code"] == "api_error"

    @respx.mock
    def test_quota_exceeded_returns_503_when_fail_closed(self):
        """429 from /v1/assess gets dedicated handling; with fail_open=False (default) it
        surfaces as 503 api_error to the buyer with quota-specific contact_merchant
        instructions (NOT retry_with_backoff — quota won't recover from retry)."""
        import json as _json

        respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 503
        body = resp.json()["detail"]
        assert body["error"]["code"] == "api_error"
        instructions = _json.loads(body["agent_instructions"])
        assert instructions["action"] == "contact_merchant"
        assert "merchant-side issue" in instructions["steps"][0]

    @respx.mock
    def test_fail_open_marks_degraded_with_infra_reason_quota(self):
        """fail_open=True + 429 → request flows through; gate state carries
        degraded=True + infra_reason='quota_exceeded' for merchant logging/alerts."""
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))
        from fastapi import FastAPI

        from agentscore_commerce.identity.fastapi import GATE_STATE_KEY

        gate = AgentScoreGate(api_key="ask_test", fail_open=True)
        app = FastAPI()
        captured: dict = {}

        @app.get("/", dependencies=[Depends(gate)])
        def _root(req: Request):
            state = getattr(req.state, GATE_STATE_KEY, None)
            captured.update(state or {})
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 200
        assert captured.get("degraded") is True
        assert captured.get("infra_reason") == "quota_exceeded"

    @respx.mock
    def test_fail_open_marks_degraded_with_infra_reason_api_error(self):
        """fail_open=True + 5xx → request flows through; gate state carries
        degraded=True + infra_reason='api_error'."""
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500))
        from fastapi import FastAPI

        from agentscore_commerce.identity.fastapi import GATE_STATE_KEY

        gate = AgentScoreGate(api_key="ask_test", fail_open=True)
        app = FastAPI()
        captured: dict = {}

        @app.get("/", dependencies=[Depends(gate)])
        def _root(req: Request):
            state = getattr(req.state, GATE_STATE_KEY, None)
            captured.update(state or {})
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 200
        assert captured.get("degraded") is True
        assert captured.get("infra_reason") == "api_error"

    def test_get_gate_degraded_state_returns_default_for_normal_allow(self):
        """get_gate_degraded_state returns {degraded: False} for normal compliance allows."""
        from fastapi import FastAPI

        from agentscore_commerce.identity.fastapi import get_gate_degraded_state

        gate = AgentScoreGate(api_key="ask_test")
        app = FastAPI()
        captured: dict = {}

        @app.get("/", dependencies=[Depends(gate)])
        def _root(req: Request):
            captured.update(get_gate_degraded_state(req))
            return {"ok": True}

        with patch(
            "agentscore_commerce.identity.fastapi.AgentScoreCore.acheck_identity",
            new=AsyncMock(
                return_value=__import__("agentscore_commerce.identity.types", fromlist=["AssessResult"]).AssessResult(
                    allow=True, decision="allow"
                )
            ),
        ):
            client = TestClient(app)
            resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status_code == 200
            assert captured == {"degraded": False}

    def test_get_gate_degraded_state_returns_infra_reason_when_degraded(self):
        """get_gate_degraded_state returns {degraded: True, infra_reason: ...} when gate degraded."""
        from fastapi import FastAPI

        from agentscore_commerce.identity.core import QuotaExceededError
        from agentscore_commerce.identity.fastapi import get_gate_degraded_state

        gate = AgentScoreGate(api_key="ask_test", fail_open=True)
        app = FastAPI()
        captured: dict = {}

        @app.get("/", dependencies=[Depends(gate)])
        def _root(req: Request):
            captured.update(get_gate_degraded_state(req))
            return {"ok": True}

        with patch(
            "agentscore_commerce.identity.fastapi.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=QuotaExceededError("quota_exceeded")),
        ):
            client = TestClient(app)
            resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status_code == 200
            assert captured == {"degraded": True, "infra_reason": "quota_exceeded"}

    def test_fail_open_marks_degraded_with_infra_reason_network_timeout(self):
        """fail_open=True + httpx.TimeoutException → request flows through;
        gate state carries degraded=True + infra_reason='network_timeout'."""
        from fastapi import FastAPI

        from agentscore_commerce.identity.fastapi import GATE_STATE_KEY

        gate = AgentScoreGate(api_key="ask_test", fail_open=True)
        app = FastAPI()
        captured: dict = {}

        @app.get("/", dependencies=[Depends(gate)])
        def _root(req: Request):
            state = getattr(req.state, GATE_STATE_KEY, None)
            captured.update(state or {})
            return {"ok": True}

        with patch(
            "agentscore_commerce.identity.fastapi.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=httpx.TimeoutException("read timeout")),
        ):
            client = TestClient(app)
            resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status_code == 200
            assert captured.get("degraded") is True
            assert captured.get("infra_reason") == "network_timeout"


class TestOnDenied:
    def test_custom_on_denied_controls_status_and_body(self):
        def custom(_req, reason):
            return {"blocked": True, "code": reason.code, "custom": "yes"}, 451

        gate = AgentScoreGate(api_key="ask_test", on_denied=custom)
        client = TestClient(_make_app(gate))
        resp = client.get("/")
        assert resp.status_code == 451
        body = resp.json()["detail"]
        assert body["blocked"] is True
        assert body["code"] == "missing_identity"
        assert body["custom"] == "yes"


class TestChainOption:
    @respx.mock
    def test_constructor_chain_forwarded_to_assess(self):
        route = _mock_assess("allow")
        gate = AgentScoreGate(api_key="ask_test", chain="solana")
        client = TestClient(_make_app(gate))
        client.get("/", headers={"X-Wallet-Address": "0xabc"})
        body = json.loads(route.calls[0].request.content)
        assert body["chain"] == "solana"

    @respx.mock
    def test_extract_chain_overrides_constructor_chain(self):
        route = _mock_assess("allow")
        gate = AgentScoreGate(
            api_key="ask_test",
            chain="base",
            extract_chain=lambda _req: "ethereum",
        )
        client = TestClient(_make_app(gate))
        client.get("/", headers={"X-Wallet-Address": "0xabc"})
        body = json.loads(route.calls[0].request.content)
        assert body["chain"] == "ethereum"

    @respx.mock
    def test_no_chain_sent_when_neither_configured(self):
        route = _mock_assess("allow")
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        client.get("/", headers={"X-Wallet-Address": "0xabc"})
        body = json.loads(route.calls[0].request.content)
        assert "chain" not in body


class TestCreateSessionOnMissing:
    @respx.mock
    def test_creates_session_and_returns_403_with_session_data(self):
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
        gate = AgentScoreGate(
            api_key="ask_test",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        client = TestClient(_make_app(gate))
        resp = client.get("/")
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"]["code"] == "identity_verification_required"
        assert detail["session_id"] == "sess_abc123"
        assert detail["verify_url"] == "https://agentscore.sh/verify/sess_abc123"
        assert detail["poll_secret"] == "ps_secret"
        # agent_instructions is the JSON-stringified next_steps from the API.
        import json as _json

        parsed = _json.loads(detail["agent_instructions"])
        assert parsed["action"] == "deliver_verify_url_and_poll"
        assert parsed["user_message"] == "please verify"

    @respx.mock
    def test_falls_back_to_missing_identity_on_session_api_failure(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(500, text="oops"))
        gate = AgentScoreGate(
            api_key="ask_test",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        client = TestClient(_make_app(gate))
        resp = client.get("/")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "missing_identity"

    @respx.mock
    def test_fixable_wallet_denial_bootstraps_session(self):
        # When /v1/assess returns deny with a fixable reason (kyc_required), the gate
        # should mint a verification session via /v1/sessions and return
        # identity_verification_required (not bare wallet_not_trusted), giving the
        # agent the same poll-and-retry UX as missing_identity.
        _mock_assess("deny", reasons=["kyc_required"])
        respx.post(SESSIONS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "session_id": "sess_kyc",
                    "verify_url": "https://agentscore.sh/verify/sess_kyc",
                    "poll_secret": "ps_kyc",
                    "next_steps": {"action": "deliver_verify_url_and_poll"},
                },
            )
        )
        gate = AgentScoreGate(
            api_key="ask_test",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"]["code"] == "identity_verification_required"
        assert detail["session_id"] == "sess_kyc"

    @respx.mock
    def test_unfixable_wallet_denial_returns_bare_wallet_not_trusted(self):
        # Sanctions / age / jurisdiction_restricted are unfixable — re-verification
        # won't change the outcome. Gate should emit bare wallet_not_trusted (no
        # session bootstrap) so the agent surfaces contact-support copy.
        _mock_assess("deny", reasons=["sanctions_flagged"])
        sessions_route = respx.post(SESSIONS_URL)
        gate = AgentScoreGate(
            api_key="ask_test",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "wallet_not_trusted"
        assert sessions_route.call_count == 0

    @respx.mock
    def test_fixable_wallet_falls_back_to_bare_when_session_mint_fails(self):
        _mock_assess("deny", reasons=["kyc_required"])
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(500, text="oops"))
        gate = AgentScoreGate(
            api_key="ask_test",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "wallet_not_trusted"


class TestCaptureWallet:
    @respx.mock
    def test_captures_when_operator_token_present(self):
        _mock_assess("allow")
        capture_route = respx.post(CAPTURE_URL).mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.post("/purchase", headers={"X-Operator-Token": "opc_abc"})
        assert resp.status_code == 200
        assert capture_route.called
        body = json.loads(capture_route.calls[0].request.content)
        assert body == {
            "operator_token": "opc_abc",
            "wallet_address": "0xsigner",
            "network": "evm",
            "idempotency_key": "pi_abc",
        }

    @respx.mock
    def test_no_ops_when_wallet_authenticated(self):
        _mock_assess("allow")
        capture_route = respx.post(CAPTURE_URL).mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.post("/purchase", headers={"X-Wallet-Address": "0xwallet"})
        assert resp.status_code == 200
        assert capture_route.call_count == 0

    def test_no_ops_when_gate_did_not_run(self):
        """Handler wired without the gate dependency — capture_wallet must silently no-op."""
        app = FastAPI()

        @app.post("/purchase")
        async def purchase(request: Request):
            await capture_wallet(request, "0xsigner", "evm")
            return {"ok": True}

        client = TestClient(app)
        with patch(
            "agentscore_commerce.identity.core.AgentScoreCore.acapture_wallet",
            new=AsyncMock(),
        ) as mock_cap:
            resp = client.post("/purchase")
            assert resp.status_code == 200
        mock_cap.assert_not_called()


class TestGetAssessData:
    @respx.mock
    def test_returns_assess_data_on_allow(self):
        _mock_assess("allow", reasons=["verified"])
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assess = resp.json()["assess"]
        assert assess["decision"] == "allow"
        assert assess["decision_reasons"] == ["verified"]

    def test_returns_none_when_gate_bypassed_via_fail_open(self):
        gate = AgentScoreGate(api_key="ask_test", fail_open=True)
        client = TestClient(_make_app(gate))
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json()["assess"] is None


class TestGetGateQuotaInfo:
    @respx.mock
    def test_propagates_quota_from_assess_response_headers(self):
        # API emits X-Quota-* on the assess response → SDK populates AssessResponse.quota →
        # gate stashes onto request state → adapter exposes via get_gate_quota_info().
        from agentscore_commerce.identity.fastapi import get_gate_quota_info

        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                headers={"X-Quota-Limit": "1500", "X-Quota-Used": "1200", "X-Quota-Reset": "2026-06-01T00:00:00Z"},
                json={"decision": "allow", "decision_reasons": []},
            ),
        )

        captured = {}
        gate = AgentScoreGate(api_key="ask_test")
        app = FastAPI()

        @app.get("/", dependencies=[Depends(gate)])
        async def index(request: Request):
            quota = get_gate_quota_info(request)
            captured["quota"] = quota
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 200
        assert captured["quota"] is not None
        assert captured["quota"].limit == 1500
        assert captured["quota"].used == 1200
        assert captured["quota"].reset == "2026-06-01T00:00:00Z"

    @respx.mock
    def test_returns_none_when_api_omits_quota_headers(self):
        # Enterprise / unlimited tiers don't emit X-Quota-* headers — the gate state
        # carries no quota and get_gate_quota_info returns None.
        from agentscore_commerce.identity.fastapi import get_gate_quota_info

        _mock_assess("allow")  # no quota headers
        gate = AgentScoreGate(api_key="ask_test")
        app = FastAPI()
        captured = {}

        @app.get("/", dependencies=[Depends(gate)])
        async def index(request: Request):
            captured["quota"] = get_gate_quota_info(request)
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 200
        assert captured["quota"] is None


class TestUserAgent:
    @respx.mock
    def test_default_user_agent_format(self):
        route = _mock_assess("allow")
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        client.get("/", headers={"X-Wallet-Address": "0xabc"})
        ua = route.calls[0].request.headers["User-Agent"]
        assert ua.startswith("agentscore-commerce/")

    @respx.mock
    def test_custom_user_agent_prepended(self):
        route = _mock_assess("allow")
        gate = AgentScoreGate(api_key="ask_test", user_agent="myapp/2.0")
        client = TestClient(_make_app(gate))
        client.get("/", headers={"X-Wallet-Address": "0xabc"})
        ua = route.calls[0].request.headers["User-Agent"]
        assert ua.startswith("myapp/2.0 (agentscore-commerce/")


@respx.mock
def test_fastapi_passes_through_token_expired():
    respx.post("https://api.agentscore.sh/v1/assess").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {"code": "token_expired", "message": "expired"},
                "next_steps": {"action": "deliver_verify_url_and_poll"},
            },
        )
    )
    gate = AgentScoreGate(api_key="ak", fail_open=False)
    app = FastAPI()

    @app.get("/", dependencies=[Depends(gate)])
    def index():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-operator-token": "opc_exp"})
    assert resp.status_code == 401
    # FastAPI wraps the denial body under HTTPException.detail.
    detail = resp.json()["detail"]
    assert detail["error"]["code"] == "token_expired"
    assert json.loads(detail["agent_instructions"]) == {"action": "deliver_verify_url_and_poll"}


@respx.mock
def test_fastapi_api_error_on_unexpected_exception():
    respx.post("https://api.agentscore.sh/v1/assess").mock(
        side_effect=httpx.ConnectError("dns down"),
    )
    gate = AgentScoreGate(api_key="ak", fail_open=False)
    app = FastAPI()

    @app.get("/", dependencies=[Depends(gate)])
    def index():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/", headers={"x-wallet-address": "0xabc"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"]["code"] == "api_error"


class TestOnDeniedThreeTuple:
    def test_on_denied_three_tuple_sets_headers(self):
        """on_denied may return (body, status, headers); the gate threads headers
        onto the HTTPException."""

        def custom(_req, reason):
            return ({"code": reason.code}, 418, {"X-Custom": "teapot"})

        gate = AgentScoreGate(api_key="ask_test", on_denied=custom)
        client = TestClient(_make_app(gate))
        resp = client.get("/")
        assert resp.status_code == 418
        assert resp.headers["X-Custom"] == "teapot"
        assert resp.json()["detail"]["code"] == "missing_identity"


class TestSignerForwarding:
    @respx.mock
    def test_recovered_signer_forwarded_to_assess(self):
        """A wallet-authenticated request with an x402 payment header pre-extracts
        the EIP-3009 signer and forwards it in the assess body."""
        import base64
        import json as _json

        route = _mock_assess("allow")
        signer_addr = "0xabcdef0123456789abcdef0123456789abcdef01"
        x402_header = base64.b64encode(
            _json.dumps(
                {"accepted": {"network": "eip155:8453"}, "payload": {"authorization": {"from": signer_addr}}}
            ).encode()
        ).decode()

        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get(
            "/",
            headers={"X-Wallet-Address": "0xwallet", "X-Payment": x402_header},
        )
        assert resp.status_code == 200
        body = _json.loads(route.calls[0].request.content)
        assert body["signer"] == {"address": signer_addr, "network": "evm"}


class TestInvalidCredentialAndPaymentFailOpen:
    @respx.mock
    def test_invalid_credential_returns_denial(self):
        """A 401 invalid_credential from assess surfaces an invalid_credential denial."""
        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(401, json={"error": {"code": "invalid_credential", "message": "bad"}})
        )
        gate = AgentScoreGate(api_key="ask_test")
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Operator-Token": "opc_bad"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"]["code"] == "invalid_credential"

    @respx.mock
    def test_payment_required_fail_open_passes_through(self):
        """fail_open=True + 402 from assess → request flows through to the handler."""
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(402))
        gate = AgentScoreGate(api_key="ask_test", fail_open=True)
        client = TestClient(_make_app(gate))
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestGetSignerVerdictEdgeCases:
    def test_returns_none_when_no_wallet_address(self):
        """get_signer_verdict returns None for operator-token-only requests."""
        from agentscore_commerce.identity.fastapi import get_signer_verdict

        gate = AgentScoreGate(api_key="ask_test")
        app = FastAPI()
        captured: dict = {}

        @app.get("/", dependencies=[Depends(gate)])
        def _root(req: Request):
            captured["verdict"] = get_signer_verdict(req)
            return {"ok": True}

        with patch(
            "agentscore_commerce.identity.fastapi.AgentScoreCore.acheck_identity",
            new=AsyncMock(
                return_value=__import__("agentscore_commerce.identity.types", fromlist=["AssessResult"]).AssessResult(
                    allow=True, decision="allow"
                )
            ),
        ):
            client = TestClient(app)
            resp = client.get("/", headers={"X-Operator-Token": "opc_abc"})
        assert resp.status_code == 200
        assert captured["verdict"] is None

    @respx.mock
    def test_returns_verdict_for_wallet_request(self):
        """A wallet-authenticated request reaches the client.get_signer_verdict read."""
        from agentscore_commerce.identity.fastapi import get_signer_verdict

        _mock_assess("allow")
        gate = AgentScoreGate(api_key="ask_test")
        app = FastAPI()
        captured: dict = {}

        @app.get("/", dependencies=[Depends(gate)])
        def _root(req: Request):
            captured["verdict"] = get_signer_verdict(req)
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status_code == 200
        # No signer was extracted (no x402 header), so the verdict is None — but the
        # client.get_signer_verdict read path (lines 336-339) was exercised.
        assert captured["verdict"] is None

    def test_returns_none_when_state_has_no_client(self):
        """Defensive guard: state with a wallet_address but no client yields None."""
        from types import SimpleNamespace

        from agentscore_commerce.identity.fastapi import GATE_STATE_KEY, get_signer_verdict

        req = SimpleNamespace(state=SimpleNamespace(**{GATE_STATE_KEY: {"wallet_address": "0xabc", "client": None}}))
        assert get_signer_verdict(req) is None  # type: ignore[arg-type]

    def test_get_gate_quota_info_returns_none_for_ungated_request(self):
        """get_gate_quota_info on an ungated route (no gate state) returns None."""
        from agentscore_commerce.identity.fastapi import get_gate_quota_info

        app = FastAPI()
        captured: dict = {}

        @app.get("/")
        def _root(req: Request):
            captured["quota"] = get_gate_quota_info(req)
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert captured["quota"] is None


class TestConditionalGate:
    @respx.mock
    def test_discovery_leg_flows_through_unauthenticated(self):
        """ConditionalAgentScoreGate lets a no-credential discovery leg through without
        calling assess."""
        from agentscore_commerce.identity.fastapi import ConditionalAgentScoreGate

        route = _mock_assess("allow")
        gate = ConditionalAgentScoreGate(api_key="ask_test", require_kyc=True)
        app = FastAPI()

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase():
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/purchase")  # no payment header
        assert resp.status_code == 200
        assert route.call_count == 0

    @respx.mock
    def test_settle_leg_triggers_full_gate(self):
        """A request carrying a payment credential triggers the inner gate."""
        from agentscore_commerce.identity.fastapi import ConditionalAgentScoreGate

        _mock_assess("deny", reasons=["kyc_required"])
        gate = ConditionalAgentScoreGate(api_key="ask_test", require_kyc=True)
        app = FastAPI()

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase():
            return {"ok": True}

        client = TestClient(app)
        resp = client.post("/purchase", headers={"X-Wallet-Address": "0xabc", "X-Payment": "abc"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"]["code"] == "wallet_not_trusted"
