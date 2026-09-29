"""Checkout x opt-in verification-session bootstrap.

Ports node-commerce ``tests/checkout_verification_bootstrap.test.ts``. An identity-gated Checkout
lets a buyer ask for a verify_url without first building a payment credential: the discovery 402
advertises ``X-Verification-Session: create``, and a request carrying it (and no identity or payment
credential) runs the gate, whose missing-identity path mints a session and answers 403. Crawlers
replaying a valid example body never send the header, so they keep getting a plain 402.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agentscore_commerce import VERIFICATION_SESSION_HEADER
from agentscore_commerce.checkout import Checkout, CheckoutGateConfig, PricingResult
from agentscore_commerce.payment.rail_spec import StripeRailSpec, X402BaseRailSpec

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")

SESSION = {
    "session_id": "sess_boot",
    "poll_secret": "poll_boot",
    "verify_url": "https://www.agentscore.com/verify?session=sess_boot",
    "poll_url": "https://api.agentscore.com/v1/sessions/sess_boot",
}


def _request(headers: dict[str, str] | None = None) -> Any:
    """A Starlette request for handle_fastapi: the gate reads identity off the native request."""
    from starlette.requests import Request

    raw_headers = [(b"content-type", b"application/json")]
    raw_headers += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
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
        "headers": raw_headers,
        "scheme": "https",
        "server": ("wine.example", 443),
        "client": ("127.0.0.1", 12345),
        "app": None,
    }
    return Request(scope, receive=_receive)


async def _handle(checkout: Checkout, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    resp = await checkout.handle_fastapi(_request(headers))
    return resp.status_code, json.loads(resp.body)


def _gated() -> Checkout:
    return Checkout(
        rails={"stripe": StripeRailSpec(profile_id="profile_x")},
        url="https://wine.example/purchase",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=50),
        gate=CheckoutGateConfig(api_key="as_test_key", require_kyc=True, min_age=21),
    )


@pytest.fixture
def sdk() -> Any:
    with (
        patch("agentscore.AgentScore.acreate_session", new=AsyncMock(return_value=SESSION)) as create,
        patch("agentscore.AgentScore.aassess", new=AsyncMock(return_value={"decision": "allow"})) as assess,
    ):
        yield {"create": create, "assess": assess}


async def test_advertises_bootstrap_on_gated_402_without_identity(sdk: Any) -> None:
    status, body = await _handle(_gated())
    assert status == 402
    bootstrap = body["identity_bootstrap"]
    assert bootstrap["header"] == VERIFICATION_SESSION_HEADER
    assert bootstrap["value"] == "create"
    assert "verify_url" in bootstrap["instructions"]
    sdk["create"].assert_not_called()


async def test_header_returns_session_403_without_paying(sdk: Any) -> None:
    status, body = await _handle(_gated(), {VERIFICATION_SESSION_HEADER: "create"})
    assert status == 403
    assert body["verify_url"] == SESSION["verify_url"]
    assert body["session_id"] == "sess_boot"
    assert body["poll_secret"] == "poll_boot"
    sdk["create"].assert_called_once()


async def test_header_name_and_value_are_case_insensitive(sdk: Any) -> None:
    status, _ = await _handle(_gated(), {"x-verification-session": " CREATE "})
    assert status == 403
    sdk["create"].assert_called_once()


async def test_ignored_when_request_already_carries_identity(sdk: Any) -> None:
    status, body = await _handle(_gated(), {VERIFICATION_SESSION_HEADER: "create", "X-Operator-Token": "opc_x"})
    assert status == 402
    assert "identity_bootstrap" not in body
    sdk["create"].assert_not_called()
    sdk["assess"].assert_not_called()


async def test_ignores_other_header_values(sdk: Any) -> None:
    status, _ = await _handle(_gated(), {VERIFICATION_SESSION_HEADER: "yes"})
    assert status == 402
    sdk["create"].assert_not_called()


async def test_gateless_merchant_neither_advertises_nor_honors(sdk: Any) -> None:
    gateless = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient="0xT")},
        url="https://api.example/call",
        compute_pricing=lambda _ctx: PricingResult(amount_usd=0.01),
        x402_server=None,
    )
    status, body = await _handle(gateless)
    assert status == 402
    assert "identity_bootstrap" not in body
    asked_status, _ = await _handle(gateless, {VERIFICATION_SESSION_HEADER: "create"})
    assert asked_status == 402
    sdk["create"].assert_not_called()
