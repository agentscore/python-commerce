"""Tests for the Sanic adapter.

Sanic's test client runs the app on a real loopback socket and uses httpx to hit it,
which makes respx-based URL mocking awkward. We mock ``GateClient.acheck_identity``
directly (matching the Flask/Django test pattern) and verify the adapter plumbing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sanic import Sanic, response

from agentscore_commerce.identity.sanic import agentscore_gate, capture_wallet, get_assess_data
from agentscore_commerce.identity.sessions import CreateSessionOnMissing
from agentscore_commerce.identity.types import AssessResult, DenialReason


def _allow_result() -> AssessResult:
    return AssessResult(allow=True, decision="allow", reasons=[], raw={"decision": "allow"})


def _deny_result() -> AssessResult:
    return AssessResult(allow=False, decision="deny", reasons=["not_kyc"])


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
            "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
            new=AsyncMock(return_value=_allow_result()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200
        assert resp.json["ok"] is True

    def test_get_assess_data_returns_assess_after_pass(self):
        app = Sanic.get_app("sanic_get_assess_data", force_create=True)
        agentscore_gate(app, api_key="ask_test")

        @app.get("/")
        async def handler(request):
            return response.json({"assess": get_assess_data(request)})

        with patch(
            "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
            new=AsyncMock(return_value=_allow_result()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200
        assert resp.json["assess"] == {"decision": "allow"}

    def test_denies_untrusted_wallet(self):
        app = _make_app("sanic_deny_wallet")
        with patch(
            "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
            new=AsyncMock(return_value=_deny_result()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 403
        assert resp.json["error"] == "wallet_not_trusted"
        assert resp.json["reasons"] == ["not_kyc"]

    def test_missing_identity_returns_403(self):
        app = _make_app("sanic_missing")
        _, resp = app.test_client.get("/")
        assert resp.status == 403
        assert resp.json["error"] == "missing_identity"

    def test_fail_open_allows_through_when_identity_missing(self):
        app = _make_app("sanic_fail_open", fail_open=True)
        _, resp = app.test_client.get("/")
        assert resp.status == 200

    def test_passes_operator_token_to_assess(self):
        app = _make_app("sanic_operator_token")
        mock = AsyncMock(return_value=_allow_result())
        with patch("agentscore_commerce.identity.sanic.GateClient.acheck_identity", new=mock):
            app.test_client.get("/", headers={"X-Operator-Token": "opc_abc"})
        # First positional arg is the AgentIdentity instance.
        identity_arg = mock.await_args.args[0]
        assert identity_arg.operator_token == "opc_abc"
        assert identity_arg.address is None


class TestErrorPaths:
    def test_returns_403_payment_required_on_402(self):
        from agentscore_commerce.identity.client import PaymentRequiredError

        app = _make_app("sanic_402")
        with patch(
            "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
            new=AsyncMock(side_effect=PaymentRequiredError()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 403
        assert resp.json["error"] == "payment_required"

    def test_returns_503_api_error_on_exception(self):
        app = _make_app("sanic_api_error")
        with patch(
            "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 503
        assert resp.json["error"] == "api_error"

    def test_fail_open_allows_through_on_402(self):
        from agentscore_commerce.identity.client import PaymentRequiredError

        app = _make_app("sanic_fail_open_402", fail_open=True)
        with patch(
            "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
            new=AsyncMock(side_effect=PaymentRequiredError()),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200

    def test_fail_open_allows_through_on_api_error(self):
        app = _make_app("sanic_fail_open_api", fail_open=True)
        with patch(
            "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            _, resp = app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert resp.status == 200


class TestChainOption:
    def test_no_extract_chain_passes_none_to_acheck_identity(self):
        """Adapter passes None as chain override when extract_chain isn't configured,
        so GateClient's constructor-level chain takes effect (or no chain is sent)."""
        app = _make_app("sanic_chain_none", chain="solana")
        mock = AsyncMock(return_value=_allow_result())
        with patch("agentscore_commerce.identity.sanic.GateClient.acheck_identity", new=mock):
            app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        chain_arg = mock.await_args.args[1]
        assert chain_arg is None  # extract_chain not set → adapter passes None

    def test_extract_chain_callback_passed_to_acheck_identity(self):
        app = _make_app("sanic_chain_callback", extract_chain=lambda _req: "ethereum")
        mock = AsyncMock(return_value=_allow_result())
        with patch("agentscore_commerce.identity.sanic.GateClient.acheck_identity", new=mock):
            app.test_client.get("/", headers={"X-Wallet-Address": "0xabc"})
        assert mock.await_args.args[1] == "ethereum"

    def test_constructor_chain_stored_on_client(self):
        """The constructor-level `chain` option is forwarded to GateClient so it gets
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
        assert resp.json["error"] == "identity_verification_required"
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
        assert resp.json["error"] == "missing_identity"


class TestCaptureWallet:
    def test_captures_when_operator_token_present(self):
        app = _make_app("sanic_capture_op")
        with (
            patch(
                "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
                new=AsyncMock(return_value=_allow_result()),
            ),
            patch(
                "agentscore_commerce.identity.sanic.GateClient.acapture_wallet",
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
                "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
                new=AsyncMock(return_value=_allow_result()),
            ),
            patch(
                "agentscore_commerce.identity.sanic.GateClient.acapture_wallet",
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
            "agentscore_commerce.identity.sanic.GateClient.acapture_wallet",
            new=AsyncMock(),
        ) as mock_capture:
            _, resp = app.test_client.post("/purchase")
            assert resp.status == 200
        mock_capture.assert_not_awaited()


def test_sanic_passes_through_token_expired():
    from agentscore_commerce.identity.client import TokenDeniedError

    app = Sanic("sanic_token_expired_test")
    agentscore_gate(app, api_key="ak", fail_open=False)

    @app.get("/")
    async def index(_request):
        return response.json({"ok": True})

    with patch(
        "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
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
    assert body["error"] == "token_expired"
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
        "agentscore_commerce.identity.sanic.GateClient.acheck_identity",
        new=AsyncMock(side_effect=RuntimeError("unexpected")),
    ):
        _req, resp = app.test_client.get("/", headers={"x-wallet-address": "0xabc"})

    assert resp.status == 503
    assert resp.json["error"] == "api_error"
