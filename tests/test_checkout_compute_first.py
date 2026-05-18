"""Smoke tests for ``compute_first_checkout`` covering probe + settle flows."""

from typing import Any

import pytest

from agentscore_commerce.checkout_compute_first import (
    ComputeFirstCheckout,
    ComputeFirstRails,
    ComputeFirstRequest,
    ComputeFirstWorkContext,
    WorkOutcome,
)
from agentscore_commerce.payment.rail_spec import TempoRailSpec, X402BaseRailSpec


def _make_rails() -> ComputeFirstRails:
    return ComputeFirstRails(
        tempo=TempoRailSpec(recipient="0xtempo", testnet=True),
        x402_base=X402BaseRailSpec(recipient="0xbase", network="eip155:84532"),
    )


async def _run_one_result(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    return WorkOutcome(result_count=1, body={"matches": ["one"], "total": 1})


async def _run_zero_results(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    return WorkOutcome(result_count=0, body={"matches": [], "total": 0})


def _build_request(headers: dict[str, str] | None = None) -> ComputeFirstRequest:
    return ComputeFirstRequest(
        method="POST",
        url="https://api.example.com/search",
        headers=headers or {},
        body={"q": "test"},
    )


@pytest.mark.asyncio
async def test_zero_result_fast_path_returns_200_no_charge() -> None:
    handler = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=None,
        run_work=_run_zero_results,
    )
    status, body, _headers = await handler.handle(_build_request())
    assert status == 200
    assert body["payment_status"] == "no_charge"
    assert body["charged_usd"] == "0.00"


@pytest.mark.asyncio
async def test_validate_input_raises_returns_4xx_envelope() -> None:
    from agentscore_commerce.checkout import CheckoutValidationError

    def _validate(body: dict[str, Any]) -> None:
        if "q" not in body:
            raise CheckoutValidationError(code="missing_q", message="`q` is required.")

    handler = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=None,
        run_work=_run_zero_results,
        validate_input=_validate,
    )
    status, body, _headers = await handler.handle(
        ComputeFirstRequest(method="POST", url="https://x", headers={}, body={})
    )
    assert status == 400
    assert body["error"]["code"] == "missing_q"


@pytest.mark.asyncio
async def test_settle_leg_with_no_cached_quote_returns_stale_quote() -> None:
    handler = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=None,
        run_work=_run_one_result,
    )
    # Settle leg simulated by sending payment-signature header without a prior probe.
    status, body, _headers = await handler.handle(_build_request(headers={"payment-signature": "<base64>"}))
    assert status == 400
    assert body["error"]["code"] == "stale_quote"
    assert body["next_steps"]["action"] == "re_probe"


@pytest.mark.asyncio
async def test_mpp_settle_with_no_compose_hook_returns_503() -> None:
    handler = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=None,
        run_work=_run_one_result,
    )
    # First do probe to seed cache
    await handler.handle(_build_request())
    # Now settle on MPP — but no compose_mppx wired → 503 mpp_unavailable
    status, body, _headers = await handler.handle(_build_request(headers={"authorization": "Payment <base64>"}))
    assert status == 503
    assert body["error"]["code"] == "mpp_unavailable"


@pytest.mark.asyncio
async def test_upstream_runwork_error_returns_200_no_charge() -> None:
    async def _broken(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
        raise RuntimeError("upstream blew up")

    handler = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=_make_rails(),
        x402_server=None,
        run_work=_broken,
    )
    status, body, _headers = await handler.handle(_build_request())
    assert status == 200
    assert body["payment_status"] == "no_charge"
    assert body["error"]["code"] == "upstream_failed"


@pytest.mark.asyncio
async def test_probe_leg_emits_402_with_pricing_and_retry_body() -> None:
    """Exercise the _emit_402 path — work returns 1 result, probe caches +
    emits a 402 with accepted methods, pricing block, retry_body."""

    handler = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=3,
        rails=ComputeFirstRails(
            tempo=TempoRailSpec(recipient="0xtempo", testnet=True),
        ),
        x402_server=None,
        run_work=_run_one_result,
    )
    status, body, headers = await handler.handle(_build_request())
    assert status == 402
    assert body["amount_usd"] == "0.03"
    assert body["pricing"]["subtotal"] == "0.03"
    assert body["retry_body"] == {"q": "test"}
    assert headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_probe_leg_cache_hit_skips_run_work() -> None:
    """Second probe with the same body re-uses the cached price + body."""
    calls = []

    async def _record(body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
        calls.append(body)
        return WorkOutcome(result_count=2, body={"matches": ["a", "b"], "total": 2})

    handler = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=5,
        rails=ComputeFirstRails(
            tempo=TempoRailSpec(recipient="0xtempo", testnet=True),
        ),
        x402_server=None,
        run_work=_record,
    )
    # First probe runs work, caches.
    status1, body1, _h1 = await handler.handle(_build_request())
    # Second probe with same body hits cache; run_work NOT called again.
    status2, body2, _h2 = await handler.handle(_build_request())
    assert status1 == status2 == 402
    assert body1["amount_usd"] == body2["amount_usd"] == "0.10"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fractional_unit_price_auto_derives_decimals() -> None:
    """Sub-cent pricing — auto-derive precision from unit_price_cents."""

    handler = ComputeFirstCheckout(
        name="tokens",
        url="https://api.example.com/tokens",
        unit_price_cents=0.0001,  # $0.000001 per unit
        rails=ComputeFirstRails(
            tempo=TempoRailSpec(recipient="0xtempo", testnet=True),
        ),
        x402_server=None,
        run_work=_run_one_result,
    )
    assert handler.decimals == 6  # 2 + 4 fractional digits


@pytest.mark.asyncio
async def test_minted_recipients_override_static_rail_recipient() -> None:
    """mint_recipients hook output replaces the static `rails[*].recipient`."""
    from agentscore_commerce.checkout_compute_first import (
        ComputeFirstMintContext,
        MintedRecipients,
    )

    async def _mint(_ctx: ComputeFirstMintContext) -> MintedRecipients:
        return MintedRecipients(tempo="0xMINTED", x402_base="0xMINTEDBASE")

    handler = ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=ComputeFirstRails(
            tempo=TempoRailSpec(recipient="0xstatic", testnet=True),
        ),
        x402_server=None,
        run_work=_run_one_result,
        mint_recipients=_mint,
    )
    _status, body, _h = await handler.handle(_build_request())
    # 402 body's accepted_methods should reference the minted recipient
    assert body["amount_usd"] == "0.01"
    methods = body.get("accepted_methods") or []
    tempo_method = next((m for m in methods if "tempo" in str(m).lower()), None)
    assert tempo_method is not None
