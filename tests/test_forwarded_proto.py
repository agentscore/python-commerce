"""X-Forwarded-Proto scheme correction for the x402 resource.url (mirrors node-commerce)."""

import base64
import json
from typing import Any

import pytest

from agentscore_commerce.checkout_compute_first import (
    ComputeFirstCheckout,
    ComputeFirstRails,
    ComputeFirstRequest,
    ComputeFirstWorkContext,
    WorkOutcome,
)
from agentscore_commerce.forwarded_proto import apply_forwarded_proto, read_forwarded_proto
from agentscore_commerce.payment.rail_spec import TempoRailSpec


def test_apply_forwarded_proto_rewrites_scheme() -> None:
    assert apply_forwarded_proto("http://agents.example.com/purchase", "https") == "https://agents.example.com/purchase"


def test_apply_forwarded_proto_takes_first_proxy_hop() -> None:
    assert apply_forwarded_proto("http://x.com/a", "https, http") == "https://x.com/a"


def test_apply_forwarded_proto_passthrough_when_absent() -> None:
    assert apply_forwarded_proto("http://localhost:3003/purchase", None) == "http://localhost:3003/purchase"
    assert apply_forwarded_proto("http://x.com/a", "") == "http://x.com/a"


def test_read_forwarded_proto_both_casings() -> None:
    # Lowercase (fastapi/flask/aiohttp/sanic) and Title-Case (Django) both resolve.
    assert read_forwarded_proto({"x-forwarded-proto": "https"}) == "https"
    assert read_forwarded_proto({"X-Forwarded-Proto": "https"}) == "https"
    assert read_forwarded_proto({}) is None


async def _run_one(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    return WorkOutcome(result_count=1, body={"matches": ["one"], "total": 1})


def _handler() -> ComputeFirstCheckout:
    return ComputeFirstCheckout(
        name="search",
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=ComputeFirstRails(tempo=TempoRailSpec(recipient="0xtempo", testnet=True)),
        x402_server=None,
        run_work=_run_one,
    )


def _resource_url(headers: dict[str, str]) -> str:
    decoded = json.loads(base64.b64decode(headers["PAYMENT-REQUIRED"]))
    return decoded["resource"]["url"]


@pytest.mark.asyncio
async def test_compute_first_402_rewrites_resource_scheme_behind_proxy() -> None:
    req = ComputeFirstRequest(
        method="POST",
        url="http://agents.example.com/purchase",
        headers={"x-forwarded-proto": "https"},
        body={"q": "x"},
    )
    status, _body, headers = await _handler().handle(req)
    assert status == 402
    assert _resource_url(headers) == "https://agents.example.com/purchase"


@pytest.mark.asyncio
async def test_compute_first_402_leaves_http_without_proxy_header() -> None:
    req = ComputeFirstRequest(
        method="POST",
        url="http://localhost:3003/purchase",
        headers={},
        body={"q": "x"},
    )
    status, _body, headers = await _handler().handle(req)
    assert status == 402
    assert _resource_url(headers) == "http://localhost:3003/purchase"
