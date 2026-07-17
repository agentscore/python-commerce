"""Credential shape gate: junk payment headers are rejected BEFORE any merchant
hook (pre_validate / pricing / minting) or the identity-gate assess call runs."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from agentscore_commerce.checkout import (
    Checkout,
    CheckoutRequest,
    MppxComposeOutcome,
    PricingResult,
)
from agentscore_commerce.payment import TempoRailSpec, X402BaseRailSpec, malformed_payment_credential

VALID_MPP = (
    "Payment "
    + base64.b64encode(
        json.dumps(
            {"challenge": {"id": "ch_1", "realm": "api.example"}, "payload": {"type": "hash", "hash": "0xabc"}}
        ).encode()
    ).decode()
)


def _req(headers: dict[str, str]) -> CheckoutRequest:
    return CheckoutRequest(
        method="POST",
        url="https://api.example/purchase",
        headers=headers,
        body={"item": "wine"},
    )


def test_malformed_payment_credential_classifies_channels() -> None:
    assert malformed_payment_credential({"authorization": "Payment total-garbage!!!"}) is not None
    assert malformed_payment_credential({"x-payment": "!!!garbage!!!"}) is not None
    assert malformed_payment_credential({"authorization": VALID_MPP}) is None
    # JWT-shaped tokens pass (Stripe SPT and other token-style credentials).
    assert malformed_payment_credential({"authorization": "Payment eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ4In0.c2ln"}) is None
    # No payment header at all → nothing to classify.
    assert malformed_payment_credential({}) is None


@pytest.mark.asyncio
async def test_junk_mpp_header_rechallenges_with_fresh_402_no_pre_validate() -> None:
    calls: list[str] = []

    async def _pre_validate(_ctx: Any) -> dict[str, Any]:
        calls.append("pre_validate")
        return {}

    async def _compose(_ctx: Any) -> MppxComposeOutcome:
        calls.append("compose")
        return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="fresh"'})

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dEaD")},
        url="https://api.example/purchase",
        pre_validate=_pre_validate,
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=_compose,
    )
    result = await checkout.handle(_req({"authorization": "Payment total-garbage!!!"}))
    assert result.status == 402
    assert result.settle_phase == "credential_malformed"
    assert result.headers["www-authenticate"] == 'Payment realm="fresh"'
    # The junk credential must not burn the merchant's paid probe.
    assert "pre_validate" not in calls


@pytest.mark.asyncio
async def test_junk_x402_header_rechallenges_with_fresh_402_no_pre_validate() -> None:
    calls: list[str] = []

    async def _pre_validate(_ctx: Any) -> dict[str, Any]:
        calls.append("pre_validate")
        return {}

    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0x" + "00" * 19 + "dEaD")},
        url="https://api.example/purchase",
        pre_validate=_pre_validate,
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=object(),
    )
    result = await checkout.handle(_req({"x-payment": "!!!garbage!!!"}))
    assert result.status == 402
    # A fresh challenge the agent can re-pay against, not a bare error body.
    assert result.body["accepted_methods"] is not None
    assert result.settle_phase == "credential_malformed"
    # Junk must not burn the merchant's paid probe.
    assert calls == []


@pytest.mark.asyncio
async def test_credential_pre_check_false_opts_out() -> None:
    calls: list[str] = []

    async def _pre_validate(_ctx: Any) -> dict[str, Any]:
        calls.append("pre_validate")
        return {}

    async def _compose(_ctx: Any) -> MppxComposeOutcome:
        calls.append("compose")
        return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="t"'})

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dEaD")},
        url="https://api.example/purchase",
        pre_validate=_pre_validate,
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=_compose,
        credential_pre_check=False,
    )
    result = await checkout.handle(_req({"authorization": "Payment total-garbage!!!"}))
    assert "pre_validate" in calls
    assert result.settle_phase != "credential_malformed"


@pytest.mark.asyncio
async def test_x402_header_at_tempo_only_merchant_not_enforced() -> None:
    async def _compose(_ctx: Any) -> MppxComposeOutcome:
        return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="t"'})

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dEaD")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=_compose,
    )
    result = await checkout.handle(_req({"x-payment": "!!!garbage!!!"}))
    # No x402 rail → the junk x402 header is ignored and the request falls
    # through to the anonymous discovery leg (402 with rails), same as before.
    assert result.status == 402
