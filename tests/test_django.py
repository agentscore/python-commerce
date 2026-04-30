"""Tests for the Django integration."""

from __future__ import annotations

import json
from unittest.mock import patch

import django
from django.conf import settings

# Configure Django before importing anything else.
if not settings.configured:
    settings.configure(
        AGENTSCORE_GATE={"api_key": "test-key"},
        MIDDLEWARE=[],
        ROOT_URLCONF="tests.test_django",
        SECRET_KEY="test-secret",
    )
    django.setup()

from django.http import HttpRequest, JsonResponse
from django.test import RequestFactory

from agentscore_commerce.identity.client import PaymentRequiredError
from agentscore_commerce.identity.django import AgentScoreMiddleware, get_assess_data
from agentscore_commerce.identity.types import AssessResult

# Minimal URL conf for Django test runner.
urlpatterns: list = []


def _ok_response(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True, "agentscore": getattr(request, "agentscore", None)})


def _mock_result(allow: bool = True, decision: str | None = "allow") -> AssessResult:
    return AssessResult(allow=allow, decision=decision, reasons=[], raw={"score": 80, "grade": "B"})


class TestDjangoMiddleware:
    """Django middleware tests."""

    factory = RequestFactory()

    def _make_middleware(self, **config_overrides: object) -> AgentScoreMiddleware:
        original = settings.AGENTSCORE_GATE.copy()
        settings.AGENTSCORE_GATE = {**original, **config_overrides}
        try:
            return AgentScoreMiddleware(_ok_response)
        finally:
            settings.AGENTSCORE_GATE = original

    def test_allows_trusted_wallet(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", return_value=_mock_result()):
            resp = mw(request)
            assert resp.status_code == 200
            data = json.loads(resp.content)
            assert data["ok"] is True

    def test_blocks_untrusted_wallet(self) -> None:
        mw = self._make_middleware()
        result = AssessResult(allow=False, decision="deny", reasons=["score_too_low"], raw={})
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", return_value=result):
            resp = mw(request)
            assert resp.status_code == 403
            data = json.loads(resp.content)
            assert data["error"]["code"] == "wallet_not_trusted"

    def test_missing_wallet_returns_403(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/")
        resp = mw(request)
        assert resp.status_code == 403
        data = json.loads(resp.content)
        assert data["error"]["code"] == "missing_identity"

    def test_missing_wallet_fail_open(self) -> None:
        mw = self._make_middleware(fail_open=True)
        request = self.factory.get("/")
        resp = mw(request)
        assert resp.status_code == 200

    def test_api_error_fail_open(self) -> None:
        mw = self._make_middleware(fail_open=True)
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", side_effect=RuntimeError("timeout")):
            resp = mw(request)
            assert resp.status_code == 200

    def test_api_error_fail_closed(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", side_effect=RuntimeError("timeout")):
            resp = mw(request)
            assert resp.status_code == 503
            data = json.loads(resp.content)
            assert data["error"]["code"] == "api_error"

    def test_payment_required_fail_open(self) -> None:
        mw = self._make_middleware(fail_open=True)
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", side_effect=PaymentRequiredError):
            resp = mw(request)
            assert resp.status_code == 200

    def test_payment_required_fail_closed(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", side_effect=PaymentRequiredError):
            resp = mw(request)
            assert resp.status_code == 403
            data = json.loads(resp.content)
            assert data["error"]["code"] == "payment_required"

    def test_extract_chain_passed_to_api(self) -> None:
        def custom_extract_chain(_request):
            return "ethereum"

        mw = self._make_middleware(extract_chain=custom_extract_chain)
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch(
            "agentscore_commerce.identity.django.GateClient.check_identity", return_value=_mock_result()
        ) as mock_check:
            mw(request)
            call_args = mock_check.call_args
            assert call_args[0][0].address == "0xabc"
            assert call_args[0][1] == "ethereum"

    def test_null_decision_allows_request(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        result = AssessResult(allow=True, decision=None, reasons=[], raw={"score": 75})
        with patch("agentscore_commerce.identity.django.GateClient.check", return_value=result):
            resp = mw(request)
            assert resp.status_code == 200

    def test_attaches_data_to_request(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", return_value=_mock_result()):
            mw(request)
            assert hasattr(request, "agentscore")
            assert request.agentscore["score"] == 80  # type: ignore[attr-defined]

    def test_get_assess_data_returns_assess_after_pass(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", return_value=_mock_result()):
            mw(request)
            assert get_assess_data(request) == {"score": 80, "grade": "B"}

    def test_get_assess_data_returns_none_for_ungated_request(self) -> None:
        request = self.factory.get("/")
        assert get_assess_data(request) is None

    def test_compliance_params_passed_to_client(self) -> None:
        with patch("agentscore_commerce.identity.django.GateClient") as mock_cls:
            mock_cls.return_value = mock_cls
            mock_cls.fail_open = False
            mock_cls.check.return_value = _mock_result()
            self._make_middleware(
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

    def test_deny_includes_reasons_from_compliance(self) -> None:
        mw = self._make_middleware()
        result = AssessResult(
            allow=False,
            decision="deny",
            reasons=["kyc_required", "sanctions_check_pending"],
            raw={
                "verify_url": "https://agentscore.sh/verify/abc123",
                "operator_verification": {"level": "none"},
            },
        )
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", return_value=result):
            resp = mw(request)
            assert resp.status_code == 403
            data = json.loads(resp.content)
            assert data["error"]["code"] == "wallet_not_trusted"
            assert "kyc_required" in data["reasons"]
            assert "sanctions_check_pending" in data["reasons"]

    def test_allow_with_operator_verification_attaches_to_request(self) -> None:
        mw = self._make_middleware()
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
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", return_value=result):
            resp = mw(request)
            assert resp.status_code == 200
            assert request.agentscore["operator_verification"]["level"] == "kyc_verified"  # type: ignore[attr-defined]

    def test_verify_url_available_in_raw_on_deny(self) -> None:
        mw = self._make_middleware()
        raw = {
            "decision": "deny",
            "verify_url": "https://agentscore.sh/verify/abc123",
        }
        result = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw=raw)
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with patch("agentscore_commerce.identity.django.GateClient.check", return_value=result):
            resp = mw(request)
            assert resp.status_code == 403
            data = json.loads(resp.content)
            assert data["error"]["code"] == "wallet_not_trusted"


class TestDjangoCreateSessionOnMissing:
    """Django middleware's create_session_on_missing support."""

    factory = RequestFactory()

    def _make_middleware(self, **config_overrides: object) -> AgentScoreMiddleware:
        original = settings.AGENTSCORE_GATE.copy()
        settings.AGENTSCORE_GATE = {**original, **config_overrides}
        try:
            return AgentScoreMiddleware(_ok_response)
        finally:
            settings.AGENTSCORE_GATE = original

    def test_creates_session_and_returns_403_with_session_data(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing
        from agentscore_commerce.identity.types import DenialReason

        session_reason = DenialReason(
            code="identity_verification_required",
            verify_url="https://agentscore.sh/verify/sess_abc",
            session_id="sess_abc",
            poll_secret="ps_secret",
            agent_instructions="please verify",
        )
        mw = self._make_middleware(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        request = self.factory.get("/")
        with patch(
            "agentscore_commerce.identity.django.try_create_session_denial_reason_sync",
            return_value=session_reason,
        ):
            resp = mw(request)
        assert resp.status_code == 403
        data = json.loads(resp.content)
        assert data["error"]["code"] == "identity_verification_required"
        assert data["session_id"] == "sess_abc"
        assert data["verify_url"] == "https://agentscore.sh/verify/sess_abc"
        assert data["poll_secret"] == "ps_secret"
        assert data["agent_instructions"] == "please verify"

    def test_falls_back_to_missing_identity_on_session_helper_failure(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing

        mw = self._make_middleware(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        request = self.factory.get("/")
        with patch(
            "agentscore_commerce.identity.django.try_create_session_denial_reason_sync",
            return_value=None,
        ):
            resp = mw(request)
        assert resp.status_code == 403
        data = json.loads(resp.content)
        assert data["error"]["code"] == "missing_identity"

    def test_fixable_wallet_denial_bootstraps_session(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing
        from agentscore_commerce.identity.types import DenialReason

        mw = self._make_middleware(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        result = AssessResult(allow=False, decision="deny", reasons=["kyc_required"], raw={})
        session_reason = DenialReason(
            code="identity_verification_required",
            verify_url="https://agentscore.sh/verify/sess_kyc",
            session_id="sess_kyc",
            poll_secret="ps_kyc",
        )
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with (
            patch(
                "agentscore_commerce.identity.django.GateClient.check",
                return_value=result,
            ),
            patch(
                "agentscore_commerce.identity.django.try_create_session_denial_reason_sync",
                return_value=session_reason,
            ),
        ):
            resp = mw(request)
        assert resp.status_code == 403
        data = json.loads(resp.content)
        assert data["error"]["code"] == "identity_verification_required"
        assert data["session_id"] == "sess_kyc"

    def test_unfixable_wallet_denial_returns_bare_wallet_not_trusted(self) -> None:
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing

        mw = self._make_middleware(
            create_session_on_missing=CreateSessionOnMissing(api_key="ask_session"),
        )
        result = AssessResult(allow=False, decision="deny", reasons=["sanctions_flagged"], raw={})
        request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
        with (
            patch(
                "agentscore_commerce.identity.django.GateClient.check",
                return_value=result,
            ),
            patch(
                "agentscore_commerce.identity.django.try_create_session_denial_reason_sync",
            ) as session_helper,
        ):
            resp = mw(request)
        assert resp.status_code == 403
        data = json.loads(resp.content)
        assert data["error"]["code"] == "wallet_not_trusted"
        session_helper.assert_not_called()


class TestDjangoIdentityModel:
    """Django middleware identity model tests."""

    factory = RequestFactory()

    def _make_middleware(self, **config_overrides: object) -> AgentScoreMiddleware:
        original = settings.AGENTSCORE_GATE.copy()
        settings.AGENTSCORE_GATE = {**original, **config_overrides}
        try:
            return AgentScoreMiddleware(_ok_response)
        finally:
            settings.AGENTSCORE_GATE = original

    def test_default_extract_identity_returns_operator_token(self) -> None:
        identity = AgentScoreMiddleware._default_extract_identity(
            self.factory.get("/", HTTP_X_OPERATOR_TOKEN="opc_django", HTTP_X_WALLET_ADDRESS="0xabc")
        )
        assert identity is not None
        assert identity.operator_token == "opc_django"
        assert identity.address == "0xabc"

    def test_default_extract_identity_address_only(self) -> None:
        identity = AgentScoreMiddleware._default_extract_identity(self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc"))
        assert identity is not None
        assert identity.address == "0xabc"
        assert identity.operator_token is None

    def test_default_extract_identity_returns_none_when_empty(self) -> None:
        identity = AgentScoreMiddleware._default_extract_identity(self.factory.get("/"))
        assert identity is None

    def test_missing_identity_returns_403(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/")
        resp = mw(request)
        assert resp.status_code == 403
        data = json.loads(resp.content)
        assert data["error"]["code"] == "missing_identity"

    def test_missing_identity_fail_open(self) -> None:
        mw = self._make_middleware(fail_open=True)
        request = self.factory.get("/")
        resp = mw(request)
        assert resp.status_code == 200

    def test_operator_token_header_calls_check_identity(self) -> None:
        mw = self._make_middleware()
        request = self.factory.get("/", HTTP_X_OPERATOR_TOKEN="opc_django_test")
        with patch(
            "agentscore_commerce.identity.django.GateClient.check_identity", return_value=_mock_result()
        ) as mock_check:
            resp = mw(request)
            assert resp.status_code == 200
            call_args = mock_check.call_args
            identity = call_args[0][0]
            assert identity.operator_token == "opc_django_test"


class TestDjangoCaptureWallet:
    factory = RequestFactory()

    def _make_middleware(self) -> AgentScoreMiddleware:
        return AgentScoreMiddleware(_ok_response)

    def test_captures_when_operator_token_present(self) -> None:
        from agentscore_commerce.identity.django import capture_wallet

        mw = self._make_middleware()
        request = self.factory.post("/purchase", HTTP_X_OPERATOR_TOKEN="opc_django_cap")
        with (
            patch("agentscore_commerce.identity.django.GateClient.check", return_value=_mock_result()),
            patch("agentscore_commerce.identity.django.GateClient.capture_wallet") as mock_capture,
        ):
            mw(request)
            capture_wallet(request, "0xsigner", "evm", idempotency_key="pi_abc")
            mock_capture.assert_called_once_with(
                "opc_django_cap",
                "0xsigner",
                "evm",
                idempotency_key="pi_abc",
            )

    def test_no_ops_when_wallet_authenticated(self) -> None:
        from agentscore_commerce.identity.django import capture_wallet

        mw = self._make_middleware()
        request = self.factory.post("/purchase", HTTP_X_WALLET_ADDRESS="0xabc")
        with (
            patch("agentscore_commerce.identity.django.GateClient.check", return_value=_mock_result()),
            patch("agentscore_commerce.identity.django.GateClient.capture_wallet") as mock_capture,
        ):
            mw(request)
            capture_wallet(request, "0xsigner", "evm")
            mock_capture.assert_not_called()

    def test_no_ops_when_gate_did_not_run(self) -> None:
        from agentscore_commerce.identity.django import capture_wallet

        # A handler calling capture_wallet without the gate middleware ever running.
        request = self.factory.post("/purchase")
        with patch("agentscore_commerce.identity.django.GateClient.capture_wallet") as mock_capture:
            capture_wallet(request, "0xsigner", "evm")
            mock_capture.assert_not_called()


class TestDjangoUserAgent:
    """Django middleware user_agent + default User-Agent header coverage."""

    factory = RequestFactory()

    def _make_middleware(self, **config_overrides: object) -> AgentScoreMiddleware:
        original = settings.AGENTSCORE_GATE.copy()
        settings.AGENTSCORE_GATE = {**original, **config_overrides}
        try:
            return AgentScoreMiddleware(_ok_response)
        finally:
            settings.AGENTSCORE_GATE = original

    def test_default_user_agent_format(self) -> None:
        import httpx
        import respx

        mw = self._make_middleware()

        with respx.mock:
            route = respx.post("https://api.agentscore.sh/v1/assess").mock(
                return_value=httpx.Response(200, json={"decision": "allow", "decision_reasons": []}),
            )
            request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
            mw(request)
            assert route.called
            ua = route.calls[0].request.headers["User-Agent"]
            assert ua.startswith("agentscore-commerce/")

    def test_custom_user_agent_prepended(self) -> None:
        import httpx
        import respx

        mw = self._make_middleware(user_agent="myapp/2.0")

        with respx.mock:
            route = respx.post("https://api.agentscore.sh/v1/assess").mock(
                return_value=httpx.Response(200, json={"decision": "allow", "decision_reasons": []}),
            )
            request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
            mw(request)
            ua = route.calls[0].request.headers["User-Agent"]
            assert ua.startswith("myapp/2.0 (agentscore-commerce/")


class TestDjangoChainOption:
    """Django middleware chain= constructor option forwarding."""

    factory = RequestFactory()

    def _make_middleware(self, **config_overrides: object) -> AgentScoreMiddleware:
        original = settings.AGENTSCORE_GATE.copy()
        settings.AGENTSCORE_GATE = {**original, **config_overrides}
        try:
            return AgentScoreMiddleware(_ok_response)
        finally:
            settings.AGENTSCORE_GATE = original

    def test_constructor_chain_stored_and_forwarded(self) -> None:
        import json

        import httpx
        import respx

        mw = self._make_middleware(chain="solana")

        with respx.mock:
            route = respx.post("https://api.agentscore.sh/v1/assess").mock(
                return_value=httpx.Response(200, json={"decision": "allow", "decision_reasons": []}),
            )
            request = self.factory.get("/", HTTP_X_WALLET_ADDRESS="0xabc")
            mw(request)
            body = json.loads(route.calls[0].request.content)
            assert body["chain"] == "solana"
