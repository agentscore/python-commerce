"""Tests for the rich extract_payment_signer (returns {address, network})."""

import base64
import json

from agentscore_commerce.payment.signer import (
    PaymentSigner,
    extract_payment_signer,
    extract_payment_signer_address,
    read_x402_payment_header,
)

EVM_LOWER = "0xabcdef0123456789abcdef0123456789abcdef01"
EVM_MIXED = "0xABCDEF0123456789ABCDEF0123456789ABCDEF01"


def _encode_x402(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


class TestExtractPaymentSigner:
    def test_returns_evm_signer_for_eip3009_payload(self):
        header = _encode_x402(
            {"accepted": {"network": "eip155:8453"}, "payload": {"authorization": {"from": EVM_MIXED}}}
        )
        result = extract_payment_signer(header)
        assert result == PaymentSigner(address=EVM_LOWER, network="evm")

    def test_returns_evm_when_payload_has_no_accepted_network(self):
        header = _encode_x402({"payload": {"authorization": {"from": EVM_MIXED}}})
        result = extract_payment_signer(header)
        assert result == PaymentSigner(address=EVM_LOWER, network="evm")

    def test_returns_none_for_solana_payload(self):
        header = _encode_x402({"accepted": {"network": "solana:abc"}, "payload": {"transaction": "xxx"}})
        assert extract_payment_signer(header) is None

    def test_returns_none_for_malformed_header(self):
        assert extract_payment_signer("!!!not-base64!!!") is None

    def test_returns_none_for_empty_or_missing_header(self):
        assert extract_payment_signer(None) is None
        assert extract_payment_signer("") is None

    def test_returns_none_when_payload_missing_authorization(self):
        header = _encode_x402({"accepted": {"network": "eip155:1"}, "payload": {}})
        assert extract_payment_signer(header) is None

    def test_returns_none_when_from_is_not_an_evm_address(self):
        header = _encode_x402(
            {"accepted": {"network": "eip155:1"}, "payload": {"authorization": {"from": "not-an-address"}}}
        )
        assert extract_payment_signer(header) is None


class TestExtractPaymentSignerAddress:
    def test_returns_address_only(self):
        header = _encode_x402(
            {"accepted": {"network": "eip155:8453"}, "payload": {"authorization": {"from": EVM_MIXED}}}
        )
        assert extract_payment_signer_address(header) == EVM_LOWER

    def test_returns_none_when_signer_unrecoverable(self):
        assert extract_payment_signer_address(None) is None
        assert extract_payment_signer_address("") is None
        assert extract_payment_signer_address("!!!") is None


class TestReadX402PaymentHeader:
    def test_prefers_payment_signature(self):
        assert read_x402_payment_header({"payment-signature": "ps_value", "x-payment": "xp_value"}) == "ps_value"

    def test_falls_back_to_x_payment(self):
        assert read_x402_payment_header({"x-payment": "xp_value"}) == "xp_value"

    def test_case_insensitive(self):
        assert read_x402_payment_header({"X-Payment": "xp_value"}) == "xp_value"
        assert read_x402_payment_header({"Payment-Signature": "ps_value"}) == "ps_value"

    def test_returns_none_when_neither_present(self):
        assert read_x402_payment_header({}) is None
        assert read_x402_payment_header({"authorization": "Payment ..."}) is None
