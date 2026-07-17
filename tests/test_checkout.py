"""Tests for the Checkout orchestrator covering every flexibility axis.

Matrix:

* x402-only / MPP-only / both
* self-custody (chain rails) / custodial (Stripe) / mixed
* gated identity / ungated
* goods seller (on_settled persists order) / API seller (on_settled returns inline body)
"""

from __future__ import annotations

import importlib.util
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
from agentscore_commerce.payment.default_rails import build_default_checkout_rails
from agentscore_commerce.payment.rail_spec import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
)

_X402_INSTALLED = importlib.util.find_spec("x402") is not None


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


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
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
async def test_emit_402_carries_resource_info_and_extensions_in_body_and_header() -> None:
    """x402 v2: resource_info + discovery_extensions ride in both the body and the
    base64 PAYMENT-REQUIRED header, where Bazaar validators read them."""
    import base64
    import json as _json

    @dataclass
    class _FakeX402Server:
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
        rails={"x402_base": X402BaseRailSpec(recipient="0xbase")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_FakeX402Server(),
        resource_info={
            "serviceName": "Example Enrich",
            "tags": ["data", "enrichment"],
            "iconUrl": "https://ex.com/i.png",
        },
        discovery_extensions={"com.coinbase.bazaar": {"info": {}, "schema": {}}},
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    assert result.body["resource"]["serviceName"] == "Example Enrich"
    assert result.body["extensions"] == {"com.coinbase.bazaar": {"info": {}, "schema": {}}}
    decoded = _json.loads(base64.b64decode(result.headers["payment-required"]))
    assert decoded["resource"]["serviceName"] == "Example Enrich"
    assert decoded["resource"]["tags"] == ["data", "enrichment"]
    assert decoded["resource"]["iconUrl"] == "https://ex.com/i.png"
    assert decoded["extensions"] == {"com.coinbase.bazaar": {"info": {}, "schema": {}}}


@pytest.mark.asyncio
async def test_emit_402_resource_url_is_https_behind_tls_terminating_proxy() -> None:
    """Behind ALB / CloudFront the inbound url is http://; X-Forwarded-Proto: https
    must yield an https resource.url (x402 discovery requires https)."""
    import base64
    import json as _json

    @dataclass
    class _FakeX402Server:
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
        rails={"x402_base": X402BaseRailSpec(recipient="0xbase")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_FakeX402Server(),
    )
    request = CheckoutRequest(
        method="POST",
        url="http://api.example/purchase",
        headers={"x-forwarded-proto": "https"},
        body={},
    )
    result = await checkout.handle(request)
    assert result.status == 402
    decoded = _json.loads(base64.b64decode(result.headers["payment-required"]))
    assert decoded["resource"]["url"] == "https://api.example/purchase"


@pytest.mark.asyncio
async def test_emit_402_enriches_bazaar_extension_with_request_method() -> None:
    """info.input.method (required by the v2 discovery schema) is filled from the
    request method on the declared Bazaar extension, in both body and header."""
    import base64
    import json as _json

    @dataclass
    class _FakeX402Server:
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
        rails={"x402_base": X402BaseRailSpec(recipient="0xbase")},
        url="https://api.example/company/base",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_FakeX402Server(),
        discovery_extensions={
            "bazaar": {
                "info": {"input": {"type": "http", "bodyType": "json"}},
                "schema": {"properties": {"input": {"required": ["type"]}}},
            }
        },
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    assert result.body["extensions"]["bazaar"]["info"]["input"]["method"] == "POST"
    decoded = _json.loads(base64.b64decode(result.headers["payment-required"]))
    assert decoded["extensions"]["bazaar"]["info"]["input"]["method"] == "POST"


@pytest.mark.asyncio
async def test_emit_402_advertises_identity_metadata_when_wallet_header_present() -> None:
    """Wallet-mode 402 pre-advertises required_signer + signer_constraint.

    Without an X-Wallet-Address header the block is omitted entirely; with one
    it appears so agents self-correct at discovery instead of at the 403 retry.
    """
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
    )
    # No wallet header → no identity_metadata block on the 402 body.
    result_no_wallet = await checkout.handle(_req())
    assert "identity_mode" not in result_no_wallet.body

    # Wallet header present → required_signer is advertised.
    result_wallet = await checkout.handle(_req(headers={"X-Wallet-Address": "0xCAFEBEEF"}))
    assert result_wallet.body["identity_mode"] == "wallet"
    assert result_wallet.body["required_signer"] == "0xCAFEBEEF"
    assert "signer_constraint" in result_wallet.body


@pytest.mark.asyncio
async def test_emit_402_identity_metadata_lifts_linked_wallets_from_assess() -> None:
    """When the gate populated request.assess with linked_wallets, the 402 echoes them."""
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
    )
    req = CheckoutRequest(
        method="POST",
        url="https://api.example/purchase",
        headers={"X-Wallet-Address": "0xCAFEBEEF"},
        body={"item": "wine"},
        assess={"identity": {"linked_wallets": ["0xSIBLING1", "0xSIBLING2"]}},
    )
    result = await checkout.handle(req)
    assert result.body["required_signer"] == "0xCAFEBEEF"
    assert result.body["linked_wallets"] == ["0xSIBLING1", "0xSIBLING2"]


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


def _x402_headers_with_payload(pay_to: str = "0x000000000000000000000000000000000000dEaD") -> dict[str, str]:
    import base64
    import json

    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "accepted": {
            "network": "eip155:8453",
            "payTo": pay_to,
        },
        "payload": {
            "authorization": {
                "from": "0xPAYER",
                "to": pay_to,
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
        # Recipient must equal the payload's signed payTo — the gate binds the agent-supplied payTo
        # to the configured recipient (payTo-binding fix), so they must match for the settle to run.
        rails={"x402_base": X402BaseRailSpec(recipient="0x000000000000000000000000000000000000dEaD")},
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
        # payTo-binding: recipient must equal the payload's signed payTo (see settle_success test).
        rails={"x402_base": X402BaseRailSpec(recipient="0x000000000000000000000000000000000000dEaD")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_StubX402Server(settle_success=False),
    )
    result = await checkout.handle(_req(headers=_x402_headers_with_payload()))
    # settle_failed phase classifies to 503 payment_provider_unavailable
    # (transient on-chain settle outage; agent should retry or pick another rail).
    assert result.status == 503
    assert result.settled is False
    assert result.settle_phase == "settle_failed"
    assert result.body["error"]["code"] == "payment_provider_unavailable"


@pytest.mark.asyncio
async def test_x402_settle_rejects_agent_controlled_pay_to() -> None:
    """payTo-binding (funds-drain guard): when the merchant uses a static-treasury x402 rail and
    did NOT supply a custom is_cached_address, the agent-supplied signed payTo is bound to the
    configured recipient. A payload pointing payTo at a wallet the AGENT controls is rejected
    BEFORE settle, so funds can't be re-routed away from the merchant.
    """
    settled = AsyncMock()
    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0x000000000000000000000000000000000000dEaD")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_StubX402Server(settle_success=True),
        on_settled=settled,
    )
    # Agent swaps payTo to a wallet it owns (valid EVM address ≠ the configured recipient).
    attacker_pay_to = "0xAAaaAaAAaAaAaAAAAAaAAaaaAaAaaAAAaaaaAAaA"
    result = await checkout.handle(_req(headers=_x402_headers_with_payload(pay_to=attacker_pay_to)))
    assert result.status == 400
    assert result.settled is False
    assert result.settle_phase == "verify_failed"
    # The settle never ran and on_settled never fired for the swapped recipient.
    settled.assert_not_called()


@pytest.mark.asyncio
async def test_x402_settle_custom_is_cached_address_still_honored() -> None:
    """Merchants minting per-order deposit addresses pass their own is_cached_address; the
    payTo-binding defers to it (so a minted payTo ≠ the static recipient still settles).
    """
    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0x000000000000000000000000000000000000dEaD")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_StubX402Server(settle_success=True),
        # Merchant attests this minted payTo belongs to this order — accept it even though it's
        # not the static recipient.
        is_cached_address=lambda addr: addr.lower() == "0xfeedfacefeedfacefeedfacefeedfacefeedface",
    )
    result = await checkout.handle(
        _req(headers=_x402_headers_with_payload(pay_to="0xFEEDFACEfeedfacEFEEDFACEFEEDFACEFEEDFACE"))
    )
    assert result.status == 200
    assert result.settled is True


@pytest.mark.asyncio
async def test_x402_default_rails_empty_recipient_sentinel_binds_to_minted_pay_to() -> None:
    """Regression: the documented per-order-mint default — ``build_default_checkout_rails`` leaves
    ``recipient=""`` (the sentinel) and the real payTo is minted per request via ``mint_recipients``.

    Previously the empty-string sentinel passed through ``_resolve_static_x402_recipient`` and the
    bind degraded to ``addr.lower() == ""`` → rejected EVERY honest minted payTo (400 verify_failed),
    breaking the canonical x402 flow. The fix treats the blank sentinel as "no static recipient" and
    binds to the per-request minted ``recipients["x402_base"]`` instead. So an honest minted payTo
    settles while an attacker-swapped payTo is still rejected.
    """
    minted_pay_to = "0x000000000000000000000000000000000000dEaD"

    def mint(_ctx: CheckoutContext) -> dict[str, str]:
        return {"x402_base": minted_pay_to}

    settled = AsyncMock(return_value={"ok": True})
    checkout = Checkout(
        # Default-rails shape: recipient="" sentinel, no is_cached_address. mint_recipients supplies
        # the real per-order payTo at request time (Stripe-multichain pattern).
        rails=build_default_checkout_rails(x402_base={}),
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_StubX402Server(settle_success=True),
        mint_recipients=mint,
        on_settled=settled,
    )
    # Honest minted payTo → accepted + settled.
    ok = await checkout.handle(_req(headers=_x402_headers_with_payload(pay_to=minted_pay_to)))
    assert ok.status == 200
    assert ok.settled is True
    settled.assert_awaited_once()

    # Attacker swaps payTo to a wallet it controls → rejected BEFORE settle (funds-drain guard holds).
    settled.reset_mock()
    attacker = "0xAAaaAaAAaAaAaAAAAAaAAaaaAaAaaAAAaaaaAAaA"
    bad = await checkout.handle(_req(headers=_x402_headers_with_payload(pay_to=attacker)))
    assert bad.status == 400
    assert bad.settled is False
    assert bad.settle_phase == "verify_failed"
    settled.assert_not_called()


@pytest.mark.asyncio
async def test_x402_static_recipient_plus_mint_recipients_binds_to_minted() -> None:
    """Regression (payTo bind B): a rail can carry BOTH a static recipient AND mint_recipients (the
    static recipient is the discovery/sentinel default; the per-request mint is the real payTo).

    Binding to the construction-time static recipient here would reject the legit minted payTo on the
    settle path. The fix binds to the per-request ``recipients["x402_base"]`` (exactly as the
    compute-first path already does), so the minted payTo settles and the now-stale static recipient
    is rejected.
    """
    static_recipient = "0xc3128D86669e842573306CA82f60A005A41C44D4"
    minted_pay_to = "0x000000000000000000000000000000000000dEaD"

    def mint(_ctx: CheckoutContext) -> dict[str, str]:
        return {"x402_base": minted_pay_to}

    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient=static_recipient)},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=_StubX402Server(settle_success=True),
        mint_recipients=mint,
    )
    # The per-request minted payTo settles...
    minted_res = await checkout.handle(_req(headers=_x402_headers_with_payload(pay_to=minted_pay_to)))
    assert minted_res.status == 200
    assert minted_res.settled is True
    # ...and the (now-stale) construction-time static recipient is rejected.
    static_res = await checkout.handle(_req(headers=_x402_headers_with_payload(pay_to=static_recipient)))
    assert static_res.status == 400
    assert static_res.settled is False


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
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 200
    assert result.headers["payment-response"] == "ok"
    on_settled.assert_awaited_once()


@pytest.mark.asyncio
async def test_mppx_settle_leg_resolves_recipients_for_compose_and_on_settled() -> None:
    """Regression (parity with node ``checkout.ts`` resolve-before-dispatch): a
    ``mint_recipients``-based MPP merchant must see ``ctx.recipients`` populated in BOTH
    ``compose_mppx`` and ``on_settled`` on the settle leg. ``_handle_mppx`` previously never called
    ``_resolve_recipients`` (unlike the x402 / zero-settle / 402 handlers), so the per-request minted
    recipient was missing for both hooks. The fix resolves up-front in ``_handle_mppx``."""
    minted = "0x000000000000000000000000000000000000dEaD"
    seen: dict[str, dict[str, str]] = {}

    def mint(_ctx: CheckoutContext) -> dict[str, str]:
        return {"tempo": minted}

    async def compose(ctx: CheckoutContext) -> MppxComposeOutcome:
        seen["compose"] = dict(ctx.recipients)
        return MppxComposeOutcome(status=200, payment_response_header="ok")

    async def on_settled(ctx: CheckoutContext, _outcome: object) -> None:
        seen["on_settled"] = dict(ctx.recipients)

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        mint_recipients=mint,
        compose_mppx=compose,
        on_settled=on_settled,
    )
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 200
    # Both hooks saw the per-request minted recipient (not an empty dict).
    assert seen["compose"] == {"tempo": minted}
    assert seen["on_settled"] == {"tempo": minted}


@pytest.mark.asyncio
async def test_compose_mppx_payment_receipt_header_surfaces_on_response() -> None:
    """When ``compose_mppx`` populates ``payment_receipt_header``, Checkout echoes
    it as a ``payment-receipt`` HTTP header on the success response — symmetric
    to the existing ``payment_response_header`` (x402) behavior."""
    receipt_header = "eyJzdGF0dXMiOiJzdWNjZXNzIn0"
    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(status=200, payment_receipt_header=receipt_header),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 200
    assert result.headers["payment-receipt"] == receipt_header


@pytest.mark.asyncio
async def test_compose_mppx_omitted_payment_receipt_header_emits_no_header() -> None:
    """Default ``payment_receipt_header=None`` on the compose outcome must NOT
    emit an empty ``payment-receipt`` header on the response."""
    compose_mppx = AsyncMock(return_value=MppxComposeOutcome(status=200))
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 200
    assert "payment-receipt" not in result.headers


@pytest.mark.asyncio
async def test_compose_mppx_auto_extracts_receipt_header_from_raw_dict() -> None:
    """When a custom compose_mppx returns ``raw={'credential': c, 'receipt': r}``
    (the auto-built hook's shape) without explicitly setting
    ``payment_receipt_header``, Checkout lifts the header from ``r.to_payment_receipt()``."""

    class _Receipt:
        @staticmethod
        def to_payment_receipt() -> str:
            return "auto-from-raw-dict"

    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(status=200, raw={"credential": object(), "receipt": _Receipt()}),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 200
    assert result.headers["payment-receipt"] == "auto-from-raw-dict"


@pytest.mark.asyncio
async def test_compose_mppx_auto_extracts_receipt_header_from_raw_tuple() -> None:
    """``raw=(credential, receipt)`` (the pympp Mpp.charge return) is also a
    recognized shape — the second element's ``to_payment_receipt()`` is lifted."""

    class _Receipt:
        @staticmethod
        def to_payment_receipt() -> str:
            return "auto-from-raw-tuple"

    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(status=200, raw=(object(), _Receipt())),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 200
    assert result.headers["payment-receipt"] == "auto-from-raw-tuple"


@pytest.mark.asyncio
async def test_compose_mppx_explicit_payment_receipt_header_wins_over_raw() -> None:
    """When the hook sets ``payment_receipt_header`` explicitly, the auto-extract
    from ``raw`` is NOT consulted."""

    class _Receipt:
        @staticmethod
        def to_payment_receipt() -> str:
            return "auto-IGNORED"

    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(
            status=200,
            payment_receipt_header="explicit-value",
            raw={"receipt": _Receipt()},
        ),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 200
    assert result.headers["payment-receipt"] == "explicit-value"


@pytest.mark.asyncio
async def test_compose_mppx_receipt_to_header_that_throws_falls_through() -> None:
    """If ``to_payment_receipt()`` raises (unsupported pympp version, malformed
    receipt), the SDK omits the header rather than emitting a malformed value."""

    class _BadReceipt:
        def to_payment_receipt(self) -> str:
            raise RuntimeError("malformed receipt")

    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(status=200, raw={"receipt": _BadReceipt()}),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 200
    assert "payment-receipt" not in result.headers


@pytest.mark.asyncio
async def test_compose_mppx_returns_402_on_settle_leg_rejects_credential() -> None:
    """When the agent sends Authorization: Payment and mppx returns 402 (credential
    rejected), Checkout returns that 402 with the fresh WWW-Authenticate from mppx
    so the agent's retry signs against the new directive, not a dead-end 400."""
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
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 402
    assert result.headers["www-authenticate"] == 'Payment id="ord_x"'
    assert result.body["error"]["code"] == "payment_proof_invalid"
    assert result.settle_phase == "verify_failed"


@pytest.mark.asyncio
async def test_compose_mppx_402_with_keychain_failure_surfaces_tempo_key_not_registered() -> None:
    """When the compose hook captures a Tempo keychain rejection in failure_reason,
    Checkout returns 401 ``tempo_key_not_registered`` instead of the generic
    ``payment_proof_invalid`` so ``tempo request`` / ``agentscore-pay`` can route the user
    to the WebAuthn enrollment flow."""
    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(
            status=402,
            headers={"www-authenticate": 'Payment id="ord_x"'},
            failure_reason="RPC Request failed. (keychain validation failed: AccountKeychainError(KeyNotFound(KeyNotFound)))",
        ),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(
        _req(
            headers={
                "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19"
            }
        )
    )
    assert result.status == 401
    assert result.body["error"]["code"] == "tempo_key_not_registered"
    assert result.settle_phase == "verify_failed"


@pytest.mark.asyncio
async def test_compose_mppx_on_discovery_leg_layers_challenge_in_402() -> None:
    """On the discovery leg (no Authorization header), Checkout calls compose_mppx
    proactively to mint a fresh WWW-Authenticate, then composes it into the 402."""
    compose_mppx = AsyncMock(
        return_value=MppxComposeOutcome(
            status=402,
            headers={"www-authenticate": 'Payment id="ord_y"'},
        ),
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        compose_mppx=compose_mppx,
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    assert result.headers["www-authenticate"] == 'Payment id="ord_y"'
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
    assert anon.body["amount_usd"] == "10.00"
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
    assert verified.body["amount_usd"] == "8.00"


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


def test_rails_key_for_mppx_method_picks_the_matching_spec() -> None:
    """``_rails_key_for_mppx_method`` maps mppx credential methods (``tempo`` /
    ``solana`` / ``stripe``) to the merchant's actual rails-dict key, so MPP
    settlements distinguish Solana from Tempo (both fall under
    ``SettleOutcome.rail="mpp"``) and from Stripe SPT.
    """
    checkout = Checkout(
        rails={
            "tempo_charge": TempoRailSpec(recipient="0xtempo"),
            "x402_base": X402BaseRailSpec(recipient="0xbase"),
            "sol_rail": SolanaMppRailSpec(recipient="solanaaddr"),
            "stripe_spt": StripeRailSpec(profile_id="profile_x"),
        },
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )
    assert checkout._rails_key_for_mppx_method("tempo") == "tempo_charge"
    assert checkout._rails_key_for_mppx_method("solana") == "sol_rail"
    assert checkout._rails_key_for_mppx_method("stripe") == "stripe_spt"
    assert checkout._rails_key_for_mppx_method("unknown") is None


def test_rails_key_for_mppx_method_returns_none_when_rail_absent() -> None:
    """When the merchant hasn't registered a rail for the credential method,
    the helper returns ``None`` so the caller can fall back to
    ``_mpp_rail_key()`` or a hard-coded default."""
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )
    assert checkout._rails_key_for_mppx_method("solana") is None
    assert checkout._rails_key_for_mppx_method("stripe") is None
    assert checkout._rails_key_for_mppx_method("tempo") == "tempo"
