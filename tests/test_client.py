"""Direct unit tests for GateClient internals."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from agentscore_commerce.identity.client import GateClient, PaymentRequiredError


def _make_client(**kwargs) -> GateClient:
    defaults = {"api_key": "ask_test_key"}
    defaults.update(kwargs)
    return GateClient(**defaults)


class TestHeaders:
    def test_includes_user_agent(self):
        client = _make_client()
        headers = client._headers()
        assert "User-Agent" in headers
        assert "agentscore" in headers["User-Agent"].lower()

    def test_default_user_agent_format(self):
        from importlib.metadata import version

        client = _make_client()
        headers = client._headers()
        assert headers["User-Agent"] == f"agentscore-commerce/{version('agentscore-commerce')}"

    def test_custom_user_agent_prepended_to_default(self):
        from importlib.metadata import version

        client = _make_client(user_agent="my-app/1.2.3")
        headers = client._headers()
        expected = f"my-app/1.2.3 (agentscore-commerce/{version('agentscore-commerce')})"
        assert headers["User-Agent"] == expected

    def test_includes_api_key_header(self):
        client = _make_client(api_key="ask_my_secret")
        headers = client._headers()
        assert headers["X-API-Key"] == "ask_my_secret"


class TestCaptureWallet:
    @respx.mock
    def test_posts_to_wallets_endpoint_with_snake_case_body(self):
        route = respx.post("https://api.agentscore.sh/v1/credentials/wallets").mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )
        client = _make_client()
        client.capture_wallet("opc_abc", "0xsigner", "evm", idempotency_key="pi_1")
        assert route.called
        body = json.loads(route.calls[0].request.content.decode())
        assert body == {
            "operator_token": "opc_abc",
            "wallet_address": "0xsigner",
            "network": "evm",
            "idempotency_key": "pi_1",
        }

    @respx.mock
    def test_omits_idempotency_key_when_empty_or_none(self):
        route = respx.post("https://api.agentscore.sh/v1/credentials/wallets").mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )
        client = _make_client()
        client.capture_wallet("opc_abc", "0xsigner", "evm")
        client.capture_wallet("opc_abc", "0xsigner", "evm", idempotency_key="")
        for call in route.calls:
            body = json.loads(call.request.content.decode())
            assert "idempotency_key" not in body

    @respx.mock
    def test_swallows_server_errors_silently(self):
        respx.post("https://api.agentscore.sh/v1/credentials/wallets").mock(
            return_value=httpx.Response(500, json={"error": {"code": "internal_error"}}),
        )
        client = _make_client()
        # Must not raise even on 5xx
        client.capture_wallet("opc_abc", "0xsigner", "evm")

    @respx.mock
    def test_swallows_network_errors_silently(self):
        respx.post("https://api.agentscore.sh/v1/credentials/wallets").mock(
            side_effect=httpx.ConnectError("boom"),
        )
        client = _make_client()
        client.capture_wallet("opc_abc", "0xsigner", "evm")


class TestCaptureWalletAsync:
    @pytest.mark.asyncio
    @respx.mock
    async def test_async_variant_posts_to_wallets_endpoint(self):
        route = respx.post("https://api.agentscore.sh/v1/credentials/wallets").mock(
            return_value=httpx.Response(200, json={"associated": True, "first_seen": True}),
        )
        client = _make_client()
        await client.acapture_wallet("opc_abc", "0xsigner", "solana", idempotency_key="tx_hash")
        assert route.called
        body = json.loads(route.calls[0].request.content.decode())
        assert body == {
            "operator_token": "opc_abc",
            "wallet_address": "0xsigner",
            "network": "solana",
            "idempotency_key": "tx_hash",
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_async_swallows_errors_silently(self):
        respx.post("https://api.agentscore.sh/v1/credentials/wallets").mock(
            side_effect=httpx.ConnectError("boom"),
        )
        client = _make_client()
        await client.acapture_wallet("opc_abc", "0xsigner", "evm")


class TestCacheKey:
    def test_lowercases_address(self):
        client = _make_client()
        key = client._cache_key("0xABCDEF")
        assert key == "0xabcdef"

    def test_address_only(self):
        client = _make_client()
        key = client._cache_key("0xABC")
        assert key == "0xabc"


class TestParseResponse:
    def test_missing_decision_allows(self):
        """When the decision field is absent, the request should be allowed."""
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {"score": 80}

        result = client._parse_response(resp)
        assert result.allow is True

    def test_deny_decision(self):
        """When decision is 'deny', result.allow should be False."""
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {
            "decision": "deny",
            "decision_reasons": ["low_score"],
        }

        result = client._parse_response(resp)
        assert result.allow is False
        assert result.decision == "deny"
        assert "low_score" in result.reasons


class TestBuildBody:
    def test_includes_policy_when_set(self):
        client = _make_client(require_kyc=True)
        body = client._build_body("0xabc")
        assert body["address"] == "0xabc"
        assert "chain" not in body
        assert "policy" in body
        assert body["policy"]["require_kyc"] is True

    def test_no_policy_when_empty(self):
        client = _make_client()
        body = client._build_body("0xabc")
        assert "policy" not in body
        assert "chain" not in body


class TestParseResponse402:
    def test_raises_payment_required_error(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 402
        with pytest.raises(PaymentRequiredError):
            client._parse_response(resp)


class TestParseResponseStatusCodes:
    """Cover the bespoke 401/429/5xx branches in _parse_response.

    Production traffic flows through the SDK (which raises typed errors directly),
    but the helper is preserved for tests that pin the gate's contract independently.
    """

    def test_429_raises_quota_exceeded(self):
        from agentscore_commerce.identity.client import QuotaExceededError

        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        with pytest.raises(QuotaExceededError):
            client._parse_response(resp)

    def test_401_token_expired_raises_token_denied(self):
        from agentscore_commerce.identity.client import TokenDeniedError

        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.json.return_value = {
            "error": {"code": "token_expired"},
            "verify_url": "https://x",
            "session_id": "s",
            "poll_secret": "p",
        }
        with pytest.raises(TokenDeniedError) as info:
            client._parse_response(resp)
        assert info.value.body.get("verify_url") == "https://x"

    def test_401_invalid_credential_raises_invalid_credential(self):
        from agentscore_commerce.identity.client import InvalidCredentialError

        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.json.return_value = {"error": {"code": "invalid_credential"}}
        with pytest.raises(InvalidCredentialError):
            client._parse_response(resp)

    def test_401_unknown_code_raises_runtime_error(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.json.return_value = {"error": {"code": "future_drift"}}
        with pytest.raises(RuntimeError, match="returned 401"):
            client._parse_response(resp)

    def test_401_body_unparseable_raises_runtime_error(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.json.side_effect = ValueError("not json")
        with pytest.raises(RuntimeError, match="returned 401"):
            client._parse_response(resp)

    def test_5xx_raises_runtime_error(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 503
        resp.is_success = False
        with pytest.raises(RuntimeError, match="returned 503"):
            client._parse_response(resp)


ASSESS_URL = "https://api.agentscore.sh/v1/assess"


class TestCheckWithChain:
    @respx.mock
    def test_check_sends_chain_override(self):
        client = _make_client()
        route = respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_reasons": []},
            )
        )

        client.check("0xABC", chain="ethereum")

        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert body["chain"] == "ethereum"

    @respx.mock
    def test_check_uses_client_chain_when_no_override(self):
        client = _make_client(chain="base")
        route = respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_reasons": []},
            )
        )

        client.check("0xABC")

        body = json.loads(route.calls[0].request.content)
        assert body["chain"] == "base"


class TestCheckCaching:
    @respx.mock
    def test_second_call_uses_cache(self):
        client = _make_client()
        route = respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_reasons": []},
            )
        )

        client.check("0xABC")
        client.check("0xABC")

        assert route.call_count == 1


class TestAcheckCaching:
    @pytest.mark.anyio
    @respx.mock
    async def test_second_call_uses_cache(self):
        client = _make_client()
        route = respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_reasons": []},
            )
        )

        await client.acheck("0xABC")
        await client.acheck("0xABC")

        assert route.call_count == 1


class TestCheckFailOpen:
    def test_fail_open_stored_on_client(self):
        client = _make_client(fail_open=True)
        assert client.fail_open is True

    @respx.mock
    def test_check_raises_on_api_error(self):
        client = _make_client(fail_open=True)
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500))

        with pytest.raises(RuntimeError, match="AgentScore API returned 500"):
            client.check("0xABC")

    @respx.mock
    def test_check_raises_quota_exceeded_on_429(self):
        """429 must be distinguishable from generic 5xx so adapters surface
        ``infra_reason='quota_exceeded'`` separately when fail_open=True."""
        from agentscore_commerce.identity.client import QuotaExceededError

        client = _make_client(fail_open=False)
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))

        with pytest.raises(QuotaExceededError, match="quota_exceeded"):
            client.check("0xABC")

    @respx.mock
    def test_check_raises_quota_exceeded_on_typed_429(self):
        """SDK emits typed QuotaExceededError when body has error.code='quota_exceeded' —
        commerce wraps it so callers get the gate's QuotaExceededError sentinel.
        """
        from agentscore_commerce.identity.client import QuotaExceededError

        client = _make_client(fail_open=False)
        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                429,
                json={"error": {"code": "quota_exceeded", "message": "cap exceeded"}},
            )
        )

        with pytest.raises(QuotaExceededError):
            client.check("0xABC")

    @respx.mock
    def test_check_raises_payment_required_on_402(self):
        """402 maps to commerce's PaymentRequiredError sentinel."""
        client = _make_client(fail_open=False)
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(402))

        with pytest.raises(PaymentRequiredError):
            client.check("0xABC")

    @respx.mock
    def test_check_raises_token_denied_on_typed_401(self):
        """401 with error.code='token_expired' surfaces TokenDeniedError carrying body."""
        from agentscore_commerce.identity.client import TokenDeniedError

        client = _make_client(fail_open=False)
        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "error": {"code": "token_expired"},
                    "verify_url": "https://x",
                    "session_id": "s",
                    "poll_secret": "p",
                },
            )
        )

        with pytest.raises(TokenDeniedError) as info:
            client.check(operator_token="opc_x")
        assert info.value.body.get("verify_url") == "https://x"

    @respx.mock
    def test_check_raises_invalid_credential_on_typed_401(self):
        """401 with error.code='invalid_credential' surfaces InvalidCredentialError."""
        from agentscore_commerce.identity.client import InvalidCredentialError

        client = _make_client(fail_open=False)
        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(401, json={"error": {"code": "invalid_credential"}}),
        )

        with pytest.raises(InvalidCredentialError):
            client.check(operator_token="opc_x")


class TestCompliancePolicyFields:
    def test_build_body_includes_require_kyc(self):
        client = _make_client(require_kyc=True)
        body = client._build_body("0xabc")
        assert body["policy"]["require_kyc"] is True

    def test_build_body_includes_require_sanctions_clear(self):
        client = _make_client(require_sanctions_clear=True)
        body = client._build_body("0xabc")
        assert body["policy"]["require_sanctions_clear"] is True

    def test_build_body_includes_min_age(self):
        client = _make_client(min_age=90)
        body = client._build_body("0xabc")
        assert body["policy"]["min_age"] == 90

    def test_build_body_includes_blocked_jurisdictions(self):
        client = _make_client(blocked_jurisdictions=["KP", "IR"])
        body = client._build_body("0xabc")
        assert body["policy"]["blocked_jurisdictions"] == ["KP", "IR"]

    def test_build_body_includes_all_compliance_fields(self):
        client = _make_client(
            require_kyc=True,
            require_sanctions_clear=True,
            min_age=30,
            blocked_jurisdictions=["KP"],
        )
        body = client._build_body("0xabc")
        assert body["policy"] == {
            "require_kyc": True,
            "require_sanctions_clear": True,
            "min_age": 30,
            "blocked_jurisdictions": ["KP"],
        }


class TestOperatorVerificationParsing:
    def test_parses_operator_verification_from_response(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {
            "decision": "deny",
            "decision_reasons": ["kyc_required"],
            "operator_verification": {
                "level": "kyc_verified",
                "operator_type": "business",
                "verified_at": "2024-06-15T00:00:00Z",
            },
        }

        result = client._parse_response(resp)
        assert result.operator_verification is not None
        assert result.operator_verification.level == "kyc_verified"
        assert result.operator_verification.operator_type == "business"
        assert result.operator_verification.verified_at == "2024-06-15T00:00:00Z"

    def test_operator_verification_is_none_when_absent(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {
            "decision": "allow",
            "decision_reasons": [],
        }

        result = client._parse_response(resp)
        assert result.operator_verification is None


class TestVerifyUrlParsing:
    def test_parses_verify_url_from_response(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {
            "decision": "deny",
            "decision_reasons": ["kyc_required"],
            "verify_url": "https://agentscore.sh/verify/abc123",
        }

        result = client._parse_response(resp)
        assert result.verify_url == "https://agentscore.sh/verify/abc123"

    def test_verify_url_is_none_when_absent(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {
            "decision": "allow",
            "decision_reasons": [],
        }

        result = client._parse_response(resp)
        assert result.verify_url is None

    def test_parses_resolved_operator_from_response(self):
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {
            "decision": "deny",
            "decision_reasons": ["kyc_required"],
            "resolved_operator": "0xoperator456",
        }

        result = client._parse_response(resp)
        assert result.resolved_operator == "0xoperator456"


class TestComplianceDenyIntegration:
    @respx.mock
    def test_full_compliance_deny_flow(self):
        """Integration test: full middleware flow with compliance deny."""
        compliance_response = {
            "decision": "deny",
            "decision_reasons": ["kyc_required", "sanctions_flagged"],
            "score": {"value": 72, "grade": "C", "status": "scored"},
            "operator_verification": {
                "level": "none",
                "operator_type": None,
                "verified_at": None,
            },
            "verify_url": "https://agentscore.sh/verify/xyz789",
            "resolved_operator": "0xoperator456",
            "chains": [
                {
                    "chain": "base",
                    "classification": {"entity_type": "wallet"},
                    "activity": {},
                    "identity": {},
                }
            ],
        }

        route = respx.post(ASSESS_URL).mock(return_value=httpx.Response(200, json=compliance_response))

        client = _make_client(
            require_kyc=True,
            require_sanctions_clear=True,
        )
        result = client.check("0xABC")

        assert result.allow is False
        assert result.decision == "deny"
        assert "kyc_required" in result.reasons
        assert "sanctions_flagged" in result.reasons
        assert result.verify_url == "https://agentscore.sh/verify/xyz789"
        assert result.operator_verification is not None
        assert result.operator_verification.level == "none"
        assert result.resolved_operator == "0xoperator456"

        body = json.loads(route.calls[0].request.content)
        assert body["policy"]["require_kyc"] is True
        assert body["policy"]["require_sanctions_clear"] is True


class TestAgentIdentityDataclass:
    def test_construct_with_address_only(self):
        from agentscore_commerce.identity.types import AgentIdentity

        identity = AgentIdentity(address="0xabc")
        assert identity.address == "0xabc"
        assert identity.operator_token is None

    def test_construct_with_operator_token_only(self):
        from agentscore_commerce.identity.types import AgentIdentity

        identity = AgentIdentity(operator_token="opc_test")
        assert identity.address is None
        assert identity.operator_token == "opc_test"

    def test_construct_with_both(self):
        from agentscore_commerce.identity.types import AgentIdentity

        identity = AgentIdentity(address="0xabc", operator_token="opc_test")
        assert identity.address == "0xabc"
        assert identity.operator_token == "opc_test"

    def test_construct_empty_defaults_to_none(self):
        from agentscore_commerce.identity.types import AgentIdentity

        identity = AgentIdentity()
        assert identity.address is None
        assert identity.operator_token is None


class TestCheckIdentity:
    @respx.mock
    def test_check_identity_sends_operator_token(self):
        from agentscore_commerce.identity.types import AgentIdentity

        client = _make_client()
        route = respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_reasons": []},
            )
        )

        identity = AgentIdentity(operator_token="opc_abc")
        client.check_identity(identity)

        body = json.loads(route.calls[0].request.content)
        assert body["operator_token"] == "opc_abc"
        assert "address" not in body

    @respx.mock
    def test_check_identity_sends_address(self):
        from agentscore_commerce.identity.types import AgentIdentity

        client = _make_client()
        route = respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_reasons": []},
            )
        )

        identity = AgentIdentity(address="0xABC")
        client.check_identity(identity)

        body = json.loads(route.calls[0].request.content)
        assert body["address"] == "0xABC"
        assert "operator_token" not in body

    @respx.mock
    def test_check_identity_sends_both(self):
        from agentscore_commerce.identity.types import AgentIdentity

        client = _make_client()
        route = respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_reasons": []},
            )
        )

        identity = AgentIdentity(address="0xABC", operator_token="opc_both")
        client.check_identity(identity)

        body = json.loads(route.calls[0].request.content)
        assert body["address"] == "0xABC"
        assert body["operator_token"] == "opc_both"


class TestAcheckIdentity:
    @pytest.mark.anyio
    @respx.mock
    async def test_acheck_identity_sends_operator_token(self):
        from agentscore_commerce.identity.types import AgentIdentity

        client = _make_client()
        route = respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_reasons": []},
            )
        )

        identity = AgentIdentity(operator_token="opc_async")
        await client.acheck_identity(identity)

        body = json.loads(route.calls[0].request.content)
        assert body["operator_token"] == "opc_async"
        assert "address" not in body


class TestCacheKeyIdentity:
    def test_cache_key_prefers_operator_token(self):
        client = _make_client()
        key = client._cache_key(address="0xABC", operator_token="OPC_TEST")
        assert key == "opc_test"

    def test_cache_key_falls_back_to_address(self):
        client = _make_client()
        key = client._cache_key(address="0xABC")
        assert key == "0xabc"

    def test_cache_key_lowercases_operator_token(self):
        client = _make_client()
        key = client._cache_key(operator_token="OPC_UPPER")
        assert key == "opc_upper"


class TestBuildBodyIdentity:
    def test_build_body_with_operator_token_only(self):
        client = _make_client()
        body = client._build_body(operator_token="opc_test")
        assert body["operator_token"] == "opc_test"
        assert "address" not in body

    def test_build_body_with_both(self):
        client = _make_client()
        body = client._build_body(address="0xabc", operator_token="opc_test")
        assert body["address"] == "0xabc"
        assert body["operator_token"] == "opc_test"

    def test_build_body_address_only_backwards_compat(self):
        client = _make_client()
        body = client._build_body("0xabc")
        assert body["address"] == "0xabc"
        assert "operator_token" not in body


ASSESS_URL = "https://api.agentscore.sh/v1/assess"


class TestInvalidCredential:
    """Coverage for the 401 invalid_credential branch — distinct from token_expired
    in that no auto-session is minted. The client surfaces it as InvalidCredentialError
    so adapters can render a permanent-failure 403 instead of a transient 503 retry."""

    @respx.mock
    def test_raises_invalid_credential_on_401_invalid_credential(self):
        from agentscore_commerce.identity.client import InvalidCredentialError

        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                401, json={"error": {"code": "invalid_credential", "message": "Operator credential not found"}}
            )
        )
        client = _make_client()
        with pytest.raises(InvalidCredentialError) as exc_info:
            client.check(operator_token="opc_typo")
        assert exc_info.value.code == "invalid_credential"

    @respx.mock
    def test_raises_runtime_error_on_unknown_401_code(self):
        # New 401 codes from the API that we don't have a specific handler for fall
        # through to RuntimeError + a console.warn so ops notice without crashing
        # the request.
        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(401, json={"error": {"code": "future_code_we_havent_mapped"}})
        )
        client = _make_client()
        with pytest.raises(RuntimeError, match="returned 401"):
            client.check(operator_token="opc_x")

    @respx.mock
    def test_logs_when_401_body_isnt_valid_json(self):
        # Body parse failure used to swallow silently; now we log and fall through
        # to the generic "API returned 401" RuntimeError.
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(401, content=b"<html>not json at all</html>"))
        client = _make_client()
        with pytest.raises(RuntimeError, match="returned 401"):
            client.check(operator_token="opc_x")

    def test_build_invalid_credential_reason_carries_action_copy(self):
        from agentscore_commerce.identity.client import build_invalid_credential_reason

        reason = build_invalid_credential_reason()
        assert reason.code == "invalid_credential"
        assert reason.agent_instructions is not None
        instructions = json.loads(reason.agent_instructions)
        assert instructions["action"] == "switch_token_or_restart_session"
        # No session fields — the API doesn't mint one for this case.
        assert reason.session_id is None
        assert reason.verify_url is None
        assert reason.poll_secret is None


class TestResolveWalletErrorHandling:
    """The wallet→operator resolve path is used by verify_wallet_signer_match. HTTPError
    and non-2xx responses must return (False, None, []) so the caller can map to api_error
    instead of crashing the request."""

    @respx.mock
    def test_resolve_returns_false_on_http_error(self):
        respx.post(ASSESS_URL).mock(side_effect=httpx.ConnectError("dns failure"))
        client = _make_client()
        ok, op, links = client._resolve_wallet_to_operator("0xfeedfeedfeedfeedfeedfeedfeedfeedfeedfeed")
        assert ok is False
        assert op is None
        assert links == []

    @respx.mock
    def test_resolve_returns_false_on_non_success(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
        client = _make_client()
        ok, op, links = client._resolve_wallet_to_operator("0xfeedfeedfeedfeedfeedfeedfeedfeedfeedfeed")
        assert ok is False
        assert op is None
        assert links == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_aresolve_returns_false_on_http_error(self):
        respx.post(ASSESS_URL).mock(side_effect=httpx.ConnectError("dns failure"))
        client = _make_client()
        ok, op, links = await client._aresolve_wallet_to_operator("0xfeedfeedfeedfeedfeedfeedfeedfeedfeedfeed")
        assert ok is False
        assert op is None
        assert links == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_aresolve_returns_false_on_non_success(self):
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
        client = _make_client()
        ok, op, links = await client._aresolve_wallet_to_operator("0xfeedfeedfeedfeedfeedfeedfeedfeedfeedfeed")
        assert ok is False
        assert op is None
        assert links == []


class TestAcheckTypedErrors:
    """Async path mirror of TestCheckFailOpen — exercises SdkXxxError → commerce-error mapping
    in :meth:`acheck`. Pinned independently of the sync path because adapters wire each
    path separately and a regression in one wouldn't show up via the other.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_acheck_raises_quota_exceeded_on_typed_429(self):
        from agentscore_commerce.identity.client import QuotaExceededError

        client = _make_client()
        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                429,
                json={"error": {"code": "quota_exceeded", "message": "cap exceeded"}},
            )
        )

        with pytest.raises(QuotaExceededError):
            await client.acheck("0xABC")

    @pytest.mark.asyncio
    @respx.mock
    async def test_acheck_raises_quota_exceeded_on_untyped_429(self):
        """Mirrors the sync defensive 429-fallback path."""
        from agentscore_commerce.identity.client import QuotaExceededError

        client = _make_client()
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(429))

        with pytest.raises(QuotaExceededError):
            await client.acheck("0xABC")

    @pytest.mark.asyncio
    @respx.mock
    async def test_acheck_raises_payment_required_on_402(self):
        client = _make_client()
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(402))

        with pytest.raises(PaymentRequiredError):
            await client.acheck("0xABC")

    @pytest.mark.asyncio
    @respx.mock
    async def test_acheck_raises_token_denied_on_typed_401(self):
        from agentscore_commerce.identity.client import TokenDeniedError

        client = _make_client()
        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "error": {"code": "token_expired"},
                    "verify_url": "https://x",
                    "session_id": "s",
                    "poll_secret": "p",
                },
            )
        )

        with pytest.raises(TokenDeniedError) as info:
            await client.acheck(operator_token="opc_x")
        assert info.value.body.get("verify_url") == "https://x"

    @pytest.mark.asyncio
    @respx.mock
    async def test_acheck_raises_invalid_credential_on_typed_401(self):
        from agentscore_commerce.identity.client import InvalidCredentialError

        client = _make_client()
        respx.post(ASSESS_URL).mock(
            return_value=httpx.Response(401, json={"error": {"code": "invalid_credential"}}),
        )

        with pytest.raises(InvalidCredentialError):
            await client.acheck(operator_token="opc_x")

    @pytest.mark.asyncio
    @respx.mock
    async def test_acheck_raises_runtime_error_on_5xx(self):
        client = _make_client()
        respx.post(ASSESS_URL).mock(return_value=httpx.Response(500))

        with pytest.raises(RuntimeError, match="AgentScore API returned 500"):
            await client.acheck("0xABC")

    @pytest.mark.asyncio
    @respx.mock
    async def test_acheck_re_raises_timeout_as_httpx_timeoutexception(self):
        """SDK wraps httpx timeouts in its typed TimeoutError, but commerce adapters
        catch ``httpx.TimeoutException`` to map to ``infra_reason='network_timeout'``.
        Client must re-raise so adapters keep their existing exception clauses.
        """
        client = _make_client()
        respx.post(ASSESS_URL).mock(side_effect=httpx.TimeoutException("read timeout"))

        with pytest.raises(httpx.TimeoutException):
            await client.acheck("0xABC")

    @respx.mock
    def test_check_re_raises_timeout_as_httpx_timeoutexception(self):
        """Sync mirror of :test:`test_acheck_re_raises_timeout_as_httpx_timeoutexception`."""
        client = _make_client()
        respx.post(ASSESS_URL).mock(side_effect=httpx.TimeoutException("read timeout"))

        with pytest.raises(httpx.TimeoutException):
            client.check("0xABC")
