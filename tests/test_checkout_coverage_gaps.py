"""Targeted tests closing remaining coverage gaps in checkout.py.

Covers:
- pre_validate raising CheckoutValidationError (lines 904-921)
- pre_validate returning a state dict (line 921)
- handle_fastapi / handle_aiohttp / handle_flask / handle_django / handle_sanic
  adapter wrappers + invalid-body envelope paths (lines 999-1018 + siblings)
- Auto-derive compose_mppx from mppx_secret_key + mpp rails (lines 695-709)
- zero_settle x402 carve-out happy path (lines 1595-1624)
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from agentscore_commerce.checkout import (
    Checkout,
    CheckoutContext,
    CheckoutRequest,
    CheckoutValidationError,
    PricingResult,
)
from agentscore_commerce.payment.rail_spec import (
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
)

X402_NETWORK = "eip155:84532"
X402_PAY_TO = "0xc3128D86669e842573306CA82f60A005A41C44D4"


def _req(*, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> CheckoutRequest:
    return CheckoutRequest(
        method="POST",
        url="https://api.example/purchase",
        headers=headers or {},
        body=body or {"item": "x"},
    )


def _x402_header(network: str = X402_NETWORK, pay_to: str = X402_PAY_TO) -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": network,
        "accepted": {"network": network, "payTo": pay_to, "scheme": "exact"},
        "payload": {"authorization": {"from": "0xeb2Ca790F72787c7e61bC6c861353a1e4ACDFCa5"}},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


# ─── pre_validate hook paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_validate_validation_error_returns_4xx_with_envelope() -> None:
    async def _pre_validate(_ctx: CheckoutContext) -> dict[str, Any]:
        raise CheckoutValidationError(
            code="out_of_stock",
            message="That product is out of stock.",
            action="select_different_product",
            status=409,
            extra={"product_id": "wine-42"},
        )

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=10.0),
        pre_validate=_pre_validate,
    )
    result = await checkout.handle(_req())
    assert result.status == 409
    assert result.body["error"]["code"] == "out_of_stock"
    assert result.settled is False
    assert result.settle_phase == "pre_validate_failed"


@pytest.mark.asyncio
async def test_pre_validate_returning_state_dict_stashes_on_ctx() -> None:
    seen_state: dict[str, Any] = {}

    async def _pre_validate(_ctx: CheckoutContext) -> dict[str, Any]:
        return {"resolved_product_id": "wine-1", "price_lookup": 12.50}

    def _compute(ctx: CheckoutContext) -> PricingResult:
        seen_state.update(ctx.state)
        return PricingResult(amount_usd=ctx.state.get("price_lookup", 1.0))

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=_compute,
        pre_validate=_pre_validate,
    )
    result = await checkout.handle(_req())
    assert result.status == 402
    assert seen_state["resolved_product_id"] == "wine-1"
    assert seen_state["price_lookup"] == 12.50


# ─── auto-derive compose_mppx from mppx_secret_key + rails ───────────────────


def test_auto_derive_compose_mppx_when_mppx_secret_key_supplied() -> None:
    """Init path: rails has MPP specs + mppx_secret_key → compose_mppx wired."""
    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient="0xtempo"),
            "stripe": StripeRailSpec(profile_id="profile_x"),
        },
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
        mppx_secret_key="X" * 32,
    )
    assert checkout.compose_mppx is not None


def test_auto_derive_compose_mppx_skipped_when_no_mpp_rails() -> None:
    """No MPP rails in the dict → compose_mppx stays None even with secret_key."""
    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient=X402_PAY_TO, network=X402_NETWORK)},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
        mppx_secret_key="X" * 32,
    )
    assert checkout.compose_mppx is None


# ─── zero-settle x402 carve-out ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_settle_x402_carve_out_verifies_credential_skips_settle() -> None:
    settled_outcomes: list[Any] = []

    async def _on_settled(_ctx: CheckoutContext, outcome: Any) -> dict[str, Any]:
        settled_outcomes.append(outcome)
        return {"redeemed": True}

    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient=X402_PAY_TO, network=X402_NETWORK)},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.0),
        x402_server=object(),  # required for _x402_server_available()
        zero_settle_carve_out=True,
        on_settled=_on_settled,
    )
    result = await checkout.handle(_req(headers={"x-payment": _x402_header()}))
    assert result.status == 200
    assert len(settled_outcomes) == 1
    assert settled_outcomes[0].rail == "x402"
    assert settled_outcomes[0].tx_hash is None
    # signer_address gets lifted from the payload.authorization.from
    assert settled_outcomes[0].signer_address is not None
    assert settled_outcomes[0].signer_network == "evm"


@pytest.mark.asyncio
async def test_zero_settle_x402_verify_failure_returns_4xx() -> None:
    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient=X402_PAY_TO, network=X402_NETWORK)},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.0),
        x402_server=object(),
        zero_settle_carve_out=True,
    )
    result = await checkout.handle(_req(headers={"x-payment": "not-base64-json"}))
    assert 400 <= result.status < 500
    assert result.settled is False
    assert result.settle_phase == "verify_failed"


@pytest.mark.asyncio
async def test_zero_settle_mpp_carve_out_returns_200_no_tx() -> None:
    """No x402 header → falls through to MPP $0 carve-out (line 1626-1640)."""
    settled_outcomes: list[Any] = []

    async def _on_settled(_ctx: CheckoutContext, outcome: Any) -> dict[str, Any]:
        settled_outcomes.append(outcome)
        return {}

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.0),
        zero_settle_carve_out=True,
        on_settled=_on_settled,
    )
    result = await checkout.handle(
        _req(headers={"authorization": "Payment opaque-jwt"}),
    )
    assert result.status == 200
    assert len(settled_outcomes) == 1
    assert settled_outcomes[0].rail == "mpp"
    assert settled_outcomes[0].tx_hash is None


# ─── handle_<framework> adapters ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_fastapi_wraps_handle_in_jsonresponse() -> None:
    from starlette.requests import Request

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )
    # Construct a Starlette Request with a JSON body.
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
        "headers": [(b"content-type", b"application/json")],
        "scheme": "http",
        "server": ("api.example", 80),
        "client": ("127.0.0.1", 12345),
    }
    request = Request(scope, receive=_receive)
    response = await checkout.handle_fastapi(request)
    assert response.status_code == 402
    assert b"accepted_methods" in response.body


@pytest.mark.asyncio
async def test_handle_fastapi_invalid_body_returns_400() -> None:
    """Non-JSON body → 400 invalid_body envelope (line 1004-1005)."""
    from starlette.requests import Request

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )

    received = False

    async def _receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"not json", "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/purchase",
        "raw_path": b"/purchase",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("api.example", 80),
    }
    request = Request(scope, receive=_receive)
    response = await checkout.handle_fastapi(request)
    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"]["code"] == "invalid_body"


@pytest.mark.asyncio
async def test_handle_fastapi_explicit_body_skips_parsing() -> None:
    """Pass ``body=`` to bypass request.json() parsing."""
    from starlette.requests import Request

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/purchase",
        "raw_path": b"/purchase",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("api.example", 80),
    }

    async def _receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    request = Request(scope, receive=_receive)
    response = await checkout.handle_fastapi(request, body={"item": "preparsed"})
    assert response.status_code == 402


@pytest.mark.asyncio
async def test_handle_aiohttp_invalid_body_returns_400() -> None:
    """aiohttp adapter: non-JSON body → 400 envelope."""
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )

    class _FakeReq:
        method = "POST"
        url = "https://api.example/purchase"
        headers: dict[str, str] = {}

        async def json(self) -> dict[str, Any]:
            raise ValueError("malformed")

    response = await checkout.handle_aiohttp(_FakeReq())
    assert response.status == 400


def test_handle_flask_invalid_body_returns_400() -> None:
    """Flask adapter: get_json returning None → 400 envelope."""
    from flask import Flask

    app = Flask(__name__)
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )
    with app.test_request_context("/purchase", method="POST", data=b"not json", content_type="text/plain"):
        from flask import request as flask_request

        resp = checkout.handle_flask(flask_request)
        assert resp.status_code == 400


def test_handle_django_invalid_body_returns_400() -> None:
    """Django adapter: invalid JSON in request.body → 400 envelope."""
    # Configure Django minimally if not already configured.
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False,
            ALLOWED_HOSTS=["*"],
            DATABASES={},
            INSTALLED_APPS=[],
            USE_TZ=True,
        )
        django.setup()
    else:
        settings.ALLOWED_HOSTS = ["*"]

    from django.test import RequestFactory

    rf = RequestFactory(SERVER_NAME="testserver")
    request = rf.post(
        "/purchase",
        data=b"not valid json",
        content_type="application/json",
    )
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )
    response = checkout.handle_django(request)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_handle_sanic_invalid_body_returns_400() -> None:
    """Sanic adapter: request.json raising → 400 envelope."""
    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xtempo")},
        url="https://api.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=1.0),
    )

    class _FakeSanicReq:
        method = "POST"
        url = "https://api.example/purchase"
        headers: dict[str, str] = {}

        @property
        def json(self) -> dict[str, Any]:
            raise RuntimeError("malformed body")

    response = await checkout.handle_sanic(_FakeSanicReq())
    assert response.status == 400
