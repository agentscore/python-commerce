"""Tests for the rich extract_payment_signer (returns {address, network})."""

import base64
import json

import pytest

from agentscore_commerce.payment.signer import (
    PaymentSigner,
    extract_payment_signer,
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

    def test_returns_none_when_decoded_json_is_not_an_object(self):
        """x402 payload that decodes to a JSON array/scalar instead of an object."""
        list_header = base64.b64encode(json.dumps([1, 2, 3]).encode()).decode()
        assert extract_payment_signer(list_header) is None

    def test_returns_none_when_payload_field_is_not_a_dict(self):
        """x402 with `payload: null` or `payload: "string"` is malformed; no signer recoverable."""
        null_header = _encode_x402({"payload": None})
        assert extract_payment_signer(null_header) is None
        string_header = _encode_x402({"payload": "oops"})
        assert extract_payment_signer(string_header) is None


# ── MPP `Authorization: Payment <base64>` path ────────────────────────────────
#
# The locked fixtures below are shared with the Node sibling at
# `node-commerce/tests/payment/signer.test.ts`. Both files reference identical
# Authorization header values + expected PaymentSigner outputs. A drift in
# either language (DID parsing, base64 handling, scheme prefix) fails that
# language's test against the locked value.

# ASCII-safe Authorization header values (literal "Payment " prefix + base64 token).
_MPP_DID_EIP155_TOP_LEVEL = (
    "Payment eyJzb3VyY2UiOiAiZGlkOnBraDplaXAxNTU6NDIxNzoweEFCQ0RlZjEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQifQ=="
)
_MPP_DID_SOLANA_CHALLENGE = (
    "Payment "
    "eyJjaGFsbGVuZ2UiOiB7InNvdXJjZSI6ICJkaWQ6cGtoOnNvbGFuYTo1ZXlrdDRVc0Z2OFA4TkpkVFJFcFkxdnpxS3FaS3ZkcFVrZkZw"
    "OjduUUVneHFFVzFiRHFhVDNrWldhOEtxVWs0V2ZoNFZiY3cifX0="
)
_MPP_NO_SOURCE = "Payment eyJmb28iOiAiYmFyIn0="  # {"foo": "bar"} — no source field anywhere
_MPP_NON_DICT_JSON = "Payment WzEsIDIsIDNd"  # [1, 2, 3] — JSON list, not an object
_MPP_NON_DID_SOURCE = "Payment eyJzb3VyY2UiOiAiaHR0cHM6Ly9leGFtcGxlLmNvbSJ9"  # {"source": "https://example.com"}
# {"source": "did:pkh:tezos:NetXdQprcVkpaWU:tz1abc..."} — valid did:pkh shape but unknown family
_MPP_UNKNOWN_FAMILY = "Payment eyJzb3VyY2UiOiAiZGlkOnBraDp0ZXpvczpOZXRYZFFwcmNWa3BhV1U6dHoxYWJjZGVmZ2hpamtsbW5vcCJ9"
# {"source": "did:pkh:eip155:4217:not-an-evm-address"} — valid did:pkh but malformed address
_MPP_MALFORMED_ADDR = "Payment eyJzb3VyY2UiOiAiZGlkOnBraDplaXAxNTU6NDIxNzpub3QtYW4tZXZtLWFkZHJlc3MifQ=="

_MPP_FIXTURES: list[tuple[str, str, PaymentSigner | None]] = [
    (
        "did_pkh_eip155_top_level_source",
        _MPP_DID_EIP155_TOP_LEVEL,
        PaymentSigner(address="0xabcdef1234567890123456789012345678901234", network="evm"),
    ),
    (
        "did_pkh_solana_challenge_source",
        _MPP_DID_SOLANA_CHALLENGE,
        PaymentSigner(address="7nQEgxqEW1bDqaT3kZWa8KqUk4Wfh4Vbcw", network="solana"),
    ),
    ("credential_without_source", _MPP_NO_SOURCE, None),
    ("credential_not_json_object", _MPP_NON_DICT_JSON, None),
    ("source_not_did_pkh", _MPP_NON_DID_SOURCE, None),
    ("did_pkh_unknown_family", _MPP_UNKNOWN_FAMILY, None),
    ("did_pkh_malformed_address", _MPP_MALFORMED_ADDR, None),
    ("bearer_not_payment_scheme", "Bearer abc.def.ghi", None),
    ("payment_with_empty_token", "Payment ", None),
    ("payment_with_non_base64_token", "Payment !!!not-base64!!!", None),
    ("empty_string", "", None),
]


class TestExtractPaymentSignerMppPath:
    """MPP ``Authorization: Payment <base64>`` extraction; locked cross-language fixtures."""

    @pytest.mark.parametrize(
        ("label", "auth_header", "expected"),
        _MPP_FIXTURES,
        ids=[label for label, _, _ in _MPP_FIXTURES],
    )
    def test_locked_cross_language_fixture(
        self,
        label: str,
        auth_header: str,
        expected: PaymentSigner | None,
    ) -> None:
        del label  # consumed by parametrize ids
        assert extract_payment_signer(authorization_header=auth_header) == expected

    def test_case_insensitive_payment_scheme(self) -> None:
        """``payment``, ``PAYMENT``, and ``Payment`` are equivalent per RFC 7235."""
        result_upper = extract_payment_signer(
            authorization_header=_MPP_DID_EIP155_TOP_LEVEL.replace("Payment ", "PAYMENT "),
        )
        result_lower = extract_payment_signer(
            authorization_header=_MPP_DID_EIP155_TOP_LEVEL.replace("Payment ", "payment "),
        )
        expected = PaymentSigner(address="0xabcdef1234567890123456789012345678901234", network="evm")
        assert result_upper == expected
        assert result_lower == expected

    def test_x402_header_takes_precedence_over_mpp(self) -> None:
        """When both headers are supplied, the x402 path is tried first."""
        x402_evm = base64.b64encode(
            json.dumps({"payload": {"authorization": {"from": EVM_MIXED}}}).encode(),
        ).decode()
        result = extract_payment_signer(x402_evm, authorization_header=_MPP_DID_SOLANA_CHALLENGE)
        assert result == PaymentSigner(address=EVM_LOWER, network="evm")

    def test_mpp_only_when_x402_absent(self) -> None:
        """No x402 supplied → MPP path runs."""
        result = extract_payment_signer(None, authorization_header=_MPP_DID_EIP155_TOP_LEVEL)
        assert result == PaymentSigner(address="0xabcdef1234567890123456789012345678901234", network="evm")

    def test_no_headers_returns_none(self) -> None:
        assert extract_payment_signer() is None
        assert extract_payment_signer(None) is None
        assert extract_payment_signer(None, authorization_header=None) is None

    def test_does_not_require_mpp_parsing_module(self) -> None:
        """Regression: the helper must NOT import ``mpp._parsing`` (private upstream).

        This is a smoke test — we don't try to mock the import absence, just confirm
        the helper works without pympp's private parser being involved. The function
        body relies only on stdlib (base64 + json) for the MPP path.
        """
        # Re-running a fixture confirms no import-time side effects on the MPP path.
        result = extract_payment_signer(authorization_header=_MPP_DID_EIP155_TOP_LEVEL)
        assert result is not None
        assert result.network == "evm"


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
