"""Tests for `agentscore_commerce._mppx_receipt` covering all three pympp shapes."""

import pytest

from agentscore_commerce._mppx_receipt import (
    derive_mppx_receipt_method,
    extract_mppx_receipt_header_from_raw,
    extract_mppx_receipt_method,
)


class FakeRawWithReceiptHeader:
    """Shape 1: direct `receipt_header` attribute (pympp current)."""

    receipt_header = "fake-receipt-base64"


class FakeReceiptToPayment:
    def to_payment_receipt(self) -> str:
        return "header-from-receipt-method"


class FakeRawWithToPaymentReceipt:
    """Shape 2: raw is itself a Receipt with `to_payment_receipt()`."""

    def to_payment_receipt(self) -> str:
        return "header-from-raw"


class FakeRawWithInnerReceipt:
    """Shape 2b: raw has a `receipt` attribute carrying a Receipt."""

    def __init__(self) -> None:
        self.receipt = FakeReceiptToPayment()


class _FakeResponseHeaders:
    def __init__(self, val: str) -> None:
        self._val = val

    def get(self, name: str) -> str | None:
        return self._val if name == "Payment-Receipt" else None


class _FakeResponseWithReceipt:
    def __init__(self, val: str) -> None:
        self.headers = _FakeResponseHeaders(val)


class FakeRawWithWithReceipt:
    """Shape 3: node-style `with_receipt(response) -> response`."""

    def with_receipt(self, _resp: object) -> _FakeResponseWithReceipt:
        return _FakeResponseWithReceipt("header-from-with-receipt")


class FakeRawWithReceiptThrowing:
    def with_receipt(self, _resp: object) -> None:
        raise RuntimeError("isMissingReceiptResponseError-style sentinel")


def test_extract_returns_none_for_unsupported_shapes() -> None:
    assert extract_mppx_receipt_header_from_raw(None) is None
    assert extract_mppx_receipt_header_from_raw("string") is None
    assert extract_mppx_receipt_header_from_raw({}) is None


def test_extract_shape_1_receipt_header_attribute() -> None:
    assert extract_mppx_receipt_header_from_raw(FakeRawWithReceiptHeader()) == "fake-receipt-base64"


def test_extract_shape_2_to_payment_receipt_direct() -> None:
    assert extract_mppx_receipt_header_from_raw(FakeRawWithToPaymentReceipt()) == "header-from-raw"


def test_extract_shape_2_to_payment_receipt_via_inner_receipt() -> None:
    assert extract_mppx_receipt_header_from_raw(FakeRawWithInnerReceipt()) == "header-from-receipt-method"


def test_extract_shape_2_tuple_credential_receipt() -> None:
    raw = ("credential", FakeReceiptToPayment())
    assert extract_mppx_receipt_header_from_raw(raw) == "header-from-receipt-method"


def test_extract_shape_2_dict_with_receipt_key() -> None:
    raw = {"receipt": FakeReceiptToPayment()}
    assert extract_mppx_receipt_header_from_raw(raw) == "header-from-receipt-method"


def test_extract_shape_3_with_receipt_wrapper() -> None:
    assert extract_mppx_receipt_header_from_raw(FakeRawWithWithReceipt()) == "header-from-with-receipt"


def test_extract_shape_3_with_receipt_throws() -> None:
    assert extract_mppx_receipt_header_from_raw(FakeRawWithReceiptThrowing()) is None


def test_extract_returns_none_when_to_payment_receipt_throws() -> None:
    class Broken:
        def to_payment_receipt(self) -> str:
            raise RuntimeError("broken")

    assert extract_mppx_receipt_header_from_raw(Broken()) is None


def test_extract_method_returns_none_for_malformed_header() -> None:
    # `mpp.Receipt.from_payment_receipt` will raise; helper returns None.
    assert extract_mppx_receipt_method("not-a-valid-receipt-base64") is None


@pytest.mark.asyncio
async def test_derive_prefers_direct_receipt_method() -> None:
    class FakeRaw:
        def __init__(self) -> None:
            self.receipt = type("R", (), {"method": "tempo"})()

    assert derive_mppx_receipt_method(FakeRaw()) == "tempo"


@pytest.mark.asyncio
async def test_derive_returns_none_when_no_path_resolves() -> None:
    assert derive_mppx_receipt_method(None) is None
    assert derive_mppx_receipt_method({}) is None


@pytest.mark.asyncio
async def test_derive_falls_back_to_header_path() -> None:
    # Raw with header but no direct receipt.method → falls through to
    # extract_mppx_receipt_method, which returns None (no real receipt body).
    result = derive_mppx_receipt_method(FakeRawWithReceiptHeader())
    assert result is None
