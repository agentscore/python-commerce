"""Tests for the always-on wallet OFAC SDN enforcement default (TEC-311).

Mirrors node-commerce's `seamless-helpers.test.ts` wallet-OFAC suite. Covers:
  - SDN signer → 403 deny on settle, no rail handler fires
  - clean signer → settle proceeds
  - no AGENTSCORE_API_KEY → log+skip (dev/testnet pattern)
  - Stripe SPT (no extractable signer) → silent skip
  - API outage → fail-closed (503)
  - gate config without api_key → falls through to wallet-OFAC enforcement
  - hasIdentityGate() trigger for TEC-312 boilerplate (agent_memory / opc references)
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from agentscore_commerce.checkout import (
    Checkout,
    CheckoutGateConfig,
    CheckoutRequest,
    PricingResult,
)
from agentscore_commerce.payment.rail_spec import X402BaseRailSpec

ASSESS_URL = "https://api.agentscore.sh/v1/assess"
RECIPIENT = "0x1111111111111111111111111111111111111111"
SDN_WALLET = "0xdead000000000000000000000000000000000bad"
CLEAN_WALLET = "0xaaa0000000000000000000000000000000000099"


def _x402_payment_header(payer: str) -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": "eip155:84532",
        "payload": {
            "signature": "0x" + "ee" * 65,
            "authorization": {
                "from": payer,
                "to": RECIPIENT,
                "value": "100000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "00" * 32,
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _req(headers: dict[str, str] | None = None) -> CheckoutRequest:
    return CheckoutRequest(
        method="POST",
        url="https://api.example/purchase",
        headers=headers or {},
        body={"item": "wine"},
    )


def _checkout(*, gate: CheckoutGateConfig | None = None) -> Checkout:
    return Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient=RECIPIENT, network="eip155:84532")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
        gate=gate,
    )


def _mock_assess(decision: str = "allow", reasons: list[str] | None = None) -> respx.Route:
    return respx.post(ASSESS_URL).mock(
        return_value=httpx.Response(200, json={"decision": decision, "decision_reasons": reasons or []})
    )


def _reset_warned_flag() -> None:
    """The module-level warn-once flag carries across tests; reset between
    cases that exercise the missing-key branch so each test sees a fresh
    'first call' state."""
    import agentscore_commerce.checkout as checkout_mod

    checkout_mod._WARNED_NO_API_KEY = False


@pytest.mark.asyncio
@respx.mock
async def test_sdn_signer_with_no_gate_denies_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """No gate config + payment header with SDN signer → /v1/assess deny → 403."""
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")
    _mock_assess("deny", reasons=["sanctions_flagged"])
    checkout = _checkout(gate=None)
    request = _req(headers={"x-payment": _x402_payment_header(SDN_WALLET)})
    result = await checkout.handle(request)
    assert result.status == 403
    assert result.settled is False


@pytest.mark.asyncio
@respx.mock
async def test_clean_signer_with_no_gate_allows_settle_to_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No gate config + clean signer → /v1/assess allow → settle path continues."""
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")
    _mock_assess("allow", reasons=[])
    checkout = _checkout(gate=None)
    request = _req(headers={"x-payment": _x402_payment_header(CLEAN_WALLET)})
    result = await checkout.handle(request)
    # No x402 server configured; the settle path will fail downstream — but the
    # OFAC gate ITSELF must have allowed (not denied). status != 403.
    assert result.status != 403 or "wallet_not_trusted" not in str(result.body)


@pytest.mark.asyncio
async def test_no_api_key_logs_warn_once_and_skips(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without AGENTSCORE_API_KEY, OFAC enforcement is skipped and a single
    warning is logged. Subsequent settles do NOT re-log."""
    monkeypatch.delenv("AGENTSCORE_API_KEY", raising=False)
    _reset_warned_flag()
    checkout = _checkout(gate=None)
    import logging

    caplog.set_level(logging.WARNING)
    request = _req(headers={"x-payment": _x402_payment_header(CLEAN_WALLET)})
    await checkout.handle(request)
    first_warns = [r for r in caplog.records if "AGENTSCORE_API_KEY is not set" in r.message]
    assert len(first_warns) >= 1
    caplog.clear()
    # Second call: same instance, flag already set → no new warning
    await checkout.handle(request)
    second_warns = [r for r in caplog.records if "AGENTSCORE_API_KEY is not set" in r.message]
    assert len(second_warns) == 0


@pytest.mark.asyncio
async def test_no_signer_skips_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stripe SPT (no extractable wallet signer) → skip OFAC, settle continues.
    Simulated by mocking extract_payment_signer to return None even though a
    payment header is present."""
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")
    with (
        patch("agentscore_commerce.payment.signer.extract_payment_signer", return_value=None),
        patch("agentscore_commerce.api.AgentScore.aassess", new=AsyncMock()) as mock_aassess,
    ):
        checkout = _checkout(gate=None)
        # Use Stripe SPT-style Authorization header
        request = _req(headers={"authorization": "Payment ZmFrZS1zcHQ="})
        await checkout.handle(request)
        # OFAC path must NOT have called /v1/assess (no signer to screen)
        mock_aassess.assert_not_called()


@pytest.mark.asyncio
async def test_api_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When /v1/assess raises (network failure / 5xx), return 503 — strict
    liability fail-closed."""
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("connection refused")

    with patch("agentscore_commerce.api.AgentScore.aassess", new=_raise):
        checkout = _checkout(gate=None)
        request = _req(headers={"x-payment": _x402_payment_header(CLEAN_WALLET)})
        result = await checkout.handle(request)
        assert result.status == 503


@pytest.mark.asyncio
@respx.mock
async def test_gate_without_api_key_falls_through_to_wallet_ofac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-fix: gate.api_key = None + require_kyc → silent allow (runGate
    returned null). Now: fall through to wallet OFAC enforcement so the
    merchant at least gets the strict-liability floor instead of nothing."""
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")
    _mock_assess("deny", reasons=["sanctions_flagged"])
    # api_key omitted from the gate; require_kyc set but un-enforceable
    gate = CheckoutGateConfig(api_key=None, require_kyc=True)
    checkout = _checkout(gate=gate)
    request = _req(headers={"x-payment": _x402_payment_header(SDN_WALLET)})
    result = await checkout.handle(request)
    # Should NOT silently allow; the fallback OFAC check must fire and deny on SDN.
    assert result.status == 403


def test_has_identity_gate_true_when_require_kyc_set() -> None:
    gate = CheckoutGateConfig(api_key="ask_x", require_kyc=True)
    checkout = _checkout(gate=gate)
    assert checkout._has_identity_gate() is True


def test_has_identity_gate_true_when_min_age_set() -> None:
    gate = CheckoutGateConfig(api_key="ask_x", min_age=21)
    checkout = _checkout(gate=gate)
    assert checkout._has_identity_gate() is True


def test_has_identity_gate_true_when_jurisdictions_set() -> None:
    gate = CheckoutGateConfig(api_key="ask_x", blocked_jurisdictions=["CU"])
    checkout = _checkout(gate=gate)
    assert checkout._has_identity_gate() is True


def test_has_identity_gate_false_when_only_api_key_set() -> None:
    """Wallet-OFAC-only mode: gate.api_key without identity-bearing flags
    should NOT count as an identity gate."""
    gate = CheckoutGateConfig(api_key="ask_x")
    checkout = _checkout(gate=gate)
    assert checkout._has_identity_gate() is False


def test_has_identity_gate_false_when_no_gate() -> None:
    checkout = _checkout(gate=None)
    assert checkout._has_identity_gate() is False
