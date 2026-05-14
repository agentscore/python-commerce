"""Tests for build_payment_headers."""

import base64
import json

from agentscore_commerce.payment import (
    PaymentHeadersRail,
    X402AcceptsBlock,
    build_payment_headers,
)


def _call(rails, x402=None):
    return build_payment_headers(rails=rails, order_id="ord_1", realm="agents.example", x402=x402)


def test_emits_single_directive_when_one_rail():
    result = _call([PaymentHeadersRail(rail="tempo-mainnet", amount_usd=10, recipient="0xrecipient")])
    assert "Payment " in result["www_authenticate"]
    assert 'id="ord_1-tempo-mainnet"' in result["www_authenticate"]
    assert 'realm="agents.example"' in result["www_authenticate"]
    assert "payment_required" not in result


def test_joins_multiple_directives_per_rfc_7235():
    result = _call(
        [
            PaymentHeadersRail(rail="tempo-mainnet", amount_usd=10, recipient="0xtempo"),
            PaymentHeadersRail(rail="x402-base-mainnet", amount_usd=10, recipient="0xbase"),
        ],
    )
    directives = [s for s in result["www_authenticate"].split(", ") if s.startswith("Payment ")]
    assert len(directives) == 2


def test_unique_challenge_ids_per_rail():
    result = _call(
        [
            PaymentHeadersRail(rail="tempo-mainnet", amount_usd=1, recipient="0xa"),
            PaymentHeadersRail(rail="mpp-solana-mainnet", amount_usd=1, recipient="0xb"),
        ],
    )
    assert 'id="ord_1-tempo-mainnet"' in result["www_authenticate"]
    assert 'id="ord_1-mpp-solana-mainnet"' in result["www_authenticate"]


def test_emits_payment_required_header_when_x402_provided():
    result = _call(
        [PaymentHeadersRail(rail="x402-base-mainnet", amount_usd=1, recipient="0xa")],
        x402=X402AcceptsBlock(accepts=[{"scheme": "exact", "network": "eip155:8453"}], version=1),
    )
    assert "payment_required" in result
    decoded = json.loads(base64.b64decode(result["payment_required"]).decode())
    assert decoded["x402Version"] == 1
    assert decoded["accepts"] == [{"scheme": "exact", "network": "eip155:8453"}]


def test_passes_through_intent_and_expires():
    expires = "2099-12-31T23:59:59Z"
    result = _call(
        [
            PaymentHeadersRail(
                rail="tempo-mainnet",
                amount_usd=1,
                recipient="0xa",
                intent="session",
                expires=expires,
            ),
        ],
    )
    assert 'intent="session"' in result["www_authenticate"]
    assert f'expires="{expires}"' in result["www_authenticate"]
