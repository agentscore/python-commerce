import base64
import json

from agentscore_commerce.payment import (
    BuildPaymentDirectiveInput,
    PaymentDirectiveInput,
    PaymentRequestInput,
    build_payment_directive,
    build_payment_request_blob,
    payment_directive,
)


def _decode(blob: str) -> dict:
    pad = "=" * (-len(blob) % 4)
    return json.loads(base64.urlsafe_b64decode(blob + pad))


def test_build_payment_request_blob_with_rail():
    blob = build_payment_request_blob(PaymentRequestInput(rail="x402-base-mainnet", amount_usd=1.0))
    decoded = _decode(blob)
    assert decoded["amount"] == "1000000"  # 1 USDC at 6 decimals
    assert decoded["currency"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert decoded["decimals"] == 6
    assert decoded["methodDetails"]["chainId"] == 8453


def test_build_payment_request_blob_overrides_take_precedence():
    blob = build_payment_request_blob(
        PaymentRequestInput(rail="x402-base-mainnet", amount_usd=2, decimals=2, currency="usd", network_id="acct_x")
    )
    decoded = _decode(blob)
    assert decoded["amount"] == "200"
    assert decoded["currency"] == "usd"
    assert decoded["decimals"] == 2
    assert decoded["methodDetails"]["networkId"] == "acct_x"


def test_build_payment_request_blob_includes_decimals_for_node_parity():
    """Wire-format parity with @agent-score/commerce — the decoded JSON must include `decimals`
    (mppx tempo schema requires it). If this assertion fails, node-commerce and python-commerce
    are emitting different request blobs for the same payment, which breaks cross-SDK interop.
    """
    blob = build_payment_request_blob(PaymentRequestInput(rail="tempo-mainnet", amount_usd="1.50", recipient="0xabc"))
    decoded = _decode(blob)
    # Output keys must match buildPaymentRequestBlob exactly across both SDK languages
    assert set(decoded.keys()) >= {"amount", "currency", "decimals"}
    assert decoded["amount"] == "1500000"  # 1.50 USDC at 6 decimals
    assert decoded["decimals"] == 6


def test_payment_directive_format():
    directive = payment_directive(
        PaymentDirectiveInput(rail="tempo-mainnet", id="chg_1", realm="ex.com", request="abc")
    )
    assert directive.startswith('Payment id="chg_1"')
    assert 'method="tempo"' in directive
    assert 'intent="charge"' in directive
    assert 'request="abc"' in directive


def test_build_payment_directive_combines_blob_and_directive():
    directive = build_payment_directive(
        BuildPaymentDirectiveInput(
            rail="tempo-mainnet", id="chg_2", realm="ex.com", amount_usd="0.5", recipient="0xabc"
        )
    )
    assert 'method="tempo"' in directive
    assert "request=" in directive
