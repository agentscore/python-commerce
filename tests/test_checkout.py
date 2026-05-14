"""Tests for the Checkout orchestrator covering every flexibility axis.

Matrix:

* x402-only / MPP-only / both
* self-custody (chain rails) / custodial (Stripe) / mixed
* gated identity / ungated
* goods seller (on_settled persists order) / API seller (on_settled returns inline body)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentscore_commerce.checkout import (
    Checkout,
    CheckoutContext,
    CheckoutRequest,
    MppxComposeOutcome,
    PricingResult,
)
from agentscore_commerce.payment.rail_spec import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
)


def _req(*, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> CheckoutRequest:
    return CheckoutRequest(
        method="POST",
        url="https://api.example/purchase",
        headers=headers or {},
        body=body or {"item": "wine"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 402 emit — every rail combination
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_402_x402_only_no_mppx_no_identity() -> None:
    """API seller pattern: x402-only, anonymous (no assess), per-call billing."""
    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0xTREASURY")},
        url="https://api.example/call",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=None,
        # x402_base_network omitted — emit-only, no settle handler
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    assert result.settled is False
    assert "accepted_methods" in result.body
    assert result.reference_id


@pytest.mark.asyncio
async def test_emit_402_mpp_only_no_x402() -> None:
    """MPP-only goods seller: tempo + stripe SPT, no x402."""
    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient="0xtempo"),
            "stripe": StripeRailSpec(profile_id="profile_x"),
        },
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=250.0),
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    # No PAYMENT-REQUIRED header since x402 isn't configured
    assert "payment-required" not in result.headers


@pytest.mark.asyncio
async def test_emit_402_all_rails_with_x402_payment_required() -> None:
    """Multi-rail merchant: every supported rail advertised, x402 PAYMENT-REQUIRED layered."""

    @dataclass
    class _FakeX402Server:
        _schemes: dict[str, dict[str, Any]] = field(default_factory=dict)

        def build_payment_requirements(self, config: Any) -> Any:
            class _Req:
                def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
                    return {
                        "scheme": "exact",
                        "network": config.network,
                        "payTo": config.pay_to,
                        "maxAmountRequired": "10000",
                        "extra": {"name": "USD Coin", "version": "2"},
                    }

            return [_Req()]

    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient="0xtempo"),
            "x402_base": X402BaseRailSpec(recipient="0xbase"),
            "solana_mpp": SolanaMppRailSpec(recipient="solanaaddr"),
            "stripe": StripeRailSpec(profile_id="profile_x"),
        },
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        x402_server=_FakeX402Server(),
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    assert "payment-required" in result.headers


@pytest.mark.asyncio
async def test_emit_402_custodial_only_stripe() -> None:
    """Custodial-only merchant: Stripe SPT only, no chain rails."""
    checkout = Checkout(
        rails={"stripe": StripeRailSpec(profile_id="profile_x")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=50.0),
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    assert result.body["accepted_methods"]


# ─────────────────────────────────────────────────────────────────────────────
# x402 settle path
# ─────────────────────────────────────────────────────────────────────────────


class _StubX402Server:
    """Minimal x402 server fake — exercises settle path without real x402 deps.

    Mirrors x402 2.9's ``x402ResourceServer`` surface enough to pass
    ``process_x402_settle``: ``build_payment_requirements(config) -> [req]``,
    ``verify_payment(payload, req)``, ``settle_payment(payload, req)``.
    """

    def __init__(self, *, settle_success: bool = True) -> None:
        self.settle_success = settle_success

    def build_payment_requirements(self, _config: Any) -> list[Any]:
        return [{"scheme": "exact", "network": "eip155:8453"}]

    async def verify_payment(self, _payload: Any, _requirement: Any) -> Any:
        @dataclass
        class _Verified:
            is_valid: bool = True

        return _Verified()

    async def settle_payment(self, _payload: Any, _requirement: Any) -> Any:
        # ProcessX402Settle treats a falsy success as a settle_failed phase via the
        # exception raised by settle_result_to_json_bytes when it tries to serialise
        # an empty dict, so we shape the response as a plain JSON-serializable dict.
        if not self.settle_success:
            raise RuntimeError("settle rejected")
        return {
            "success": True,
            "transaction": "0xtx",
            "network": "eip155:8453",
            "payer": "0xpayer",
        }


def _x402_headers_with_payload() -> dict[str, str]:
    import base64
    import json

    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "accepted": {
            "network": "eip155:8453",
            "payTo": "0x000000000000000000000000000000000000dEaD",
        },
        "payload": {
            "authorization": {
                "from": "0xPAYER",
                "to": "0x000000000000000000000000000000000000dEaD",
            },
        },
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"x-payment": encoded}


@pytest.mark.asyncio
async def test_x402_settle_success_runs_on_settled_hook() -> None:
    """Goods seller: on_settled persists the order; success body merges reference_id."""
    on_settled = AsyncMock(return_value={"order_status": "queued"})
    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0xTREASURY")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_StubX402Server(settle_success=True),
        on_settled=on_settled,
    )
    result = await checkout.handle(_req(headers=_x402_headers_with_payload()))
    assert result.status == 200
    assert result.settled is True
    assert result.body["order_status"] == "queued"
    assert result.body["reference_id"] == result.reference_id
    on_settled.assert_awaited_once()
    ctx_arg, outcome_arg = on_settled.await_args.args
    assert isinstance(ctx_arg, CheckoutContext)
    assert outcome_arg.rail == "x402"


@pytest.mark.asyncio
async def test_x402_settle_failure_returns_4xx_with_phase() -> None:
    """Settle failure surfaces ``payment_proof_invalid`` + ``settle_phase`` for diagnostics."""
    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0xTREASURY")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_StubX402Server(settle_success=False),
    )
    result = await checkout.handle(_req(headers=_x402_headers_with_payload()))
    assert result.status == 400
    assert result.settled is False
    assert result.settle_phase is not None
    assert result.body["error"]["code"] == "payment_proof_invalid"


# ─────────────────────────────────────────────────────────────────────────────
# MPP compose path (via compose_mppx hook)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_mppx_returns_200_runs_on_settled() -> None:
    """When pympp validates the credential, compose_mppx returns 200 and Checkout
    runs ``on_settled``."""
    on_settled = AsyncMock(return_value=None)
    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(status=200, payment_response_header="ok"),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
        on_settled=on_settled,
    )
    result = await checkout.handle(_req(headers={"authorization": "Payment id=abc"}))
    assert result.status == 200
    assert result.headers["payment-response"] == "ok"
    on_settled.assert_awaited_once()


@pytest.mark.asyncio
async def test_compose_mppx_returns_402_composes_rich_body() -> None:
    """When pympp re-emits 402, Checkout layers the rich body on top of pympp's WWW-Auth."""
    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(
            status=402,
            headers={"www-authenticate": 'Payment id="ord_x"'},
        ),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(_req(headers={"authorization": "Payment id=abc"}))
    assert result.status == 402
    assert result.headers["www-authenticate"] == 'Payment id="ord_x"'
    assert "accepted_methods" in result.body


# ─────────────────────────────────────────────────────────────────────────────
# Custom hooks: pricing, recipient minting, reference id
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_pricing_can_branch_on_identity() -> None:
    """Identity-aware pricing: KYC'd agents get a different price."""

    def price(ctx: CheckoutContext) -> PricingResult:
        if ctx.request.assess and ctx.request.assess.get("identity_status") == "verified":
            return PricingResult(amount_usd=8.0)
        return PricingResult(amount_usd=10.0)

    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0xTREASURY")},
        url="https://api.example/call",
        compute_pricing=price,
    )
    # Anonymous
    anon = await checkout.handle(_req())
    assert anon.body["amount_usd"] == "10.0"
    # KYC'd
    verified = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/call",
            headers={},
            body={"item": "x"},
            assess={"identity_status": "verified"},
        ),
    )
    assert verified.body["amount_usd"] == "8.0"


@pytest.mark.asyncio
async def test_mint_recipients_overrides_rail_recipients() -> None:
    """Stripe-multichain pattern: per-order deposit addresses replace static treasury."""

    def mint(_ctx: CheckoutContext) -> dict[str, str]:
        return {"tempo": "0xPERORDER_TEMPO", "x402_base": "0xPERORDER_BASE"}

    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient="0xstatic_tempo"),
            "x402_base": X402BaseRailSpec(recipient="0xstatic_base"),
        },
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=100.0),
        mint_recipients=mint,
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    # The 402 body's accepted_methods should reflect the minted recipients.
    accepted_str = str(result.body["accepted_methods"])
    assert "0xPERORDER_TEMPO" in accepted_str
    assert "0xPERORDER_BASE" in accepted_str


@pytest.mark.asyncio
async def test_mint_reference_id_runs_when_provided() -> None:
    """Goods sellers mint their own order_id (e.g. against their orders table)."""

    async def mint() -> str:
        return "ord_abc123"

    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0xTREASURY")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
        mint_reference_id=lambda _ctx: mint(),
    )
    result = await checkout.handle(_req())
    assert result.reference_id == "ord_abc123"


# ─────────────────────────────────────────────────────────────────────────────
# Init guards
# ─────────────────────────────────────────────────────────────────────────────


def test_init_requires_x402_base_railspec_when_x402_server_provided() -> None:
    """x402_server demands an X402BaseRailSpec in rails['x402_base'] — the rail's
    `network` field carries the CAIP-2, so there's no separate kwarg to forget."""
    with pytest.raises(ValueError, match="X402BaseRailSpec"):
        Checkout(
            rails={"tempo": TempoRailSpec(recipient="0xT")},
            url="https://x.example",
            compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
            x402_server=object(),
        )
