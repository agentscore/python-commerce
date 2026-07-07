"""Compute-first settle-path tests with fake x402_server + compose_mppx.

Covers _handle_x402_settle and _handle_mpp_settle paths.
"""

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentscore_commerce.checkout_compute_first import (
    ComputeFirstCheckout,
    ComputeFirstMppContext,
    ComputeFirstMppResult,
    ComputeFirstRails,
    ComputeFirstRequest,
    ComputeFirstSettledContext,
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


def _x402_header(network: str = X402_NETWORK, pay_to: str = X402_PAY_TO) -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": network,
        "accepted": {"network": network, "payTo": pay_to, "scheme": "exact"},
        "payload": {"authorization": {"from": "0xeb2Ca790F72787c7e61bC6c861353a1e4ACDFCa5"}},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def _run_one(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    return WorkOutcome(result_count=1, body={"matches": ["a"], "total": 1})


async def _run_two(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    return WorkOutcome(result_count=2, body={"matches": ["hit1", "hit2"], "total": 2})


def _req(headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> ComputeFirstRequest:
    return ComputeFirstRequest(
        method="POST",
        url="https://api.example.com/search",
        headers=headers or {},
        body=body or {"q": "test"},
    )


@pytest.mark.asyncio
async def test_x402_settle_full_roundtrip() -> None:
    server = _make_fake_x402_server()
    handler = ComputeFirstCheckout(
        name="x402_full",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=server,
        run_work=_run_two,
    )
    body = {"q": "acme"}
    # Probe
    probe_status, _pb, _ph = await handler.handle(_req(body=body))
    assert probe_status == 402
    # Settle
    status, response_body, _h = await handler.handle(_req(headers={"x-payment": _x402_header()}, body=body))
    assert status == 200
    assert response_body["payment_status"] == "completed"
    assert response_body["charged_usd"] == "0.02"
    assert "Base" in response_body["rail"]
    assert response_body["result"]["matches"] == ["hit1", "hit2"]


@pytest.mark.asyncio
async def test_x402_settle_failure_returns_502() -> None:
    server = _make_fake_x402_server()
    server.settle_payment = AsyncMock(side_effect=RuntimeError("facilitator rejected"))
    handler = ComputeFirstCheckout(
        name="x402_fail",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=server,
        run_work=_run_one,
    )
    body = {"q": "x"}
    await handler.handle(_req(body=body))
    status, response_body, _h = await handler.handle(_req(headers={"x-payment": _x402_header()}, body=body))
    assert status == 502
    assert response_body["error"]["code"] == "settle_failed"


@pytest.mark.asyncio
async def test_invalid_x402_header_returns_400() -> None:
    server = _make_fake_x402_server()
    handler = ComputeFirstCheckout(
        name="x402_bad_hdr",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=server,
        run_work=_run_one,
    )
    body = {"q": "bad"}
    await handler.handle(_req(body=body))
    status, _b, _h = await handler.handle(_req(headers={"x-payment": "not-base64-json"}, body=body))
    assert 400 <= status < 500


@pytest.mark.asyncio
async def test_x402_settle_rejects_agent_controlled_pay_to() -> None:
    """payTo-binding (funds-drain guard): a payload whose signed payTo points at an
    AGENT-controlled wallet — not the configured x402_base recipient — is rejected before any
    on-chain settle, so the agent can't re-route funds away from the merchant.
    """
    server = _make_fake_x402_server()
    handler = ComputeFirstCheckout(
        name="x402_paytoswap",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),  # configured recipient is X402_PAY_TO
        x402_server=server,
        run_work=_run_one,
    )
    body = {"q": "drain"}
    await handler.handle(_req(body=body))  # probe → caches the quote
    # Agent swaps payTo to a wallet IT controls (a valid EVM address ≠ the merchant recipient).
    attacker_pay_to = "0xAAaaAaAAaAaAaAAAAAaAAaaaAaAaaAAAaaaaAAaA"
    status, response_body, _h = await handler.handle(
        _req(headers={"x-payment": _x402_header(pay_to=attacker_pay_to)}, body=body)
    )
    assert 400 <= status < 500
    # Rejected at verification — settle_payment must NOT have run for the swapped recipient.
    server.settle_payment.assert_not_called()
    assert response_body["error"]["code"] in ("payment_proof_invalid", "payment_required")


@pytest.mark.asyncio
async def test_x402_on_settled_hook_fires_and_errors_caught() -> None:
    server = _make_fake_x402_server()
    settled_calls = []

    async def _on_settled(ctx: ComputeFirstSettledContext) -> None:
        settled_calls.append(ctx)
        raise RuntimeError("hook broken — should be caught")

    handler = ComputeFirstCheckout(
        name="x402_onsettled",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=server,
        run_work=_run_one,
        on_settled=_on_settled,
    )
    body = {"q": "x"}
    await handler.handle(_req(body=body))
    status, _rb, _h = await handler.handle(_req(headers={"x-payment": _x402_header()}, body=body))
    # Should be 200 despite the hook throwing
    assert status == 200
    assert len(settled_calls) == 1
    assert settled_calls[0].rail == "x402"


@pytest.mark.asyncio
async def test_mpp_settle_success_returns_200() -> None:
    server = _make_fake_x402_server()

    async def _compose(ctx: ComputeFirstMppContext) -> ComputeFirstMppResult:
        auth_present = (ctx.request.headers.get("authorization") or "").startswith("Payment ")
        if not auth_present:
            return ComputeFirstMppResult(status=402, headers={"www-authenticate": 'Payment realm="x"'})
        return ComputeFirstMppResult(
            status=200,
            raw=type("FakeRaw", (), {"receipt": type("R", (), {"method": "tempo"})()})(),
            tx_hash="pi_test_123",
            signer_address="0xsigner",
            signer_network="evm",
        )

    handler = ComputeFirstCheckout(
        name="mpp_full",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=server,
        run_work=_run_one,
        compose_mppx=_compose,
    )
    body = {"q": "mpp"}
    await handler.handle(_req(body=body))
    status, response_body, _h = await handler.handle(_req(headers={"authorization": "Payment <base64>"}, body=body))
    assert status == 200
    assert "Tempo" in response_body["rail"]
    assert response_body.get("payment_intent_id") == "pi_test_123"


@pytest.mark.asyncio
async def test_mpp_settle_compose_non_200_returns_400() -> None:
    server = _make_fake_x402_server()

    async def _compose(ctx: ComputeFirstMppContext) -> ComputeFirstMppResult:
        return ComputeFirstMppResult(status=402, headers={"www-authenticate": 'Payment realm="x"'})

    handler = ComputeFirstCheckout(
        name="mpp_fail",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=server,
        run_work=_run_one,
        compose_mppx=_compose,
    )
    body = {"q": "mpp_fail"}
    await handler.handle(_req(body=body))
    status, response_body, _h = await handler.handle(_req(headers={"authorization": "Payment <base64>"}, body=body))
    assert status == 400
    assert response_body["error"]["code"] == "mpp_settle_failed"


@pytest.mark.asyncio
async def test_mpp_rail_label_stripe() -> None:
    server = _make_fake_x402_server()

    async def _compose(ctx: ComputeFirstMppContext) -> ComputeFirstMppResult:
        auth_present = (ctx.request.headers.get("authorization") or "").startswith("Payment ")
        if not auth_present:
            return ComputeFirstMppResult(status=402, headers={})
        return ComputeFirstMppResult(
            status=200,
            raw=type("FakeRaw", (), {"receipt": type("R", (), {"method": "stripe"})()})(),
        )

    handler = ComputeFirstCheckout(
        name="mpp_stripe",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=server,
        run_work=_run_one,
        compose_mppx=_compose,
    )
    body = {"q": "stripe"}
    await handler.handle(_req(body=body))
    status, response_body, _h = await handler.handle(_req(headers={"authorization": "Payment <x>"}, body=body))
    assert status == 200
    assert response_body["rail"] == "Stripe (card+link)"


@pytest.mark.asyncio
async def test_mpp_rail_label_unknown_falls_back_to_mpp() -> None:
    server = _make_fake_x402_server()

    async def _compose(ctx: ComputeFirstMppContext) -> ComputeFirstMppResult:
        auth_present = (ctx.request.headers.get("authorization") or "").startswith("Payment ")
        if not auth_present:
            return ComputeFirstMppResult(status=402, headers={})
        return ComputeFirstMppResult(
            status=200,
            raw=type("FakeRaw", (), {"receipt": type("R", (), {"method": "unknown_scheme"})()})(),
        )

    handler = ComputeFirstCheckout(
        name="mpp_unknown",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=server,
        run_work=_run_one,
        compose_mppx=_compose,
    )
    body = {"q": "unknown"}
    await handler.handle(_req(body=body))
    status, response_body, _h = await handler.handle(_req(headers={"authorization": "Payment <x>"}, body=body))
    assert status == 200
    assert response_body["rail"] == "MPP"
