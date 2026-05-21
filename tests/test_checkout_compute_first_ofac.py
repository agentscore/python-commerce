"""Tests for wallet OFAC SDN enforcement in compute_first_checkout (TEC-311).

Mirrors `tests/test_checkout_wallet_ofac_default.py` for the variable-cost
compute-first helper. Covers _enforce_wallet_sanctions paths:
  - SDN signer → 403 deny before rail handler fires
  - clean signer → continue to rail handler
  - no AGENTSCORE_API_KEY → log+skip
  - no extractable signer (Stripe SPT) → silent skip
  - API outage → 503 fail-closed
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentscore_commerce.checkout_compute_first import (
    ComputeFirstCheckout,
    ComputeFirstRails,
    ComputeFirstRequest,
    ComputeFirstWorkContext,
    WorkOutcome,
)
from agentscore_commerce.payment.rail_spec import TempoRailSpec, X402BaseRailSpec

X402_NETWORK = "eip155:84532"
X402_PAY_TO = "0xc3128D86669e842573306CA82f60A005A41C44D4"


def _make_rails() -> ComputeFirstRails:
    return ComputeFirstRails(
        tempo=TempoRailSpec(recipient="0xtempo", testnet=True),
        x402_base=X402BaseRailSpec(recipient=X402_PAY_TO, network=X402_NETWORK),
    )


def _make_fake_x402_server() -> MagicMock:
    server = MagicMock()
    server.build_payment_requirements = MagicMock(
        return_value=[
            {
                "scheme": "exact",
                "network": X402_NETWORK,
                "payTo": X402_PAY_TO,
                "maxAmountRequired": "10000",
                "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                "resource": "https://api.example.com/search",
                "description": "test",
                "mimeType": "application/json",
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USDC", "version": "2"},
            },
        ]
    )
    server.enrich_extensions = MagicMock(return_value=None)
    server.verify_payment = AsyncMock(return_value={"is_valid": True})
    server.settle_payment = AsyncMock(
        return_value={"success": True, "transaction": "0xdeadbeef", "network": X402_NETWORK},
    )
    return server


def _x402_header(payer: str = "0xeb2Ca790F72787c7e61bC6c861353a1e4ACDFCa5") -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": X402_NETWORK,
        "accepted": {"network": X402_NETWORK, "payTo": X402_PAY_TO, "scheme": "exact"},
        "payload": {"authorization": {"from": payer}},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def _run_one(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    return WorkOutcome(result_count=1, body={"matches": ["a"], "total": 1})


def _req(headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> ComputeFirstRequest:
    return ComputeFirstRequest(
        method="POST",
        url="https://api.example.com/search",
        headers=headers or {},
        body=body or {"q": "test"},
    )


def _checkout() -> ComputeFirstCheckout:
    return ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=_make_fake_x402_server(),
        run_work=_run_one,
    )


def _reset_warned_flag() -> None:
    import agentscore_commerce.checkout_compute_first as cf

    cf._WARNED_NO_API_KEY = False


@pytest.mark.asyncio
async def test_sdn_signer_denies_before_x402_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDN signer on settle leg → 403 + wallet_not_trusted; x402 verify
    never fires."""
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")
    fake_server = _make_fake_x402_server()
    checkout = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=fake_server,
        run_work=_run_one,
    )
    # Prime the cache with a probe
    await checkout.handle(_req(body={"q": "x"}))
    with patch(
        "agentscore_commerce.api.AgentScore.aassess",
        new=AsyncMock(return_value={"decision": "deny", "decision_reasons": ["sanctions_flagged"]}),
    ):
        result = await checkout.handle(_req(headers={"x-payment": _x402_header()}, body={"q": "x"}))
    assert result[0] == 403
    body = result[1]
    assert body["error"]["code"] == "wallet_not_trusted"
    # x402 settle path must NOT have fired
    fake_server.verify_payment.assert_not_called()


@pytest.mark.asyncio
async def test_clean_signer_continues_to_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")
    fake_server = _make_fake_x402_server()
    checkout = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=fake_server,
        run_work=_run_one,
    )
    await checkout.handle(_req(body={"q": "x"}))
    with patch(
        "agentscore_commerce.api.AgentScore.aassess",
        new=AsyncMock(return_value={"decision": "allow", "decision_reasons": []}),
    ):
        result = await checkout.handle(_req(headers={"x-payment": _x402_header()}, body={"q": "x"}))
    assert result[0] == 200
    fake_server.verify_payment.assert_called_once()


@pytest.mark.asyncio
async def test_no_api_key_warns_once_and_skips(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("AGENTSCORE_API_KEY", raising=False)
    _reset_warned_flag()
    import logging

    caplog.set_level(logging.WARNING)
    checkout = _checkout()
    await checkout.handle(_req(body={"q": "x"}))
    result = await checkout.handle(_req(headers={"x-payment": _x402_header()}, body={"q": "x"}))
    assert result[0] == 200  # OFAC skipped → settle continues
    warns = [r for r in caplog.records if "AGENTSCORE_API_KEY is not set" in r.message]
    assert len(warns) == 1


@pytest.mark.asyncio
async def test_no_signer_skips_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authorization-style header but no extractable wallet signer (Stripe
    SPT-shaped) → skip OFAC silently, settle path continues."""
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")
    checkout = _checkout()
    await checkout.handle(_req(body={"q": "x"}))
    with (
        patch(
            "agentscore_commerce.payment.signer.extract_payment_signer",
            return_value=None,
        ),
        patch("agentscore_commerce.api.AgentScore.aassess", new=AsyncMock()) as mock_aassess,
    ):
        await checkout.handle(_req(headers={"x-payment": _x402_header()}, body={"q": "x"}))
        mock_aassess.assert_not_called()


@pytest.mark.asyncio
async def test_api_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTSCORE_API_KEY", "ask_test_key")
    checkout = _checkout()
    await checkout.handle(_req(body={"q": "x"}))

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("connection refused")

    with patch("agentscore_commerce.api.AgentScore.aassess", new=_raise):
        result = await checkout.handle(_req(headers={"x-payment": _x402_header()}, body={"q": "x"}))
    assert result[0] == 503
    assert result[1]["error"]["code"] == "api_error"
