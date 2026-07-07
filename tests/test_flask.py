"""Tests for the Flask integration."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from flask import Flask

from agentscore_commerce.identity.core import PaymentRequiredError, QuotaExceededError
from agentscore_commerce.identity.flask import agentscore_gate, get_agentscore_data
from agentscore_commerce.identity.types import AssessResult


def _make_app(**gate_kwargs: object) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    agentscore_gate(app, api_key="test-key", **gate_kwargs)

    @app.route("/")
    def index():
        from flask import g

        return {"ok": True, "agentscore": getattr(g, "agentscore", None)}

    return app


def _mock_result(allow: bool = True, decision: str | None = "allow") -> AssessResult:
    return AssessResult(allow=allow, decision=decision, reasons=[], raw={"score": 80, "grade": "B"})


class TestFlaskGate:
    """Flask adapter tests."""

    def test_allows_trusted_wallet(self) -> None:
        app = _make_app()
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=_mock_result()):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["agentscore"] is not None

    def test_get_agentscore_data_returns_assess_after_pass(self) -> None:
        app = Flask(__name__)
        app.config["TESTING"] = True
        agentscore_gate(app, api_key="test-key")

        @app.route("/")
        def index() -> dict[str, object]:
            return {"assess": get_agentscore_data()}

        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=_mock_result()):
            resp = app.test_client().get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            assert resp.get_json()["assess"] == {"score": 80, "grade": "B"}

    def test_get_agentscore_data_returns_none_outside_request(self) -> None:
        app = Flask(__name__)
        with app.app_context():
            assert get_agentscore_data() is None

    def test_blocks_untrusted_wallet(self) -> None:
        app = _make_app()
        result = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw={})
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=result):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 403
            data = resp.get_json()
            assert data["error"]["code"] == "wallet_not_trusted"

    def test_missing_wallet_returns_403(self) -> None:
        app = _make_app()
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["error"]["code"] == "missing_identity"

    def test_missing_wallet_fail_open(self) -> None:
        app = _make_app(fail_open=True)
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 200

    def test_api_error_fail_open(self) -> None:
        app = _make_app(fail_open=True)
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", side_effect=RuntimeError("timeout")):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200

    def test_api_error_fail_closed(self) -> None:
        app = _make_app()
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", side_effect=RuntimeError("timeout")):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 503
            data = resp.get_json()
            assert data["error"]["code"] == "api_error"

    def test_get_gate_degraded_state_default_returns_not_degraded(self) -> None:
        from agentscore_commerce.identity.flask import get_gate_degraded_state

        app = _make_app()
        captured: dict = {}

        @app.route("/snoop")
        def _snoop():
            captured.update(get_gate_degraded_state())
            return {"ok": True}

        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=_mock_result()):
            resp = app.test_client().get("/snoop", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            assert captured == {"degraded": False}

    def test_get_gate_degraded_state_returns_infra_reason_when_degraded(self) -> None:
        from agentscore_commerce.identity.flask import get_gate_degraded_state

        app = _make_app(fail_open=True)
        captured: dict = {}

        @app.route("/snoop")
        def _snoop():
            captured.update(get_gate_degraded_state())
            return {"ok": True}

        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=QuotaExceededError("quota_exceeded"),
        ):
            resp = app.test_client().get("/snoop", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            assert captured == {"degraded": True, "infra_reason": "quota_exceeded"}

    def test_quota_exceeded_fail_open_marks_degraded(self) -> None:
        """fail_open=True + QuotaExceededError → request flows through; gate state on
        ``g._agentscore_gate`` carries degraded=True + infra_reason='quota_exceeded'."""
        app = _make_app(fail_open=True)

        @app.route("/snoop")
        def _snoop():
            from flask import g, jsonify

            state = getattr(g, "_agentscore_gate", {}) or {}
            return jsonify({k: v for k, v in state.items() if k != "client"})

        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=QuotaExceededError("quota_exceeded"),
        ):
            client = app.test_client()
            resp = client.get("/snoop", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data.get("degraded") is True
            assert data.get("infra_reason") == "quota_exceeded"

    def test_quota_exceeded_fail_closed_returns_api_error(self) -> None:
        import json as _json

        app = _make_app()
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=QuotaExceededError("quota_exceeded"),
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 503
            body = resp.get_json()
            assert body["error"]["code"] == "api_error"
            instructions = _json.loads(body["agent_instructions"])
            assert instructions["action"] == "contact_merchant"
            assert "merchant-side issue" in instructions["steps"][0]

    def test_timeout_fail_open_marks_degraded_with_network_timeout(self) -> None:
        app = _make_app(fail_open=True)

        @app.route("/snoop")
        def _snoop():
            from flask import g, jsonify

            state = getattr(g, "_agentscore_gate", {}) or {}
            return jsonify({k: v for k, v in state.items() if k != "client"})

        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=httpx.TimeoutException("read timeout"),
        ):
            client = app.test_client()
            resp = client.get("/snoop", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data.get("degraded") is True
            assert data.get("infra_reason") == "network_timeout"

    def test_generic_exception_fail_open_marks_degraded_with_api_error(self) -> None:
        app = _make_app(fail_open=True)

        @app.route("/snoop")
        def _snoop():
            from flask import g, jsonify

            state = getattr(g, "_agentscore_gate", {}) or {}
            return jsonify({k: v for k, v in state.items() if k != "client"})

        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=RuntimeError("oops"),
        ):
            client = app.test_client()
            resp = client.get("/snoop", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data.get("degraded") is True
            assert data.get("infra_reason") == "api_error"

    def test_payment_required_fail_open(self) -> None:
        app = _make_app(fail_open=True)
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=PaymentRequiredError,
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200

    def test_payment_required_fail_closed(self) -> None:
        app = _make_app()
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=PaymentRequiredError,
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 403
            data = resp.get_json()
            assert data["error"]["code"] == "payment_required"

    def test_extract_chain_passed_to_api(self) -> None:
        def custom_extract_chain(_request):
            return "ethereum"

        app = _make_app(extract_chain=custom_extract_chain)
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check_identity", return_value=_mock_result()
        ) as mock_check:
            client = app.test_client()
            client.get("/", headers={"x-wallet-address": "0xabc"})
            call_args = mock_check.call_args
            assert call_args[0][0].address == "0xabc"
            assert call_args[0][1] == "ethereum"

    def test_custom_on_denied_returning_wrong_type(self) -> None:
        def bad_on_denied(_request, _reason):
            return "not-a-tuple"

        app = _make_app(on_denied=bad_on_denied)
        client = app.test_client()
        with pytest.raises(TypeError, match="on_denied must return a"):
            client.get("/")

    def test_requires_api_key(self) -> None:
        with pytest.raises(ValueError, match="API key is required"):
            app = Flask(__name__)
            agentscore_gate(app, api_key="")

    def test_compliance_params_passed_to_client(self) -> None:
        with patch("agentscore_commerce.identity.flask.AgentScoreCore") as mock_cls:
            mock_cls.return_value = mock_cls
            mock_cls.fail_open = False
            app = Flask(__name__)
            agentscore_gate(
                app,
                api_key="test-key",
                require_kyc=True,
                require_sanctions_clear=True,
                min_age=90,
                blocked_jurisdictions=["KP", "IR"],
            )
            call_kwargs = mock_cls.call_args[1]
            assert call_kwargs["require_kyc"] is True
            assert call_kwargs["require_sanctions_clear"] is True
            assert call_kwargs["min_age"] == 90
            assert call_kwargs["blocked_jurisdictions"] == ["KP", "IR"]

    def test_deny_includes_compliance_reasons(self) -> None:
        app = _make_app()
        result = AssessResult(
            allow=False,
            decision="deny",
            reasons=["kyc_required", "sanctions_flagged"],
            raw={
                "verify_url": "https://www.agentscore.com/verify/abc123",
                "operator_verification": {"level": "none"},
            },
        )
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=result):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 403
            data = resp.get_json()
            assert data["error"]["code"] == "wallet_not_trusted"
            assert "kyc_required" in data["reasons"]
            assert "sanctions_flagged" in data["reasons"]

    def test_allow_with_operator_verification_attaches_to_g(self) -> None:
        app = _make_app()
        raw = {
            "score": 80,
            "operator_verification": {
                "level": "kyc_verified",
                "operator_type": "business",
                "claimed_at": "2024-06-01T00:00:00Z",
                "verified_at": "2024-06-15T00:00:00Z",
            },
        }
        result = AssessResult(allow=True, decision="allow", reasons=[], raw=raw)
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=result):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["agentscore"]["operator_verification"]["level"] == "kyc_verified"

    def test_verify_url_available_in_raw_on_deny(self) -> None:
        app = _make_app()
        raw = {
            "decision": "deny",
            "verify_url": "https://www.agentscore.com/verify/abc123",
        }
        result = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw=raw)
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=result):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 403
            data = resp.get_json()
            assert data["error"]["code"] == "wallet_not_trusted"


class TestFlaskCreateSessionOnMissing:
    """Flask adapter's create_session_on_missing support."""

    def test_creates_session_and_returns_403_with_session_data(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing
        from agentscore_commerce.identity.types import DenialReason

        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        session_reason = DenialReason(
            code="identity_verification_required",
            verify_url="https://www.agentscore.com/verify/sess_abc",
            session_id="sess_abc",
            poll_secret="ps_secret",
            agent_instructions="please verify",
        )
        with patch(
            "agentscore_commerce.identity.flask.try_create_session_denial_reason_sync",
            return_value=session_reason,
        ):
            client = app.test_client()
            resp = client.get("/")
            assert resp.status_code == 403
            data = resp.get_json()
            assert data["error"]["code"] == "identity_verification_required"
            assert data["session_id"] == "sess_abc"
            assert data["verify_url"] == "https://www.agentscore.com/verify/sess_abc"
            assert data["poll_secret"] == "ps_secret"
            assert data["agent_instructions"] == "please verify"

    def test_falls_back_to_missing_identity_on_session_helper_failure(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing

        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        with patch(
            "agentscore_commerce.identity.flask.try_create_session_denial_reason_sync",
            return_value=None,
        ):
            client = app.test_client()
            resp = client.get("/")
            assert resp.status_code == 403
            data = resp.get_json()
            assert data["error"]["code"] == "missing_identity"

    def test_fixable_wallet_denial_bootstraps_session(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing
        from agentscore_commerce.identity.types import DenialReason

        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        result = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw={})
        session_reason = DenialReason(
            code="identity_verification_required",
            verify_url="https://www.agentscore.com/verify/sess_kyc",
            session_id="sess_kyc",
            poll_secret="ps_kyc",
        )
        with (
            patch(
                "agentscore_commerce.identity.flask.AgentScoreCore.check",
                return_value=result,
            ),
            patch(
                "agentscore_commerce.identity.flask.try_create_session_denial_reason_sync",
                return_value=session_reason,
            ),
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 403
            data = resp.get_json()
            assert data["error"]["code"] == "identity_verification_required"
            assert data["session_id"] == "sess_kyc"

    def test_unfixable_wallet_denial_returns_bare_wallet_not_trusted(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing

        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        result = AssessResult(allow=False, decision="deny", reasons=["sanctions_flagged"], raw={})
        with (
            patch(
                "agentscore_commerce.identity.flask.AgentScoreCore.check",
                return_value=result,
            ),
            patch(
                "agentscore_commerce.identity.flask.try_create_session_denial_reason_sync",
            ) as session_helper,
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 403
            data = resp.get_json()
            assert data["error"]["code"] == "wallet_not_trusted"
            session_helper.assert_not_called()


class TestFlaskIdentityModel:
    """Flask adapter identity model tests."""

    def test_default_extract_identity_returns_operator_token(self) -> None:
        from agentscore_commerce.identity.flask import _default_extract_identity

        class FakeRequest:
            headers = {"x-operator-token": "opc_flask", "x-wallet-address": "0xabc"}

        identity = _default_extract_identity(FakeRequest())
        assert identity is not None
        assert identity.operator_token == "opc_flask"
        assert identity.address == "0xabc"

    def test_default_extract_identity_address_only(self) -> None:
        from agentscore_commerce.identity.flask import _default_extract_identity

        class FakeRequest:
            headers = {"x-wallet-address": "0xabc"}

        identity = _default_extract_identity(FakeRequest())
        assert identity is not None
        assert identity.address == "0xabc"
        assert identity.operator_token is None

    def test_default_extract_identity_returns_none_when_empty(self) -> None:
        from agentscore_commerce.identity.flask import _default_extract_identity

        class FakeRequest:
            headers = {}

        identity = _default_extract_identity(FakeRequest())
        assert identity is None

    def test_missing_identity_returns_403(self) -> None:
        app = _make_app()
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["error"]["code"] == "missing_identity"

    def test_missing_identity_fail_open(self) -> None:
        app = _make_app(fail_open=True)
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 200

    def test_operator_token_header_calls_check_identity(self) -> None:
        app = _make_app()
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check_identity", return_value=_mock_result()
        ) as mock_check:
            client = app.test_client()
            resp = client.get("/", headers={"x-operator-token": "opc_flask_test"})
            assert resp.status_code == 200
            call_args = mock_check.call_args
            identity = call_args[0][0]
            assert identity.operator_token == "opc_flask_test"


def _make_capture_app() -> Flask:
    """Flask app whose handler calls capture_wallet so we can verify gate-state stash."""
    from agentscore_commerce.identity.flask import agentscore_gate as _install_gate
    from agentscore_commerce.identity.flask import capture_wallet as _capture

    app = Flask(__name__)
    app.config["TESTING"] = True
    _install_gate(app, api_key="test-key")

    @app.route("/purchase", methods=["POST"])
    def purchase():
        _capture("0xsigner", "evm", idempotency_key="pi_abc")
        return {"ok": True}

    return app


class TestFlaskCaptureWallet:
    def test_captures_when_operator_token_present(self) -> None:
        app = _make_capture_app()
        with (
            patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=_mock_result()),
            patch("agentscore_commerce.identity.flask.AgentScoreCore.capture_wallet") as mock_capture,
        ):
            client = app.test_client()
            resp = client.post("/purchase", headers={"x-operator-token": "opc_abc"})
            assert resp.status_code == 200
            mock_capture.assert_called_once_with(
                "opc_abc",
                "0xsigner",
                "evm",
                idempotency_key="pi_abc",
            )

    def test_no_ops_when_wallet_authenticated(self) -> None:
        app = _make_capture_app()
        with (
            patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=_mock_result()),
            patch("agentscore_commerce.identity.flask.AgentScoreCore.capture_wallet") as mock_capture,
        ):
            client = app.test_client()
            resp = client.post("/purchase", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
            mock_capture.assert_not_called()

    def test_no_ops_outside_request_context(self) -> None:
        """Calling capture_wallet without a Flask request context must not crash.

        Defensive: users who import capture_wallet into a background worker would otherwise
        see a RuntimeError from Flask's ``g`` proxy.
        """
        from agentscore_commerce.identity.flask import capture_wallet

        app = Flask(__name__)  # no gate registered
        # App context but no request context — Flask's `g` is only meaningful inside a request.
        with (
            app.app_context(),
            patch("agentscore_commerce.identity.flask.AgentScoreCore.capture_wallet") as mock_capture,
        ):
            capture_wallet("0xsigner", "evm")
            mock_capture.assert_not_called()


class TestFlaskUserAgent:
    """Flask adapter user_agent + default User-Agent header coverage."""

    def test_default_user_agent_format(self) -> None:
        import httpx
        import respx

        app = _make_app()

        with respx.mock:
            route = respx.post("https://api.agentscore.com/v1/assess").mock(
                return_value=httpx.Response(200, json={"decision": "allow", "decision_reasons": []}),
            )
            client = app.test_client()
            client.get("/", headers={"x-wallet-address": "0xabc"})
            assert route.called
            ua = route.calls[0].request.headers["User-Agent"]
            assert ua.startswith("agentscore-commerce/")

    def test_custom_user_agent_prepended(self) -> None:
        import httpx
        import respx

        app = _make_app(user_agent="myapp/2.0")

        with respx.mock:
            route = respx.post("https://api.agentscore.com/v1/assess").mock(
                return_value=httpx.Response(200, json={"decision": "allow", "decision_reasons": []}),
            )
            client = app.test_client()
            client.get("/", headers={"x-wallet-address": "0xabc"})
            ua = route.calls[0].request.headers["User-Agent"]
            assert ua.startswith("myapp/2.0 (agentscore-commerce/")


class TestFlaskChainOption:
    """Flask adapter chain= constructor option forwarding."""

    def test_constructor_chain_stored_and_forwarded(self) -> None:
        import json

        import httpx
        import respx

        app = _make_app(chain="solana")

        with respx.mock:
            route = respx.post("https://api.agentscore.com/v1/assess").mock(
                return_value=httpx.Response(200, json={"decision": "allow", "decision_reasons": []}),
            )
            client = app.test_client()
            client.get("/", headers={"x-wallet-address": "0xabc"})
            body = json.loads(route.calls[0].request.content)
            assert body["chain"] == "solana"


class TestFlaskTokenDenied:
    """Flask adapter handles granular 401 denial codes from /v1/assess."""

    def test_passes_through_token_expired_with_auto_session(self) -> None:
        # Revoked and expired credentials both surface as token_expired; adapter forwards
        # the API's auto-minted session fields into the 403 body.
        from agentscore_commerce.identity.core import TokenDeniedError

        app = _make_app()
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=TokenDeniedError(
                {
                    "error": {"code": "token_expired", "message": "invalid"},
                    "session_id": "sess_flask",
                    "poll_secret": "poll_flask",
                    "verify_url": "https://www.agentscore.com/verify?session=sess_flask",
                    "next_steps": {"action": "deliver_verify_url_and_poll"},
                }
            ),
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-operator-token": "opc_revoked"})

        assert resp.status_code == 401
        import json as _json

        body = resp.get_json()
        assert body["error"]["code"] == "token_expired"
        assert body["session_id"] == "sess_flask"
        assert body["poll_secret"] == "poll_flask"
        assert _json.loads(body["agent_instructions"]) == {"action": "deliver_verify_url_and_poll"}

    def test_passes_through_token_expired_without_next_steps(self) -> None:
        from agentscore_commerce.identity.core import TokenDeniedError

        app = _make_app()
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=TokenDeniedError({"error": {"code": "token_expired", "message": "invalid"}}),
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-operator-token": "opc_expired"})

        assert resp.status_code == 401
        body = resp.get_json()
        assert body["error"]["code"] == "token_expired"
        # API didn't supply next_steps → fallback agent_instructions injected by
        # _response.py so agents always have a recovery action.
        import json as _json

        assert _json.loads(body["agent_instructions"])["action"] == "deliver_verify_url_and_poll"


class TestFlaskGenericFailure:
    """Flask adapter emits a generic api_error on unexpected exceptions (fail-closed default)."""

    def test_api_error_on_connect_failure(self) -> None:
        import httpx

        app = _make_app()
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=httpx.ConnectError("dns lookup failed"),
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})

        assert resp.status_code == 503
        assert resp.get_json()["error"]["code"] == "api_error"

    def test_fail_open_lets_request_through_on_unexpected_exception(self) -> None:
        import httpx

        app = _make_app(fail_open=True)
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=httpx.ConnectError("dns lookup failed"),
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})

        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_payment_required_surfaces_as_denial(self) -> None:
        app = _make_app()
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=PaymentRequiredError,
        ):
            client = app.test_client()
            resp = client.get("/", headers={"x-wallet-address": "0xabc"})

        assert resp.status_code == 403
        assert resp.get_json()["error"]["code"] == "payment_required"


class TestFlaskBadOnDenied:
    """Flask adapter's _on_denied-return-shape guard: must raise a clear TypeError so
    merchants know their handler signature is wrong, not silently 500 with a cryptic error."""

    def test_missing_identity_branch_bad_on_denied_shape(self) -> None:
        app = _make_app(on_denied=lambda _req, _reason: "not a tuple")
        client = app.test_client()
        # No identity → missing_identity branch uses _on_denied which returns wrong shape.
        with pytest.raises(TypeError, match="on_denied must return"):
            client.get("/")

    def test_wallet_not_trusted_branch_bad_on_denied_shape(self) -> None:
        app = _make_app(on_denied=lambda _req, _reason: None)
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            return_value=_mock_result(allow=False, decision="deny"),
        ):
            client = app.test_client()
            with pytest.raises(TypeError, match="on_denied must return"):
                client.get("/", headers={"x-wallet-address": "0xabc"})

    def test_token_denied_branch_bad_on_denied_shape(self) -> None:
        from agentscore_commerce.identity.core import TokenDeniedError

        app = _make_app(on_denied=lambda _req, _reason: 42)
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=TokenDeniedError({"error": {"code": "token_expired"}}),
        ):
            client = app.test_client()
            with pytest.raises(TypeError, match="on_denied must return"):
                client.get("/", headers={"x-operator-token": "opc_exp"})

    def test_api_error_branch_bad_on_denied_shape(self) -> None:
        import httpx

        app = _make_app(on_denied=lambda _req, _reason: ({"x": 1},))  # single-element tuple
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=httpx.ConnectError("dns down"),
        ):
            client = app.test_client()
            with pytest.raises(TypeError, match="on_denied must return"):
                client.get("/", headers={"x-wallet-address": "0xabc"})


class TestFlaskGetSignerVerdictNoOp:
    """get_signer_verdict silently returns None when Flask state isn't available."""

    def test_no_op_outside_request_context(self) -> None:
        from agentscore_commerce.identity.flask import get_signer_verdict

        # Flask's g raises RuntimeError when accessed outside an app context.
        # The adapter catches and returns None, not propagate.
        assert get_signer_verdict() is None

    def test_no_op_when_gate_state_missing_in_request(self) -> None:
        from flask import Flask

        from agentscore_commerce.identity.flask import get_signer_verdict

        # App without the gate registered → g._agentscore_gate is absent.
        app = Flask(__name__)
        with app.test_request_context("/"):
            assert get_signer_verdict() is None


class TestFlaskCaptureWalletNoOp:
    """capture_wallet silently no-ops when state is missing or request is wallet-authenticated."""

    def test_no_op_outside_request_context(self) -> None:
        from agentscore_commerce.identity.flask import capture_wallet

        # Outside a Flask request, g.RuntimeError → return None silently.
        capture_wallet("0xwallet", "evm")

    def test_no_op_when_gate_state_missing(self) -> None:
        from flask import Flask

        from agentscore_commerce.identity.flask import capture_wallet

        app = Flask(__name__)
        with app.test_request_context("/"):
            capture_wallet("0xwallet", "evm")  # should not raise


class TestFlaskQuotaPropagation:
    def test_propagates_quota_from_assess_response(self) -> None:
        """API X-Quota-* → SDK populates AssessResponse.quota → adapter stashes onto g."""
        from agentscore_commerce.identity.flask import get_gate_quota_info
        from agentscore_commerce.identity.types import GateQuotaInfo

        app = _make_app()
        captured: dict = {}

        @app.route("/quota")
        def quota_route():
            captured["quota"] = get_gate_quota_info()
            return {"ok": True}

        result = AssessResult(
            allow=True,
            decision="allow",
            reasons=[],
            raw={"decision": "allow"},
            quota=GateQuotaInfo(limit=1500, used=1200, reset="2026-06-01T00:00:00Z"),
        )
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=result):
            client = app.test_client()
            resp = client.get("/quota", headers={"x-wallet-address": "0xabc"})
            assert resp.status_code == 200
        assert captured["quota"] is not None
        assert captured["quota"].limit == 1500


class TestFlaskBranchGaps:
    """Covers the remaining shared adapter branches for the Flask gate."""

    def test_on_denied_three_tuple_sets_headers(self) -> None:
        def custom(_req, reason):
            return ({"code": reason.code}, 418, {"X-Custom": "teapot"})

        app = _make_app(on_denied=custom)
        resp = app.test_client().get("/")  # no identity -> denied
        assert resp.status_code == 418
        assert resp.headers["X-Custom"] == "teapot"
        assert resp.get_json()["code"] == "missing_identity"

    def test_recovered_signer_forwarded_to_assess(self) -> None:
        import base64
        import json as _json

        signer_addr = "0xabcdef0123456789abcdef0123456789abcdef01"
        x402_header = base64.b64encode(
            _json.dumps(
                {"accepted": {"network": "eip155:8453"}, "payload": {"authorization": {"from": signer_addr}}}
            ).encode()
        ).decode()
        app = _make_app()
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check_identity", return_value=_mock_result()
        ) as mock_check:
            resp = app.test_client().get("/", headers={"x-wallet-address": "0xwallet", "x-payment": x402_header})
            assert resp.status_code == 200
            assert mock_check.call_args.kwargs["signer"] == {"address": signer_addr, "network": "evm"}

    def test_condition_false_short_circuits(self) -> None:
        app = _make_app(condition=lambda _req: False)
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check") as mock_check:
            resp = app.test_client().get("/")  # no identity, but condition skips gating
            assert resp.status_code == 200
            mock_check.assert_not_called()

    def test_invalid_credential_returns_401(self) -> None:
        from agentscore_commerce.identity.core import InvalidCredentialError

        app = _make_app()
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", side_effect=InvalidCredentialError()):
            resp = app.test_client().get("/", headers={"x-operator-token": "opc_bad"})
            assert resp.status_code == 401
            assert resp.get_json()["error"]["code"] == "invalid_credential"

    def test_timeout_fail_closed_returns_api_error(self) -> None:
        app = _make_app(fail_open=False)
        with patch(
            "agentscore_commerce.identity.flask.AgentScoreCore.check",
            side_effect=httpx.TimeoutException("read timeout"),
        ):
            resp = app.test_client().get("/", headers={"x-wallet-address": "0xtimeoutflask"})
            assert resp.status_code == 503
            assert resp.get_json()["error"]["code"] == "api_error"

    def test_fixable_denial_falls_back_to_bare_when_session_returns_none(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing

        app = _make_app(create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"))
        result = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw={})
        with (
            patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=result),
            patch(
                "agentscore_commerce.identity.flask.try_create_session_denial_reason_sync",
                return_value=None,
            ),
        ):
            resp = app.test_client().get("/", headers={"x-wallet-address": "0xkycflask"})
            assert resp.status_code == 403
            assert resp.get_json()["error"]["code"] == "wallet_not_trusted"

    def test_get_signer_verdict_reads_from_client(self) -> None:
        from agentscore_commerce.identity.flask import get_signer_verdict

        app = Flask(__name__)
        app.config["TESTING"] = True
        agentscore_gate(app, api_key="test-key")
        captured: dict = {}

        @app.route("/sv")
        def _sv() -> dict[str, object]:
            captured["verdict"] = get_signer_verdict()
            return {"ok": True}

        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=_mock_result()):
            resp = app.test_client().get("/sv", headers={"x-wallet-address": "0xsignerverdictflask"})
            assert resp.status_code == 200
        assert captured["verdict"] is None

    def test_conditional_gate_discovery_leg_flows_through(self) -> None:
        from agentscore_commerce.identity.flask import conditional_agentscore_gate

        app = Flask(__name__)
        app.config["TESTING"] = True
        conditional_agentscore_gate(app, api_key="test-key", require_kyc=True)

        @app.route("/", methods=["POST"])
        def _root() -> dict[str, object]:
            return {"ok": True}

        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check") as mock_check:
            resp = app.test_client().post("/")  # no payment header
            assert resp.status_code == 200
            mock_check.assert_not_called()

    def test_conditional_gate_settle_leg_gates(self) -> None:
        from agentscore_commerce.identity.flask import conditional_agentscore_gate

        app = Flask(__name__)
        app.config["TESTING"] = True
        conditional_agentscore_gate(app, api_key="test-key", require_kyc=True)

        @app.route("/", methods=["POST"])
        def _root() -> dict[str, object]:
            return {"ok": True}

        result = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw={})
        with patch("agentscore_commerce.identity.flask.AgentScoreCore.check", return_value=result):
            resp = app.test_client().post("/", headers={"x-wallet-address": "0xabc", "x-payment": "abc"})
            assert resp.status_code == 403

    def test_get_signer_verdict_none_when_no_client(self) -> None:
        """Defensive guard: gate state with a wallet_address but client=None → None."""
        from flask import Flask, g

        from agentscore_commerce.identity.flask import get_signer_verdict

        app = Flask(__name__)
        with app.test_request_context("/"):
            g._agentscore_gate = {"wallet_address": "0xabc", "client": None}
            assert get_signer_verdict() is None
