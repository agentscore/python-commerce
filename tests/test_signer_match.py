"""Tests for signer-verdict surface, agent_memory hint, and 401 token_expired path.

The verify_wallet_signer_match 2-call path was retired in favor of the gate-pre-extract
architecture: adapters extract the payment signer up front, pass it to /v1/assess via
the SDK's ``signer`` kwarg, and the API returns ``signer_match`` + ``signer_sanctions``
on the same response. Merchants read the verdicts back via ``get_signer_verdict``.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from agentscore_commerce.identity import (
    AgentMemoryHint,
    AgentScoreCore,
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
    assert hint.identity_check_endpoint == "https://api.agentscore.com/v1/credentials"
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


def test_extract_x402_signer_none_when_decoded_json_not_dict() -> None:
    """A header decoding to a JSON array/scalar (not an object) yields None."""
    array_header = base64.b64encode(json.dumps([1, 2, 3]).encode()).decode()
    assert extract_x402_signer(array_header) is None
    scalar_header = base64.b64encode(json.dumps("just-a-string").encode()).decode()
    assert extract_x402_signer(scalar_header) is None


def test_extract_x402_signer_skips_solana_network() -> None:
    """Solana x402 payloads are explicitly skipped (caller extracts the SPL payer)."""
    header = base64.b64encode(
        json.dumps(
            {
                "accepted": {"network": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"},
                "payload": {"authorization": {"from": WALLET_A}},
            }
        ).encode()
    ).decode()
    assert extract_x402_signer(header) is None


def test_extract_x402_signer_none_when_payload_not_dict() -> None:
    """`payload` present but not an object (null / string) yields None."""
    header = base64.b64encode(json.dumps({"payload": "oops"}).encode()).decode()
    assert extract_x402_signer(header) is None


def test_extract_x402_signer_none_when_authorization_not_dict() -> None:
    """`payload.authorization` present but not an object yields None."""
    header = base64.b64encode(json.dumps({"payload": {"authorization": "oops"}}).encode()).decode()
    assert extract_x402_signer(header) is None


# ---------------------------------------------------------------------------
# AgentScoreCore.check passes signer through; client.get_signer_verdict reads it back
# ---------------------------------------------------------------------------


def test_check_forwards_signer_to_assess_body() -> None:
    """Adapter pre-extracts the signer; client.check threads it onto the request body."""
    client = AgentScoreCore(api_key=API_KEY)
    captured: dict[str, object] = {}

    def fake_post(*_args: object, **kwargs: object) -> MagicMock:
        if "json" in kwargs and isinstance(kwargs["json"], dict):
            captured.update(kwargs["json"])
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {"decision": "allow", "decision_reasons": [], "resolved_operator": "op_x"}
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        client.check(address=WALLET_A, signer={"address": WALLET_B, "network": "evm"})

    assert captured.get("signer") == {"address": WALLET_B, "network": "evm"}


def test_get_signer_verdict_projects_cached_signer_match() -> None:
    """After a check() with signer, get_signer_verdict reads signer_match + signer_sanctions."""
    client = AgentScoreCore(api_key=API_KEY)

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
            },
            "signer_sanctions": {"kind": "clear"},
        }
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        client.check(address=WALLET_A, signer={"address": WALLET_B, "network": "evm"})

    verdict = client.get_signer_verdict(WALLET_A)
    assert verdict is not None
    signer_match = verdict.signer_match
    assert signer_match is not None
    assert signer_match.kind == "wallet_signer_mismatch"
    assert signer_match.expected_signer == WALLET_A.lower()
    assert signer_match.actual_signer == WALLET_B.lower()
    assert signer_match.linked_wallets == [WALLET_A.lower()]
    assert verdict.signer_sanctions == {"kind": "clear"}


def test_get_signer_verdict_returns_none_when_no_signer_blocks() -> None:
    """Operator-token-only paths leave signer_match + signer_sanctions absent."""
    client = AgentScoreCore(api_key=API_KEY)

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {"decision": "allow", "decision_reasons": [], "resolved_operator": "op_x"}
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        client.check(address=WALLET_A)

    assert client.get_signer_verdict(WALLET_A) is None


def test_get_signer_verdict_returns_none_when_address_not_cached() -> None:
    """No assess call yet → no cache entry → no verdict."""
    client = AgentScoreCore(api_key=API_KEY)
    assert client.get_signer_verdict(WALLET_A) is None


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
    from agentscore_commerce.identity.core import TokenDeniedError

    client = AgentScoreCore(api_key=API_KEY)
    mock_resp = _mock_401("token_expired", {"action": "deliver_verify_url_and_poll"})
    with patch.object(client._sync_client, "post", return_value=mock_resp):
        try:
            client.check(operator_token="opc_expired")
        except TokenDeniedError as err:
            assert err.code == "token_expired"
            assert err.body.get("next_steps") == {"action": "deliver_verify_url_and_poll"}
        else:
            pytest.fail("expected TokenDeniedError")


def test_check_raises_token_denied_on_401_revoked() -> None:
    from agentscore_commerce.identity.core import TokenDeniedError

    client = AgentScoreCore(api_key=API_KEY)
    with patch.object(client._sync_client, "post", return_value=_mock_401("token_expired")):
        try:
            client.check(operator_token="opc_revoked")
        except TokenDeniedError as err:
            assert err.code == "token_expired"
            assert "next_steps" not in err.body
        else:
            pytest.fail("expected TokenDeniedError")


def test_check_raises_runtime_error_on_401_unknown_code() -> None:
    """401 with an unrecognized error code falls through to generic RuntimeError, not TokenDeniedError."""
    from agentscore_commerce.identity.core import TokenDeniedError

    client = AgentScoreCore(api_key=API_KEY)
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

    from agentscore_commerce.identity.core import TokenDeniedError

    client = AgentScoreCore(api_key=API_KEY)
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
        respx.post("https://api.agentscore.com/v1/assess").mock(
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
    assert json.loads(body["agent_instructions"]) == {"action": "deliver_verify_url_and_poll"}


# ---------------------------------------------------------------------------
# denial_reason_to_body — agent_memory + wallet-signer-match field marshalling
# ---------------------------------------------------------------------------


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
    """Bootstrap denial carries agent_instructions that describe the full probe strategy."""
    from agentscore_commerce.identity._response import build_missing_identity_reason, denial_reason_to_body

    reason = build_missing_identity_reason()
    assert reason.agent_instructions is not None

    instructions = json.loads(reason.agent_instructions)
    assert instructions["action"] == "probe_identity_then_session"
    assert isinstance(instructions["steps"], list)
    assert len(instructions["steps"]) >= 3
    assert "X-Operator-Token" in instructions["user_message"] or "X-Wallet-Address" in instructions["user_message"]

    body = denial_reason_to_body(reason)
    body_instructions = json.loads(body["agent_instructions"])
    assert body_instructions["action"] == "probe_identity_then_session"


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
