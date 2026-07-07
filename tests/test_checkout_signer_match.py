"""Checkout enforces ``signer_match`` on the wallet-signing settle leg.

The gate's primary /v1/assess call composes a ``signer_match`` verdict when a payment signer is
extracted. node-commerce's ``Checkout.runGate`` converts a non-``pass`` verdict into a 403; this
suite pins the python parity: a wallet-signer mismatch on a wallet-signing rail (x402 EIP-3009)
DENIES with ``wallet_signer_mismatch`` rather than settling.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx
from starlette.requests import Request

from agentscore_commerce.checkout import Checkout, CheckoutGateConfig, PricingResult
from agentscore_commerce.payment.rail_spec import X402BaseRailSpec

ASSESS_URL = "https://api.agentscore.com/v1/assess"

CLAIMED_WALLET = "0x1111111111111111111111111111111111111111"
# The payment signer recovered from the x402 payload — a DIFFERENT wallet than claimed.
ACTUAL_SIGNER = "0x2222222222222222222222222222222222222222"
LINKED_WALLET = "0x3333333333333333333333333333333333333333"


class _StubX402Server:
    """Settle path fake — only reached if the gate WRONGLY allowed the mismatch."""

    def build_payment_requirements(self, _config: Any) -> list[Any]:
        return [{"scheme": "exact", "network": "eip155:8453"}]

    async def verify_payment(self, _payload: Any, _req: Any) -> Any:
        from dataclasses import dataclass

        @dataclass
        class _V:
            is_valid: bool = True

        return _V()

    async def settle_payment(self, _payload: Any, _req: Any) -> Any:
        return {"success": True, "transaction": "0xtx", "network": "eip155:8453", "payer": ACTUAL_SIGNER}


def _x402_payment_header(signer: str) -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "accepted": {"network": "eip155:8453", "payTo": "0x000000000000000000000000000000000000dEaD"},
        "payload": {"authorization": {"from": signer, "to": "0x000000000000000000000000000000000000dEaD"}},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _settle_request(*, wallet: str, signer: str) -> Request:
    """Build a Starlette settle-leg Request carrying a wallet header + x402 payment header."""
    headers = [
        (b"content-type", b"application/json"),
        (b"x-wallet-address", wallet.encode()),
        (b"x-payment", _x402_payment_header(signer).encode()),
    ]
    body_bytes = json.dumps({"item": "wine"}).encode()
    received = False

    async def _receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/purchase",
        "raw_path": b"/purchase",
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("api.example", 80),
        "client": ("127.0.0.1", 12345),
        # The gate lazily wires its flat-denial exception handler off request.app; a bare
        # Starlette Request has no "app" in scope (KeyError). In production handle_fastapi gets
        # a real app-bound request. None is enough for the handler-install to no-op here.
        "app": None,
    }
    return Request(scope, receive=_receive)


def _mock_assess_signer_mismatch() -> respx.Route:
    # decision=allow → compliance passes; the ONLY problem is the signer doesn't match the
    # claimed wallet. Without signer-match enforcement Checkout would proceed to settle.
    return respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "decision": "allow",
                "decision_reasons": [],
                "resolved_operator": "op_claimed",
                "signer_match": {
                    "kind": "wallet_signer_mismatch",
                    "claimed_operator": "op_claimed",
                    "signer_operator": "op_other",
                    "expected_signer": CLAIMED_WALLET,
                    "actual_signer": ACTUAL_SIGNER,
                    "linked_wallets": [LINKED_WALLET],
                },
            },
        )
    )


#: x402 settle binds the agent-supplied payTo to the configured recipient (payTo-binding fix),
#: so the rail recipient must equal the payload's payTo for the pass-path settle to run.
TREASURY = "0x000000000000000000000000000000000000dEaD"


def _make_checkout() -> Checkout:
    return Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient=TREASURY)},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_StubX402Server(),
        gate=CheckoutGateConfig(api_key="ask_test", require_kyc=True),
    )


@pytest.mark.asyncio
@respx.mock
async def test_signer_mismatch_on_wallet_signing_rail_denies_not_settles() -> None:
    _mock_assess_signer_mismatch()
    checkout = _make_checkout()
    request = _settle_request(wallet=CLAIMED_WALLET, signer=ACTUAL_SIGNER)
    response = await checkout.handle_fastapi(request)

    assert response.status_code == 403
    body = json.loads(response.body)
    assert body["error"]["code"] == "wallet_signer_mismatch"
    # Body carries the actionable mismatch fields so the agent can re-sign / switch identity.
    assert body["expected_signer"] == CLAIMED_WALLET
    assert body["actual_signer"] == ACTUAL_SIGNER
    assert body["linked_wallets"] == [LINKED_WALLET]
    assert body["claimed_operator"] == "op_claimed"
    # actual_signer_operator is always emitted for wallet_signer_mismatch (string = signer resolves
    # to a DIFFERENT operator; the assess mock returns signer_operator="op_other").
    assert body["actual_signer_operator"] == "op_other"
    # PARITY (issue #5): Checkout emits the SAME body shape as node-commerce's Checkout.runGate —
    # the recovery hint rides in `agent_instructions` (denial_reason_to_body), NOT in the standalone
    # build_signer_mismatch_body helper's `next_steps` container. So: agent_instructions present with
    # the canonical resign action, and NO next_steps key.
    assert "next_steps" not in body
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "resign_or_switch_to_operator_token"
    # CRITICAL: the settle never ran — no on-chain capture for a mismatched signer.
    assert "transaction" not in body


@pytest.mark.asyncio
@respx.mock
async def test_signer_match_pass_allows_settle() -> None:
    # Control: when signer_match is `pass`, Checkout proceeds to settle as before.
    respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "decision": "allow",
                "decision_reasons": [],
                "resolved_operator": "op_claimed",
                "signer_match": {
                    "kind": "pass",
                    "claimed_operator": "op_claimed",
                    "signer_operator": "op_claimed",
                    "actual_signer": CLAIMED_WALLET,
                },
            },
        )
    )
    checkout = _make_checkout()
    request = _settle_request(wallet=CLAIMED_WALLET, signer=CLAIMED_WALLET)
    response = await checkout.handle_fastapi(request)
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body.get("reference_id")


@pytest.mark.asyncio
@respx.mock
async def test_wallet_auth_requires_wallet_signing_denies() -> None:
    # The other non-pass kind: claimed a wallet on a rail with no recoverable signer match.
    respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "decision": "allow",
                "decision_reasons": [],
                "resolved_operator": "op_claimed",
                "signer_match": {
                    "kind": "wallet_auth_requires_wallet_signing",
                    "claimed_wallet": CLAIMED_WALLET,
                },
            },
        )
    )
    checkout = _make_checkout()
    request = _settle_request(wallet=CLAIMED_WALLET, signer=ACTUAL_SIGNER)
    response = await checkout.handle_fastapi(request)
    assert response.status_code == 403
    body = json.loads(response.body)
    assert body["error"]["code"] == "wallet_auth_requires_wallet_signing"
    # Same parity shape: agent_instructions container (node Checkout.runGate), no next_steps.
    assert "next_steps" not in body
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "switch_to_operator_token"
