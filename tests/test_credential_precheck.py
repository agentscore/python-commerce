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
from agentscore_commerce.errors import CheckoutValidationError
from agentscore_commerce.payment import TempoRailSpec, X402BaseRailSpec, malformed_payment_credential


class _RawHeaders:
    """A framework-style headers mapping (case-insensitive .get) for a fake raw request."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._m = {k.lower(): v for k, v in mapping.items()}

    def get(self, key: str, default: Any = None) -> Any:
        return self._m.get(key.lower(), default)

    def items(self) -> Any:
        return list(self._m.items())

    def __iter__(self) -> Any:
        return iter(self._m)


class _RawReq:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = _RawHeaders(headers)


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
async def test_junk_mpp_header_rechallenges_with_fresh_402_discovery_flow() -> None:
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
    # Treated as a discovery request: pre_validate runs (pricing depends on its state).
    assert "pre_validate" in calls


@pytest.mark.asyncio
async def test_rechallenge_strips_credential_from_raw_request_too() -> None:
    # Regression: the re-challenge must be a discovery leg for EVERY view of the
    # request, including the native ``ctx.request.raw`` that hooks like
    # ``mint_multichain_recipients`` read. A hook parsing the MPP credential off
    # ``ctx.request.raw`` and raising on junk (the martin-estate shape) would
    # otherwise turn the fresh-402 re-challenge back into a 401 dead end.
    raw_auth_seen: dict[str, Any] = {}

    async def _mint(ctx: Any) -> dict[str, str]:
        raw = ctx.request.raw
        auth = raw.headers.get("authorization") if raw is not None else None
        raw_auth_seen["value"] = auth
        if auth is not None and auth.startswith("Payment "):
            raise CheckoutValidationError(
                code="invalid_credential",
                message="The Authorization: Payment header is not a valid MPP credential.",
                action="retry_without_credential",
                status=401,
            )
        return {"tempo": "0xtempo"}

    async def _compose(_ctx: Any) -> MppxComposeOutcome:
        return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="fresh"'})

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dEaD")},
        url="https://api.example/purchase",
        pre_validate=lambda _ctx: {},
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=_compose,
        mint_recipients=_mint,
    )
    req = CheckoutRequest(
        method="POST",
        url="https://api.example/purchase",
        headers={"authorization": "Payment total-garbage!!!"},
        body={"item": "wine"},
        raw=_RawReq({"authorization": "Payment total-garbage!!!", "x-wallet-address": "0xabc"}),
    )
    result = await checkout.handle(req)
    assert result.status == 402
    assert result.settle_phase == "credential_malformed"
    # The hook ran on the re-entry and saw a raw with the credential stripped;
    # non-payment headers (x-wallet-address) still pass through.
    assert raw_auth_seen["value"] is None


@pytest.mark.asyncio
async def test_junk_x402_header_rechallenges_with_fresh_402_discovery_flow() -> None:
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
    # Discovery flow: pre_validate runs.
    assert calls == ["pre_validate"]


@pytest.mark.asyncio
async def test_malformed_credential_runs_pre_validate_so_stateful_pricing_survives() -> None:
    # Regression: compute_pricing reads state that pre_validate populates (the
    # martin-estate shape). The malformed re-challenge must run pre_validate
    # first, or pricing dereferences missing state and 500s.
    async def _pre_validate(_ctx: Any) -> dict[str, Any]:
        return {"product": {"price_cents": 4800}}

    def _compute_pricing(ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=ctx.state["product"]["price_cents"] / 100)

    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0x" + "00" * 19 + "dEaD")},
        url="https://api.example/purchase",
        pre_validate=_pre_validate,
        compute_pricing=_compute_pricing,
        x402_server=object(),
    )
    result = await checkout.handle(_req({"x-payment": "not-decodable"}))
    assert result.status == 402
    assert result.body["accepted_methods"] is not None


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
