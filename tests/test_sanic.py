"""Tests for the Sanic adapter.

Sanic's test client runs the app on a real loopback socket and uses httpx to hit it,
which makes respx-based URL mocking awkward. We mock ``AgentScoreCore.acheck_identity``
directly (matching the Flask/Django test pattern) and verify the adapter plumbing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sanic import Sanic, response

from agentscore_commerce.identity.sanic import agentscore_gate, capture_wallet, get_agentscore_data
from agentscore_commerce.identity.sessions import CreateSessionOnMissing
from agentscore_commerce.identity.types import AssessResult, DenialReason


def _allow_result() -> AssessResult:
    return AssessResult(allow=True, decision="allow", reasons=[], raw={"decision": "allow"})


def _deny_result() -> AssessResult:
    return AssessResult(allow=False, decision="deny", reasons=["kyc_required"])


def _make_app(name: str, **gate_kwargs) -> Sanic:
    # Each test uses a unique app name so Sanic's global registry doesn't collide.
    app = Sanic.get_app(name, force_create=True)
    agentscore_gate(app, api_key="ask_test", **gate_kwargs)

    @app.get("/")
    async def handler(request):
        agentscore_data = getattr(request.ctx, "agentscore", None)
        return response.json({"ok": True, "agentscore": agentscore_data})

    @app.post("/purchase")
    async def purchase(request):
        await capture_wallet(request, "0xsigner", "evm", idempotency_key="pi_abc")
        return response.json({"ok": True})

    return app


class TestIdentityExtraction:
    def test_allows_trusted_wallet(self):
        app = _make_app("sanic_allow_wallet")
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(return_value=_allow_result()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200
        assert resp.json["ok"] is True

    def test_get_agentscore_data_returns_assess_after_pass(self):
        app = Sanic.get_app("sanic_get_agentscore_data", force_create=True)
        agentscore_gate(app, api_key="ask_test")

        @app.get("/")
        async def handler(request):
            return response.json({"assess": get_agentscore_data(request)})

        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(return_value=_allow_result()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200
        assert resp.json["assess"] == {"decision": "allow"}

    def test_denies_untrusted_wallet(self):
        app = _make_app("sanic_deny_wallet")
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(return_value=_deny_result()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 403
        assert resp.json["error"]["code"] == "wallet_not_trusted"
        assert resp.json["reasons"] == ["kyc_required"]

    def test_missing_identity_returns_403(self):
        app = _make_app("sanic_missing")
        _, resp = app.test_client.get("/")
        assert resp.status == 403
        assert resp.json["error"]["code"] == "missing_identity"

    def test_fail_open_allows_through_when_identity_missing(self):
        app = _make_app("sanic_fail_open", fail_open=True)
        _, resp = app.test_client.get("/")
        assert resp.status == 200

    def test_passes_operator_token_to_assess(self):
        app = _make_app("sanic_operator_token")
        mock = AsyncMock(return_value=_allow_result())
        with patch("agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity", new=mock):
            app.test_client.get("/", headers={"X-Operator-Token": "opc_abc"})
        # First positional arg is the AgentIdentity instance.
        identity_arg = mock.await_args.args[0]
        assert identity_arg.operator_token == "opc_abc"
        assert identity_arg.address is None


class TestErrorPaths:
    def test_returns_403_payment_required_on_402(self):
        from agentscore_commerce.identity.core import PaymentRequiredError

        app = _make_app("sanic_402")
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=PaymentRequiredError()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 403
        assert resp.json["error"]["code"] == "payment_required"

    def test_returns_503_api_error_on_exception(self):
        app = _make_app("sanic_api_error")
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 503
        assert resp.json["error"]["code"] == "api_error"

    def test_fail_open_allows_through_on_402(self):
        from agentscore_commerce.identity.core import PaymentRequiredError

        app = _make_app("sanic_fail_open_402", fail_open=True)
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=PaymentRequiredError()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200

    def test_get_gate_degraded_state_returns_default_for_normal_allow(self):
        from agentscore_commerce.identity.sanic import get_gate_degraded_state

        app = Sanic.get_app("sanic_get_state_default", force_create=True)
        agentscore_gate(app, api_key="ask_test")
        captured: dict = {}

        @app.get("/snoop")
        async def _snoop(request):
            captured.update(get_gate_degraded_state(request))
            return response.json({"ok": True})

        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(return_value=_allow_result()),
        ):
            _, resp = app.test_client.get("/snoop", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200
        assert captured == {"degraded": False}

    def test_get_gate_degraded_state_returns_infra_reason_when_degraded(self):
        from agentscore_commerce.identity.core import QuotaExceededError
        from agentscore_commerce.identity.sanic import get_gate_degraded_state

        app = Sanic.get_app("sanic_get_state_degraded", force_create=True)
        agentscore_gate(app, api_key="ask_test", fail_open=True)
        captured: dict = {}

        @app.get("/snoop")
        async def _snoop(request):
            captured.update(get_gate_degraded_state(request))
            return response.json({"ok": True})

        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=QuotaExceededError("quota_exceeded")),
        ):
            _, resp = app.test_client.get("/snoop", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200
        assert captured == {"degraded": True, "infra_reason": "quota_exceeded"}

    def test_quota_exceeded_returns_503_when_fail_closed(self):
        import json as _json

        from agentscore_commerce.identity.core import QuotaExceededError

        app = _make_app("sanic_quota_closed")
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=QuotaExceededError("quota_exceeded")),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 503
        assert resp.json["error"]["code"] == "api_error"
        instructions = _json.loads(resp.json["agent_instructions"])
        assert instructions["action"] == "contact_merchant"
        assert "merchant-side issue" in instructions["steps"][0]

    def test_quota_exceeded_marks_degraded_when_fail_open(self):
        from agentscore_commerce.identity.core import QuotaExceededError
        from agentscore_commerce.identity.sanic import GATE_STATE_ATTR

        # Build a fresh app to inspect gate state.
        app = Sanic.get_app("sanic_quota_open", force_create=True)
        agentscore_gate(app, api_key="ask_test", fail_open=True)

        @app.get("/snoop")
        async def _snoop(request):
            state = getattr(request.ctx, GATE_STATE_ATTR, {}) or {}
            return response.json({k: v for k, v in state.items() if k != "client"})

        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=QuotaExceededError("quota_exceeded")),
        ):
            _, resp = app.test_client.get("/snoop", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200
        data = resp.json
        assert data.get("degraded") is True
        assert data.get("infra_reason") == "quota_exceeded"

    def test_timeout_marks_degraded_when_fail_open(self):
        import httpx

        from agentscore_commerce.identity.sanic import GATE_STATE_ATTR

        app = Sanic.get_app("sanic_timeout_open", force_create=True)
        agentscore_gate(app, api_key="ask_test", fail_open=True)

        @app.get("/snoop")
        async def _snoop(request):
            state = getattr(request.ctx, GATE_STATE_ATTR, {}) or {}
            return response.json({k: v for k, v in state.items() if k != "client"})

        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=httpx.TimeoutException("read timeout")),
        ):
            _, resp = app.test_client.get("/snoop", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200
        data = resp.json
        assert data.get("degraded") is True
        assert data.get("infra_reason") == "network_timeout"

    def test_fail_open_allows_through_on_api_error(self):
        app = _make_app("sanic_fail_open_api", fail_open=True)
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200


class TestChainOption:
    def test_no_extract_chain_passes_none_to_acheck_identity(self):
        """Adapter passes None as chain override when extract_chain isn't configured,
        so AgentScoreCore's constructor-level chain takes effect (or no chain is sent)."""
        app = _make_app("sanic_chain_none", chain="solana")
        mock = AsyncMock(return_value=_allow_result())
        with patch("agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity", new=mock):
            app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        chain_arg = mock.await_args.args[1]
        assert chain_arg is None  # extract_chain not set → adapter passes None

    def test_extract_chain_callback_passed_to_acheck_identity(self):
        app = _make_app("sanic_chain_callback", extract_chain=lambda _req: "ethereum")
        mock = AsyncMock(return_value=_allow_result())
        with patch("agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity", new=mock):
            app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert mock.await_args.args[1] == "ethereum"

    def test_constructor_chain_stored_on_client(self):
        """The constructor-level `chain` option is forwarded to AgentScoreCore so it gets
        embedded in every outbound /v1/assess body (verified in test_client.py)."""
        app = _make_app("sanic_chain_ctor", chain="base")
        # Access the client instance via the registered middleware to confirm chain was stored.
        # No public accessor, so we just verify the adapter didn't crash on construction.
        _ = app  # sanity


class TestCreateSessionOnMissing:
    def test_creates_session_denial_reason_when_configured(self):
        session_reason = DenialReason(
            code="identity_verification_required",
            verify_url="https://agentscore.sh/verify/sess_abc",
            session_id="sess_abc",
            poll_secret="ps_secret",
            agent_instructions="please verify",
        )
        app = _make_app(
            "sanic_session_on_missing",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        with patch(
            "agentscore_commerce.identity.sanic.try_create_session_denial_reason",
            new=AsyncMock(return_value=session_reason),
        ):
            _, resp = app.test_client.get("/")
        assert resp.status == 403
        assert resp.json["error"]["code"] == "identity_verification_required"
        assert resp.json["session_id"] == "sess_abc"
        assert resp.json["verify_url"] == "https://agentscore.sh/verify/sess_abc"
        assert resp.json["poll_secret"] == "ps_secret"

    def test_falls_back_to_missing_identity_on_session_helper_failure(self):
        app = _make_app(
            "sanic_session_fail",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        with patch(
            "agentscore_commerce.identity.sanic.try_create_session_denial_reason",
            new=AsyncMock(return_value=None),  # helper returned None → fallback
        ):
            _, resp = app.test_client.get("/")
        assert resp.status == 403
        assert resp.json["error"]["code"] == "missing_identity"

    def test_fixable_wallet_denial_bootstraps_session(self):
        kyc_result = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw={})
        session_reason = DenialReason(
            code="identity_verification_required",
            verify_url="https://agentscore.sh/verify/sess_kyc",
            session_id="sess_kyc",
            poll_secret="ps_kyc",
        )
        app = _make_app(
            "sanic_fixable_wallet",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        with (
            patch(
                "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
                new=AsyncMock(return_value=kyc_result),
            ),
            patch(
                "agentscore_commerce.identity.sanic.try_create_session_denial_reason",
                new=AsyncMock(return_value=session_reason),
            ),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 403
        assert resp.json["error"]["code"] == "identity_verification_required"
        assert resp.json["session_id"] == "sess_kyc"

    def test_unfixable_wallet_denial_returns_bare_wallet_not_trusted(self):
        unfixable = AssessResult(allow=False, decision="deny", reasons=["sanctions_flagged"], raw={})
        session_helper = AsyncMock()
        app = _make_app(
            "sanic_unfixable_wallet",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        with (
            patch(
                "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
                new=AsyncMock(return_value=unfixable),
            ),
            patch(
                "agentscore_commerce.identity.sanic.try_create_session_denial_reason",
                new=session_helper,
            ),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 403
        assert resp.json["error"]["code"] == "wallet_not_trusted"
        session_helper.assert_not_called()


class TestCaptureWallet:
    def test_captures_when_operator_token_present(self):
        app = _make_app("sanic_capture_op")
        with (
            patch(
                "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
                new=AsyncMock(return_value=_allow_result()),
            ),
            patch(
                "agentscore_commerce.identity.sanic.AgentScoreCore.acapture_wallet",
                new=AsyncMock(),
            ) as mock_capture,
        ):
            _, resp = app.test_client.post("/purchase", headers={"X-Operator-Token": "opc_abc"})
            assert resp.status == 200
        mock_capture.assert_awaited_once_with(
            "opc_abc",
            "0xsigner",
            "evm",
            idempotency_key="pi_abc",
        )

    def test_no_ops_when_wallet_authenticated(self):
        app = _make_app("sanic_capture_wallet")
        with (
            patch(
                "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
                new=AsyncMock(return_value=_allow_result()),
            ),
            patch(
                "agentscore_commerce.identity.sanic.AgentScoreCore.acapture_wallet",
                new=AsyncMock(),
            ) as mock_capture,
        ):
            _, resp = app.test_client.post("/purchase", headers={"X-Wallet-Address": "0xwallet"})
            assert resp.status == 200
        mock_capture.assert_not_awaited()

    def test_no_ops_when_gate_did_not_run(self):
        # App without the gate middleware — capture_wallet must silently no-op.
        app = Sanic.get_app("sanic_no_gate", force_create=True)

        @app.post("/purchase")
        async def purchase(request):
            await capture_wallet(request, "0xsigner", "evm")
            return response.json({"ok": True})

        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acapture_wallet",
            new=AsyncMock(),
        ) as mock_capture:
            _, resp = app.test_client.post("/purchase")
            assert resp.status == 200
        mock_capture.assert_not_awaited()


def test_sanic_passes_through_token_expired():
    from agentscore_commerce.identity.core import TokenDeniedError

    app = Sanic("sanic_token_expired_test")
    agentscore_gate(app, api_key="ak", fail_open=False)

    @app.get("/")
    async def index(_request):
        return response.json({"ok": True})

    with patch(
        "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
        new=AsyncMock(
            side_effect=TokenDeniedError(
                {
                    "error": {"code": "token_expired", "message": "invalid"},
                    "session_id": "sess_abc",
                    "poll_secret": "poll_xyz",
                    "verify_url": "https://agentscore.sh/verify?session=sess_abc",
                    "next_steps": {"action": "deliver_verify_url_and_poll"},
                }
            )
        ),
    ):
        _req, resp = app.test_client.get("/", headers={"x-operator-token": "opc_exp"})

    assert resp.status == 401
    body = resp.json
    assert body["error"]["code"] == "token_expired"
    # Auto-session fields forwarded from the API's 401 body.
    assert body["session_id"] == "sess_abc"
    assert body["poll_secret"] == "poll_xyz"
    import json as _json

    assert _json.loads(body["agent_instructions"]) == {"action": "deliver_verify_url_and_poll"}


def test_sanic_api_error_on_unexpected_exception():
    app = Sanic("sanic_api_error_test")
    agentscore_gate(app, api_key="ak", fail_open=False)

    @app.get("/")
    async def index(_request):
        return response.json({"ok": True})

    with patch(
        "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
        new=AsyncMock(side_effect=RuntimeError("unexpected")),
    ):
        _req, resp = app.test_client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status == 503
    assert resp.json["error"]["code"] == "api_error"


def test_sanic_propagates_quota_from_assess_response():
    """API X-Quota-* → SDK populates AssessResponse.quota → adapter stashes onto ctx."""
    from agentscore_commerce.identity.sanic import get_gate_quota_info
    from agentscore_commerce.identity.types import GateQuotaInfo

    app = Sanic("sanic_quota_test")
    agentscore_gate(app, api_key="ak")
    captured: dict = {}

    @app.get("/")
    async def index(request):
        captured["quota"] = get_gate_quota_info(request)
        return response.json({"ok": True})

    result = AssessResult(
        allow=True,
        decision="allow",
        reasons=[],
        raw={"decision": "allow"},
        quota=GateQuotaInfo(limit=1500, used=1200, reset="2026-06-01T00:00:00Z"),
    )
    with patch(
        "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
        new=AsyncMock(return_value=result),
    ):
        _req, resp = app.test_client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status == 200
    assert captured["quota"] is not None
    assert captured["quota"].limit == 1500


class TestSanicBranchGaps:
    """Covers the remaining shared adapter branches for the Sanic gate."""

    def test_on_denied_three_tuple_sets_headers(self):
        def custom(_req, reason):
            return ({"code": reason.code}, 418, {"X-Custom": "teapot"})

        app = _make_app("sanic_gaps_3tuple", on_denied=custom)
        _, resp = app.test_client.get("/")  # no identity -> denied
        assert resp.status_code == 418
        assert resp.headers["X-Custom"] == "teapot"
        assert resp.json["code"] == "missing_identity"

    def test_recovered_signer_forwarded_to_assess(self):
        import base64
        import json as _json

        signer_addr = "0xabcdef0123456789abcdef0123456789abcdef01"
        x402_header = base64.b64encode(
            _json.dumps(
                {"accepted": {"network": "eip155:8453"}, "payload": {"authorization": {"from": signer_addr}}}
            ).encode()
        ).decode()
        app = _make_app("sanic_gaps_signer")
        mock = AsyncMock(return_value=_allow_result())
        with patch("agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity", new=mock):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xwallet", "X-Payment": x402_header})
            assert resp.status_code == 200
            assert mock.call_args.kwargs["signer"] == {"address": signer_addr, "network": "evm"}

    def test_condition_false_short_circuits(self):
        app = _make_app("sanic_gaps_condition", condition=lambda _req: False)
        with patch("agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity", new=AsyncMock()) as mock:
            _, resp = app.test_client.get("/")  # no identity, condition skips
            assert resp.status_code == 200
            mock.assert_not_called()

    def test_invalid_credential_returns_401(self):
        from agentscore_commerce.identity.core import InvalidCredentialError

        app = _make_app("sanic_gaps_invalidcred")
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=InvalidCredentialError()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Operator-Token": "opc_bad"})
            assert resp.status_code == 401
            assert resp.json["error"]["code"] == "invalid_credential"

    def test_timeout_fail_closed_returns_api_error(self):
        import httpx

        app = _make_app("sanic_gaps_timeout", fail_open=False)
        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(side_effect=httpx.TimeoutException("read timeout")),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status_code == 503
            assert resp.json["error"]["code"] == "api_error"

    def test_fixable_denial_falls_back_to_bare_when_session_returns_none(self):
        app = _make_app(
            "sanic_gaps_sessionnone",
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        with (
            patch(
                "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
                new=AsyncMock(return_value=_deny_result()),
            ),
            patch(
                "agentscore_commerce.identity.sanic.try_create_session_denial_reason",
                new=AsyncMock(return_value=None),
            ),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
            assert resp.status_code == 403
            assert resp.json["error"]["code"] == "wallet_not_trusted"

    def test_get_signer_verdict_none_when_no_client(self):
        from types import SimpleNamespace

        from agentscore_commerce.identity.sanic import GATE_STATE_ATTR, get_signer_verdict

        req = SimpleNamespace(ctx=SimpleNamespace(**{GATE_STATE_ATTR: {"wallet_address": "0xabc", "client": None}}))
        assert get_signer_verdict(req) is None

    def test_conditional_gate_discovery_leg_flows_through(self):
        from agentscore_commerce.identity.sanic import conditional_agentscore_gate

        app = Sanic.get_app("sanic_gaps_cond_discovery", force_create=True)
        conditional_agentscore_gate(app, api_key="ask_test", require_kyc=True)

        @app.post("/")
        async def _root(request):
            return response.json({"ok": True})

        with patch("agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity", new=AsyncMock()) as mock:
            _, resp = app.test_client.post("/")  # no payment header
            assert resp.status_code == 200
            mock.assert_not_called()

    def test_conditional_gate_settle_leg_gates(self):
        from agentscore_commerce.identity.sanic import conditional_agentscore_gate

        app = Sanic.get_app("sanic_gaps_cond_settle", force_create=True)
        conditional_agentscore_gate(app, api_key="ask_test", require_kyc=True)

        @app.post("/")
        async def _root(request):
            return response.json({"ok": True})

        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(return_value=_deny_result()),
        ):
            _, resp = app.test_client.post("/", headers={"X-Wallet-Address": "0xabc", "X-Payment": "abc"})
            assert resp.status_code == 403

    def test_get_signer_verdict_none_when_no_state(self):
        from types import SimpleNamespace

        from agentscore_commerce.identity.sanic import get_signer_verdict

        req = SimpleNamespace(ctx=SimpleNamespace())
        assert get_signer_verdict(req) is None

    def test_get_signer_verdict_reads_from_client(self):
        from agentscore_commerce.identity.sanic import get_signer_verdict

        app = Sanic.get_app("sanic_gaps_sv_read", force_create=True)
        agentscore_gate(app, api_key="ask_test")
        captured: dict = {}

        @app.get("/sv")
        async def _sv(request):
            captured["verdict"] = get_signer_verdict(request)
            return response.json({"ok": True})

        with patch(
            "agentscore_commerce.identity.sanic.AgentScoreCore.acheck_identity",
            new=AsyncMock(return_value=_allow_result()),
        ):
            _, resp = app.test_client.get("/sv", headers={"X-Wallet-Address": "0xsvread"})
            assert resp.status_code == 200
        assert captured["verdict"] is None
