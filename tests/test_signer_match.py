"""Tests for the signer-match helpers and agent_memory hint."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentscore_commerce.identity import (
    AgentMemoryHint,
    GateClient,
    VerifyWalletSignerMatchOptions,
    build_agent_memory_hint,
    extract_x402_signer,
)

API_KEY = "test-api-key"
WALLET_A = "0x1111111111111111111111111111111111111111"
WALLET_B = "0x2222222222222222222222222222222222222222"


# ---------------------------------------------------------------------------
# build_agent_memory_hint
# ---------------------------------------------------------------------------


def test_agent_memory_hint_has_core_fields() -> None:
    hint = build_agent_memory_hint()
    assert hint.save_for_future_agentscore_gates is True
    assert "AgentScore" in hint.pattern_summary
    assert hint.identity_check_endpoint == "https://api.agentscore.sh/v1/credentials"
    assert hint.list_wallets_endpoint is None
    assert "X-Wallet-Address" in hint.identity_paths["wallet"]
    assert "X-Operator-Token" in hint.identity_paths["operator_token"]
    assert "operator_token" in hint.do_not_persist_in_memory


def test_agent_memory_hint_is_dataclass() -> None:
    hint = build_agent_memory_hint()
    assert isinstance(hint, AgentMemoryHint)


# ---------------------------------------------------------------------------
# extract_x402_signer
# ---------------------------------------------------------------------------


def _encode_x402(sender: str) -> str:
    body = {"payload": {"authorization": {"from": sender}}}
    return base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")


def test_extract_x402_signer_valid() -> None:
    header = _encode_x402(WALLET_A)
    assert extract_x402_signer(header) == WALLET_A.lower()


def test_extract_x402_signer_none_for_missing_header() -> None:
    assert extract_x402_signer(None) is None
    assert extract_x402_signer("") is None


def test_extract_x402_signer_none_for_malformed() -> None:
    assert extract_x402_signer("!!!not-base64!!!") is None
    assert extract_x402_signer(base64.b64encode(b"not json").decode("ascii")) is None


def test_extract_x402_signer_none_for_missing_from() -> None:
    header = base64.b64encode(json.dumps({"payload": {"authorization": {}}}).encode()).decode()
    assert extract_x402_signer(header) is None


def test_extract_x402_signer_rejects_non_evm() -> None:
    header = base64.b64encode(json.dumps({"payload": {"authorization": {"from": "not-a-wallet"}}}).encode()).decode()
    assert extract_x402_signer(header) is None


# ---------------------------------------------------------------------------
# GateClient.verify_wallet_signer_match
# ---------------------------------------------------------------------------


def test_verify_wallet_signer_match_byte_equal_pass() -> None:
    client = GateClient(api_key=API_KEY)
    result = client.verify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_A),
    )
    assert result.kind == "pass"


def test_verify_wallet_signer_match_requires_signing_on_null_signer() -> None:
    client = GateClient(api_key=API_KEY)
    result = client.verify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=None),
    )
    assert result.kind == "wallet_auth_requires_wallet_signing"
    assert result.claimed_wallet == WALLET_A
    assert result.agent_instructions is not None
    assert "switch_to_operator_token" in result.agent_instructions
    # user_message lives inside agent_instructions (single source of truth).
    instr = json.loads(result.agent_instructions)
    assert "wallet-signing rails" in instr["user_message"] or "Wallet-address identity" in instr["user_message"]


def test_verify_wallet_signer_match_same_operator_pass() -> None:
    client = GateClient(api_key=API_KEY)

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {"resolved_operator": "op_shared", "decision": "allow"}
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )
    assert result.kind == "pass"
    assert result.claimed_operator == "op_shared"
    assert result.signer_operator == "op_shared"


def test_verify_wallet_signer_match_different_operator_rejects() -> None:
    """New 1-call path: API returns signer_match in the assess response."""
    client = GateClient(api_key=API_KEY)

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {
            "decision": "allow",
            "decision_reasons": [],
            "resolved_operator": "op_claimed",
            "signer_match": {
                "kind": "wallet_signer_mismatch",
                "claimed_operator": "op_claimed",
                "signer_operator": "op_attacker",
                "expected_signer": WALLET_A.lower(),
                "actual_signer": WALLET_B.lower(),
                "linked_wallets": [WALLET_A.lower()],
                "agent_instructions": json.dumps(
                    {
                        "action": "resign_or_switch_to_operator_token",
                        "steps": ["re-sign with linked_wallets"],
                        "user_message": "Different operator detected",
                    }
                ),
            },
        }
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )
    assert result.kind == "wallet_signer_mismatch"
    assert result.claimed_operator == "op_claimed"
    assert result.actual_signer_operator == "op_attacker"
    assert result.expected_signer == WALLET_A.lower()
    assert result.actual_signer == WALLET_B.lower()
    assert result.agent_instructions is not None
    assert "resign_or_switch_to_operator_token" in result.agent_instructions
    instr = json.loads(result.agent_instructions)
    assert "operator" in instr["user_message"]


def test_verify_wallet_signer_match_transient_error_emits_api_error() -> None:
    """Sec2: transient /v1/assess failures must NOT be conflated with wallet_signer_mismatch."""
    client = GateClient(api_key=API_KEY)

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.is_success = False
        resp.status_code = 503
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )
    assert result.kind == "api_error"
    assert result.claimed_wallet == WALLET_A.lower()


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_transient_error_emits_api_error() -> None:
    client = GateClient(api_key=API_KEY)
    from unittest.mock import AsyncMock

    async def fake_apost(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        # SDK gates response handling on status_code >= 400 — set 503 so the SDK
        # raises AgentScoreError, which commerce maps to api_error.
        resp.status_code = 503
        resp.is_success = False
        return resp

    client._async_client.post = AsyncMock(side_effect=fake_apost)
    result = await client.averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
    )
    assert result.kind == "api_error"


def test_verify_wallet_signer_match_unlinked_signer_rejects() -> None:
    """New 1-call path: API returns signer_match with signer_operator=None for unlinked wallet."""
    client = GateClient(api_key=API_KEY)

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {
            "decision": "allow",
            "decision_reasons": [],
            "resolved_operator": "op_claimed",
            "signer_match": {
                "kind": "wallet_signer_mismatch",
                "claimed_operator": "op_claimed",
                "signer_operator": None,
                "expected_signer": WALLET_A.lower(),
                "actual_signer": WALLET_B.lower(),
            },
        }
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )
    assert result.kind == "wallet_signer_mismatch"
    assert result.actual_signer_operator is None


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_byte_equal_pass() -> None:
    client = GateClient(api_key=API_KEY)
    result = await client.averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_A),
    )
    assert result.kind == "pass"


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_linked_wallets_threaded_through() -> None:
    """Async path surfaces linked_wallets from the server-side signer_match response."""
    from unittest.mock import AsyncMock

    client = GateClient(api_key=API_KEY)
    extra_wallet = "0xcccc000000000000000000000000000000000000"

    async def fake_apost(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = MagicMock(
            return_value={
                "decision": "allow",
                "decision_reasons": [],
                "resolved_operator": "op_claimed",
                "signer_match": {
                    "kind": "wallet_signer_mismatch",
                    "claimed_operator": "op_claimed",
                    "signer_operator": "op_signer",
                    "expected_signer": WALLET_A.lower(),
                    "actual_signer": WALLET_B.lower(),
                    "linked_wallets": [WALLET_A.lower(), extra_wallet],
                },
            }
        )
        return resp

    client._async_client.post = AsyncMock(side_effect=fake_apost)
    result = await client.averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
    )
    assert result.kind == "wallet_signer_mismatch"
    assert result.linked_wallets == [WALLET_A.lower(), extra_wallet]


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_requires_signing_on_null_signer() -> None:
    client = GateClient(api_key=API_KEY)
    result = await client.averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=None),
    )
    assert result.kind == "wallet_auth_requires_wallet_signing"


# ---------------------------------------------------------------------------
# Adapter wrapper tests — operator-token wins and the signer check no-ops when
# BOTH headers are sent. Also exercises agent_memory body serialization.
# ---------------------------------------------------------------------------


class _FakeState(dict):
    """Minimal state dict shape the adapter wrappers read."""


@pytest.mark.asyncio
async def test_asgi_verify_wallet_signer_match_no_op_on_operator_token_path() -> None:
    """ASGI wrapper returns pass without calling client when request was operator-token authenticated."""
    from unittest.mock import AsyncMock

    from agentscore_commerce.identity.middleware import GATE_STATE_KEY, verify_wallet_signer_match

    fake_client = AsyncMock()
    request = MagicMock()
    request.scope = {
        "state": {GATE_STATE_KEY: {"client": fake_client, "operator_token": "opc_test", "wallet_address": None}},
    }

    result = await verify_wallet_signer_match(request, signer="0xabc")

    assert result.kind == "pass"
    fake_client.averify_wallet_signer_match.assert_not_called()


@pytest.mark.asyncio
async def test_asgi_verify_wallet_signer_match_no_op_when_both_headers_sent() -> None:
    """Section IV: token wins when both headers sent — signer check must no-op."""
    from unittest.mock import AsyncMock

    from agentscore_commerce.identity.middleware import GATE_STATE_KEY, verify_wallet_signer_match

    fake_client = AsyncMock()
    request = MagicMock()
    request.scope = {
        "state": {
            GATE_STATE_KEY: {
                "client": fake_client,
                "operator_token": "opc_test",
                "wallet_address": WALLET_A,
            },
        },
    }

    result = await verify_wallet_signer_match(request, signer=WALLET_B)

    assert result.kind == "pass"
    assert result.claimed_operator is None
    fake_client.averify_wallet_signer_match.assert_not_called()


@pytest.mark.asyncio
async def test_asgi_verify_wallet_signer_match_invokes_client_on_wallet_auth() -> None:
    """Strict wallet-auth path — helper calls client and returns its result."""
    from unittest.mock import AsyncMock

    from agentscore_commerce.identity.middleware import GATE_STATE_KEY, verify_wallet_signer_match
    from agentscore_commerce.identity.types import VerifyWalletSignerResult

    fake_client = AsyncMock()
    fake_client.averify_wallet_signer_match.return_value = VerifyWalletSignerResult(
        kind="wallet_signer_mismatch",
        claimed_operator="op_claimed",
        actual_signer_operator="op_signer",
    )
    request = MagicMock()
    request.scope = {
        "state": {
            GATE_STATE_KEY: {
                "client": fake_client,
                "operator_token": None,
                "wallet_address": WALLET_A,
            },
        },
    }

    result = await verify_wallet_signer_match(request, signer=WALLET_B)

    assert result.kind == "wallet_signer_mismatch"
    fake_client.averify_wallet_signer_match.assert_called_once()


def test_denial_reason_to_body_includes_agent_memory() -> None:
    """The shared serializer marshals agent_memory into the body dict."""
    from agentscore_commerce.identity._response import denial_reason_to_body
    from agentscore_commerce.identity.types import DenialReason, build_agent_memory_hint

    reason = DenialReason(
        code="missing_identity",
        agent_memory=build_agent_memory_hint(),
    )
    body = denial_reason_to_body(reason)

    assert body["error"]["code"] == "missing_identity"
    assert "agent_memory" in body
    assert body["agent_memory"]["save_for_future_agentscore_gates"] is True
    assert "identity_paths" in body["agent_memory"]


def test_denial_reason_to_body_includes_wallet_signer_mismatch_fields() -> None:
    """The shared serializer marshals wallet-signer-match fields into the body."""
    from agentscore_commerce.identity._response import denial_reason_to_body
    from agentscore_commerce.identity.types import DenialReason

    reason = DenialReason(
        code="wallet_signer_mismatch",
        claimed_operator="op_claimed",
        actual_signer_operator="op_signer",
        expected_signer=WALLET_A.lower(),
        actual_signer=WALLET_B.lower(),
        linked_wallets=[WALLET_A.lower()],
    )
    body = denial_reason_to_body(reason)

    assert body["error"]["code"] == "wallet_signer_mismatch"
    assert body["claimed_operator"] == "op_claimed"
    assert body["actual_signer_operator"] == "op_signer"
    assert body["expected_signer"] == WALLET_A.lower()
    assert body["actual_signer"] == WALLET_B.lower()
    assert body["linked_wallets"] == [WALLET_A.lower()]


def test_build_missing_identity_reason_attaches_memory_hint() -> None:
    """The missing_identity builder attaches an agent_memory hint by default."""
    from agentscore_commerce.identity._response import build_missing_identity_reason

    reason = build_missing_identity_reason()
    assert reason.code == "missing_identity"
    assert reason.agent_memory is not None
    assert reason.agent_memory.save_for_future_agentscore_gates is True


def test_build_missing_identity_reason_hints_probe_strategy() -> None:
    """Bootstrap denial carries agent_instructions that describe the full probe strategy —
    wallet-first on signing rails, fall back to stored opc_..., fall back to session flow."""
    from agentscore_commerce.identity._response import build_missing_identity_reason, denial_reason_to_body

    reason = build_missing_identity_reason()
    assert reason.agent_instructions is not None

    instructions = json.loads(reason.agent_instructions)
    assert instructions["action"] == "probe_identity_then_session"
    assert isinstance(instructions["steps"], list)
    assert len(instructions["steps"]) >= 3
    assert "X-Operator-Token" in instructions["user_message"] or "X-Wallet-Address" in instructions["user_message"]

    # Shows up in the serialized body.
    body = denial_reason_to_body(reason)
    body_instructions = json.loads(body["agent_instructions"])
    assert body_instructions["action"] == "probe_identity_then_session"


# ---------------------------------------------------------------------------
# Adapter parity — operator-token wins when both headers sent. Each adapter reads
# gate state from a framework-specific location, so each needs its own no-op test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fastapi_verify_wallet_signer_match_no_op_when_both_headers_sent() -> None:
    from unittest.mock import AsyncMock

    from agentscore_commerce.identity.fastapi import GATE_STATE_KEY, verify_wallet_signer_match

    fake_client = AsyncMock()
    request = MagicMock()
    setattr(
        request.state,
        GATE_STATE_KEY,
        {
            "client": fake_client,
            "operator_token": "opc_test",
            "wallet_address": WALLET_A,
        },
    )

    result = await verify_wallet_signer_match(request, signer=WALLET_B)

    assert result.kind == "pass"
    fake_client.averify_wallet_signer_match.assert_not_called()


def test_flask_verify_wallet_signer_match_no_op_when_both_headers_sent() -> None:
    from flask import Flask

    from agentscore_commerce.identity.flask import verify_wallet_signer_match

    fake_client = MagicMock()
    app = Flask(__name__)
    with app.test_request_context("/"):
        from flask import g

        g._agentscore_gate = {  # type: ignore[attr-defined]
            "client": fake_client,
            "operator_token": "opc_test",
            "wallet_address": WALLET_A,
        }
        result = verify_wallet_signer_match(signer=WALLET_B)

    assert result.kind == "pass"
    fake_client.verify_wallet_signer_match.assert_not_called()


def test_django_verify_wallet_signer_match_no_op_when_both_headers_sent() -> None:
    from agentscore_commerce.identity.django import verify_wallet_signer_match

    fake_client = MagicMock()
    request = MagicMock()
    request._agentscore_gate = {
        "client": fake_client,
        "operator_token": "opc_test",
        "wallet_address": WALLET_A,
    }

    result = verify_wallet_signer_match(request, signer=WALLET_B)

    assert result.kind == "pass"
    fake_client.verify_wallet_signer_match.assert_not_called()


@pytest.mark.asyncio
async def test_aiohttp_verify_wallet_signer_match_no_op_when_both_headers_sent() -> None:
    from unittest.mock import AsyncMock

    from agentscore_commerce.identity.aiohttp import GATE_STATE_KEY, verify_wallet_signer_match

    fake_client = AsyncMock()
    request: dict[str, object] = {
        GATE_STATE_KEY: {
            "client": fake_client,
            "operator_token": "opc_test",
            "wallet_address": WALLET_A,
        },
    }

    result = await verify_wallet_signer_match(request, signer=WALLET_B)  # type: ignore[arg-type]

    assert result.kind == "pass"
    fake_client.averify_wallet_signer_match.assert_not_called()


@pytest.mark.asyncio
async def test_sanic_verify_wallet_signer_match_no_op_when_both_headers_sent() -> None:
    from unittest.mock import AsyncMock

    from agentscore_commerce.identity.sanic import GATE_STATE_ATTR, verify_wallet_signer_match

    fake_client = AsyncMock()
    request = MagicMock()
    setattr(
        request.ctx,
        GATE_STATE_ATTR,
        {
            "client": fake_client,
            "operator_token": "opc_test",
            "wallet_address": WALLET_A,
        },
    )

    result = await verify_wallet_signer_match(request, signer=WALLET_B)

    assert result.kind == "pass"
    fake_client.averify_wallet_signer_match.assert_not_called()


# ---------------------------------------------------------------------------
# Sync path linked_wallets threading — mirror of the async test above.
# ---------------------------------------------------------------------------


def test_verify_wallet_signer_match_linked_wallets_threaded_through_sync() -> None:
    """Sync path surfaces linked_wallets from the server-side signer_match response."""
    client = GateClient(api_key=API_KEY)
    extra_wallet = "0xcccc000000000000000000000000000000000000"

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {
            "decision": "allow",
            "decision_reasons": [],
            "resolved_operator": "op_claimed",
            "signer_match": {
                "kind": "wallet_signer_mismatch",
                "claimed_operator": "op_claimed",
                "signer_operator": "op_signer",
                "expected_signer": WALLET_A.lower(),
                "actual_signer": WALLET_B.lower(),
                "linked_wallets": [WALLET_A.lower(), extra_wallet],
            },
        }
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )

    assert result.kind == "wallet_signer_mismatch"
    assert result.linked_wallets == [WALLET_A.lower(), extra_wallet]


# ---------------------------------------------------------------------------
# agent_memory gating — present on bootstrap denials, absent on everything else.
# ---------------------------------------------------------------------------


def test_denial_reason_to_body_omits_agent_memory_on_non_bootstrap_denial() -> None:
    """wallet_signer_mismatch is post-identity — body must NOT carry an agent_memory hint."""
    from agentscore_commerce.identity._response import denial_reason_to_body
    from agentscore_commerce.identity.types import DenialReason

    reason = DenialReason(
        code="wallet_signer_mismatch",
        claimed_operator="op_claimed",
        actual_signer_operator="op_signer",
        expected_signer=WALLET_A.lower(),
        actual_signer=WALLET_B.lower(),
        linked_wallets=[WALLET_A.lower()],
    )
    body = denial_reason_to_body(reason)

    assert body["error"]["code"] == "wallet_signer_mismatch"
    assert "agent_memory" not in body


def test_denial_reason_to_body_omits_agent_memory_on_wallet_not_trusted() -> None:
    """wallet_not_trusted is also post-identity; no agent_memory hint in the body."""
    from agentscore_commerce.identity._response import denial_reason_to_body
    from agentscore_commerce.identity.types import DenialReason

    body = denial_reason_to_body(DenialReason(code="wallet_not_trusted"))
    assert body["error"]["code"] == "wallet_not_trusted"
    assert "agent_memory" not in body


# ---------------------------------------------------------------------------
# 401 token_expired pass-through — covers both revoked and TTL-expired credentials
# (API deliberately doesn't disclose which). The 401 body carries an auto-minted
# session so agents recover without an API key.
# ---------------------------------------------------------------------------


def _mock_401(code: str, next_steps: dict[str, object] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 401
    resp.is_success = False
    body: dict[str, object] = {"error": {"code": code, "message": f"test {code}"}}
    if next_steps is not None:
        body["next_steps"] = next_steps
    resp.json.return_value = body
    return resp


def test_check_raises_token_denied_on_401_expired() -> None:
    from agentscore_commerce.identity.client import TokenDeniedError

    client = GateClient(api_key=API_KEY)
    mock_resp = _mock_401("token_expired", {"action": "deliver_verify_url_and_poll"})
    with patch.object(client._sync_client, "post", return_value=mock_resp):
        try:
            client.check(operator_token="opc_expired")
        except TokenDeniedError as err:
            assert err.code == "token_expired"
            assert err.next_steps == {"action": "deliver_verify_url_and_poll"}
        else:
            pytest.fail("expected TokenDeniedError")


def test_check_raises_token_denied_on_401_revoked() -> None:
    # Revoked credentials surface to the client as the same token_expired code — the API
    # deliberately doesn't disclose which case (revoked vs TTL-expired) to the gate.
    from agentscore_commerce.identity.client import TokenDeniedError

    client = GateClient(api_key=API_KEY)
    with patch.object(client._sync_client, "post", return_value=_mock_401("token_expired")):
        try:
            client.check(operator_token="opc_revoked")
        except TokenDeniedError as err:
            assert err.code == "token_expired"
            assert err.next_steps is None
        else:
            pytest.fail("expected TokenDeniedError")


def test_check_raises_runtime_error_on_401_unknown_code() -> None:
    """401 with an unrecognized error code falls through to generic RuntimeError, not TokenDeniedError."""
    from agentscore_commerce.identity.client import TokenDeniedError

    client = GateClient(api_key=API_KEY)
    with patch.object(client._sync_client, "post", return_value=_mock_401("something_else")):
        try:
            client.check(operator_token="opc_odd")
        except TokenDeniedError:
            pytest.fail("should not be raised for unknown 401 code")
        except RuntimeError as err:
            assert "401" in str(err)


@pytest.mark.asyncio
async def test_acheck_raises_token_denied_on_401() -> None:
    from unittest.mock import AsyncMock

    from agentscore_commerce.identity.client import TokenDeniedError

    client = GateClient(api_key=API_KEY)
    client._async_client.post = AsyncMock(return_value=_mock_401("token_expired"))
    try:
        await client.acheck(operator_token="opc_expired")
    except TokenDeniedError as err:
        assert err.code == "token_expired"
    else:
        pytest.fail("expected TokenDeniedError")


def test_asgi_middleware_surfaces_token_denied_as_granular_denial() -> None:
    """Integration: ASGI middleware catches TokenDeniedError → DenialReason(code=token_expired)."""
    import httpx
    import respx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from agentscore_commerce.identity.middleware import AgentScoreGate

    def _homepage(_request: object) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/", _homepage)])
    gated = AgentScoreGate(app, api_key=API_KEY)

    with respx.mock:
        respx.post("https://api.agentscore.sh/v1/assess").mock(
            return_value=httpx.Response(
                401,
                json={
                    "error": {"code": "token_expired", "message": "credential has expired"},
                    "next_steps": {"action": "deliver_verify_url_and_poll"},
                },
            ),
        )
        client = TestClient(gated, raise_server_exceptions=False)
        res = client.get("/", headers={"x-operator-token": "opc_expired"})

    assert res.status_code == 401
    body = res.json()
    assert body["error"]["code"] == "token_expired"
    # agent_instructions is a JSON string of next_steps
    assert json.loads(body["agent_instructions"]) == {"action": "deliver_verify_url_and_poll"}


# ---------------------------------------------------------------------------
# Signer-match telemetry — fire-and-forget POST to /v1/telemetry/signer-match
# ---------------------------------------------------------------------------


def test_verify_wallet_signer_match_posts_pass_telemetry() -> None:
    client = GateClient(api_key=API_KEY)
    telemetry_calls: list[str] = []

    def capture(url: str, **kwargs: object) -> MagicMock:
        if "/v1/telemetry/signer-match" in url:
            # SDK uses httpx's `json=` kwarg (auto-serializes dict). Older tests
            # patched a raw httpx call that took `content=<string>`; both shapes
            # carry the same payload — try `json` first, fall back to `content`.
            payload = kwargs.get("json")
            if payload is None and "content" in kwargs:
                payload = json.loads(kwargs["content"])  # type: ignore[arg-type]
            telemetry_calls.append(payload["kind"])  # type: ignore[index]
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 201
        resp.json.return_value = {}
        return resp

    with patch.object(client._sync_client, "post", side_effect=capture):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_A),
        )

    assert result.kind == "pass"
    assert telemetry_calls == ["pass"]


def test_verify_wallet_signer_match_posts_requires_signing_telemetry() -> None:
    client = GateClient(api_key=API_KEY)
    telemetry_calls: list[str] = []

    def capture(url: str, **kwargs: object) -> MagicMock:
        if "/v1/telemetry/signer-match" in url:
            # SDK uses httpx's `json=` kwarg (auto-serializes dict). Older tests
            # patched a raw httpx call that took `content=<string>`; both shapes
            # carry the same payload — try `json` first, fall back to `content`.
            payload = kwargs.get("json")
            if payload is None and "content" in kwargs:
                payload = json.loads(kwargs["content"])  # type: ignore[arg-type]
            telemetry_calls.append(payload["kind"])  # type: ignore[index]
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 201
        resp.json.return_value = {}
        return resp

    with patch.object(client._sync_client, "post", side_effect=capture):
        client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=None),
        )

    assert telemetry_calls == ["wallet_auth_requires_wallet_signing"]


def test_verify_wallet_signer_match_telemetry_failure_does_not_raise() -> None:
    """Gate decision must not depend on telemetry availability."""
    client = GateClient(api_key=API_KEY)

    def raiser(*_args: object, **_kwargs: object) -> MagicMock:
        raise httpx.HTTPError("telemetry outage")

    with patch.object(client._sync_client, "post", side_effect=raiser):
        # byte-equal short-circuit returns pass; telemetry failure is swallowed.
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_A),
        )
    assert result.kind == "pass"


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_telemetry_failure_does_not_raise() -> None:
    """Async path mirror — telemetry outage must not break gate decisions."""
    client = GateClient(api_key=API_KEY)

    async def araiser(*_args: object, **_kwargs: object) -> MagicMock:
        raise httpx.HTTPError("telemetry outage")

    from unittest.mock import AsyncMock

    client._async_client.post = AsyncMock(side_effect=araiser)
    result = await client.averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_A),
    )
    assert result.kind == "pass"


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_pass_via_shared_operator() -> None:
    """Different signer wallet but same operator → pass.

    Mirrors the sync ``test_verify_wallet_signer_match_pass_via_shared_operator``
    branch — pinned independently because the async path's signer_match decode +
    telemetry chain is wired separately and a regression in either wouldn't show
    up via the sync test.
    """
    client = GateClient(api_key=API_KEY)

    async def fake_apost(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {
            "decision": "allow",
            "decision_reasons": [],
            "resolved_operator": "op_shared",
            "signer_match": {
                "kind": "pass",
                "claimed_operator": "op_shared",
                "signer_operator": "op_shared",
            },
        }
        return resp

    from unittest.mock import AsyncMock

    client._async_client.post = AsyncMock(side_effect=fake_apost)
    result = await client.averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
    )
    assert result.kind == "pass"
    assert result.claimed_operator == "op_shared"
    assert result.signer_operator == "op_shared"


def test_verify_wallet_signer_match_cache_hit_skips_assess() -> None:
    """Repeat (claimed, signer) lookup hits the signer_match cache — no fresh assess."""
    client = GateClient(api_key=API_KEY)
    call_count = 0

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {
            "decision": "allow",
            "decision_reasons": [],
            "resolved_operator": "op_shared",
            "signer_match": {"kind": "pass", "claimed_operator": "op_shared", "signer_operator": "op_shared"},
        }
        return resp

    opts = VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B)
    with patch.object(client._sync_client, "post", side_effect=fake_post):
        client.verify_wallet_signer_match(opts)
        first = call_count
        client.verify_wallet_signer_match(opts)
    assert call_count == first  # second run reads cached signer_match


def test_verify_wallet_signer_match_legacy_fallback_when_signer_match_absent() -> None:
    """Old API responses lack signer_match — commerce falls back to the 2-resolve path."""
    client = GateClient(api_key=API_KEY)
    call_index = 0

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_index
        call_index += 1
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        bodies = [
            {"decision": "allow", "decision_reasons": [], "resolved_operator": "op_claimed"},
            {
                "decision": "allow",
                "decision_reasons": [],
                "resolved_operator": "op_claimed",
                "linked_wallets": [WALLET_A.lower()],
            },
            {"decision": "allow", "decision_reasons": [], "resolved_operator": "op_signer"},
        ]
        resp.json = lambda i=call_index - 1: bodies[i] if i < len(bodies) else {}
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )
    assert result.kind == "wallet_signer_mismatch"
    assert result.claimed_operator == "op_claimed"
    assert result.actual_signer_operator == "op_signer"


def test_verify_wallet_signer_match_assess_failure_returns_api_error() -> None:
    """SDK raising AgentScoreError on the resolve_signer-aware assess → api_error."""
    from agentscore import AgentScoreError as SdkErr

    client = GateClient(api_key=API_KEY)

    def raise_err(*_args: object, **_kwargs: object) -> object:
        raise SdkErr("network_error", "DNS failure", 0)

    with patch.object(client._sdk, "assess", side_effect=raise_err):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )
    assert result.kind == "api_error"
    assert result.claimed_wallet == WALLET_A.lower()


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_assess_failure_returns_api_error() -> None:
    """Async mirror — SDK raising AgentScoreError on aassess → api_error."""
    from unittest.mock import AsyncMock

    from agentscore import AgentScoreError as SdkErr

    client = GateClient(api_key=API_KEY)

    async def raise_err(*_args: object, **_kwargs: object) -> object:
        raise SdkErr("network_error", "DNS failure", 0)

    client._sdk.aassess = AsyncMock(side_effect=raise_err)
    result = await client.averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
    )
    assert result.kind == "api_error"
    assert result.claimed_wallet == WALLET_A.lower()


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_cache_hit_skips_assess() -> None:
    """Async mirror of cache hit test."""
    from unittest.mock import AsyncMock

    client = GateClient(api_key=API_KEY)
    call_count = 0

    async def fake_apost(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = MagicMock(
            return_value={
                "decision": "allow",
                "decision_reasons": [],
                "resolved_operator": "op_shared",
                "signer_match": {"kind": "pass", "claimed_operator": "op_shared", "signer_operator": "op_shared"},
            }
        )
        return resp

    client._async_client.post = AsyncMock(side_effect=fake_apost)
    opts = VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B)
    await client.averify_wallet_signer_match(opts)
    first = call_count
    await client.averify_wallet_signer_match(opts)
    assert call_count == first


def test_verify_wallet_signer_match_solana_signer_normalizes_correctly() -> None:
    """Solana base58 signers must NOT be lowercased — `_infer_signer_network` returns 'solana'."""
    client = GateClient(api_key=API_KEY)
    solana_claimed = "DRpbCBMxVnDK7maPM5tGv6MvB3v1sRMC86PZ8okm21hy"
    solana_signer = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

    captured_body: dict[str, object] = {}

    def fake_post(*_args: object, **kwargs: object) -> MagicMock:
        # SDK serializes the body via httpx `json=` kwarg.
        if "json" in kwargs:
            payload = kwargs["json"]
            if isinstance(payload, dict):
                captured_body.update(payload)
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {
            "decision": "allow",
            "decision_reasons": [],
            "resolved_operator": "op_x",
            "signer_match": {"kind": "pass", "claimed_operator": "op_x", "signer_operator": "op_x"},
        }
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=solana_claimed, signer=solana_signer),
        )
    assert result.kind == "pass"
    # Solana detection: base58 (no 0x prefix) → network='solana' on the request body.
    rs = captured_body.get("resolve_signer", {})
    if isinstance(rs, dict):
        assert rs.get("network") == "solana"


def test_verify_wallet_signer_match_signer_match_with_wallet_auth_requires_signing_kind() -> None:
    """API-side `wallet_auth_requires_wallet_signing` verdict (e.g. signer null on the API
    side) projects onto VerifyWalletSignerResult of the same kind. Distinct from the
    client-side null short-circuit because the API may emit this for other reasons.
    """
    client = GateClient(api_key=API_KEY)

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {
            "decision": "allow",
            "decision_reasons": [],
            "resolved_operator": "op_x",
            "signer_match": {
                "kind": "wallet_auth_requires_wallet_signing",
                "claimed_wallet": WALLET_A.lower(),
                "agent_instructions": json.dumps(
                    {
                        "action": "switch_to_operator_token",
                        "steps": [],
                        "user_message": "test",
                    }
                ),
            },
        }
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )
    assert result.kind == "wallet_auth_requires_wallet_signing"
    assert result.claimed_wallet == WALLET_A.lower()


def test_verify_wallet_signer_match_legacy_fallback_uses_resolve_cache() -> None:
    """Legacy fallback path reads the resolve: cache when a previous resolveWalletToOperator
    call already populated it — saves the second /v1/assess on the legacy branch.
    """
    from agentscore_commerce.identity.types import AssessResult

    client = GateClient(api_key=API_KEY)
    # Pre-warm the resolve: cache for both wallets so the legacy fallback hits cache,
    # not the network. Mirrors what would happen if a prior signer-match-disabled
    # API response had already left these entries in cache from a different gate path.
    client._cache.set(
        f"resolve:{WALLET_A.lower()}",
        AssessResult(
            allow=True, raw={"resolved_operator": "op_shared", "linked_wallets": [WALLET_A.lower(), WALLET_B.lower()]}
        ),
    )
    client._cache.set(
        f"resolve:{WALLET_B.lower()}",
        AssessResult(
            allow=True, raw={"resolved_operator": "op_shared", "linked_wallets": [WALLET_A.lower(), WALLET_B.lower()]}
        ),
    )

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        # Only the resolveSigner-aware assess hits the wire; both legacy resolves hit cache.
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        # No signer_match in body → falls through to legacy. Cache hits for both resolves.
        resp.json = lambda: {"decision": "allow", "decision_reasons": [], "resolved_operator": "op_shared"}
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        result = client.verify_wallet_signer_match(
            VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
        )
    assert result.kind == "pass"
    assert result.claimed_operator == "op_shared"


@pytest.mark.asyncio
async def test_averify_wallet_signer_match_legacy_fallback_when_signer_match_absent() -> None:
    """Async mirror of legacy fallback path."""
    from unittest.mock import AsyncMock

    client = GateClient(api_key=API_KEY)
    call_index = 0

    async def fake_apost(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal call_index
        call_index += 1
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        bodies = [
            {"decision": "allow", "decision_reasons": [], "resolved_operator": "op_claimed"},
            {
                "decision": "allow",
                "decision_reasons": [],
                "resolved_operator": "op_claimed",
                "linked_wallets": [WALLET_A.lower()],
            },
            {"decision": "allow", "decision_reasons": [], "resolved_operator": "op_signer"},
        ]
        resp.json = MagicMock(return_value=bodies[call_index - 1] if call_index <= len(bodies) else {})
        return resp

    client._async_client.post = AsyncMock(side_effect=fake_apost)
    result = await client.averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(claimed_wallet=WALLET_A, signer=WALLET_B),
    )
    assert result.kind == "wallet_signer_mismatch"
    assert result.claimed_operator == "op_claimed"
    assert result.actual_signer_operator == "op_signer"
