"""Tests for the internal helpers ``_headers``, ``_mppx_receipt``, ``_redis``."""

import pytest

from agentscore_commerce._headers import normalize_headers_to_lowercase
from agentscore_commerce._mppx_receipt import (
    derive_mppx_receipt_method,
    extract_mppx_receipt_header_from_raw,
)
from agentscore_commerce._redis import memoized_redis


def test_normalize_headers_lowercases_keys() -> None:
    assert normalize_headers_to_lowercase({"Content-Type": "json", "X-Foo": "bar"}) == {
        "content-type": "json",
        "x-foo": "bar",
    }


def test_normalize_headers_idempotent() -> None:
    once = normalize_headers_to_lowercase({"Content-Type": "json"})
    twice = normalize_headers_to_lowercase(once)
    assert once == twice


def test_extract_mppx_receipt_header_from_attribute() -> None:
    class Raw:
        receipt_header = "deadbeef"

    assert extract_mppx_receipt_header_from_raw(Raw()) == "deadbeef"


def test_extract_mppx_receipt_header_from_to_payment_receipt() -> None:
    class Receipt:
        def to_payment_receipt(self) -> str:
            return "header-value"

    assert extract_mppx_receipt_header_from_raw(Receipt()) == "header-value"


def test_extract_mppx_receipt_header_from_dict_with_receipt() -> None:
    class Receipt:
        def to_payment_receipt(self) -> str:
            return "from-dict"

    assert extract_mppx_receipt_header_from_raw({"receipt": Receipt()}) == "from-dict"


def test_extract_mppx_receipt_header_from_tuple() -> None:
    class Receipt:
        def to_payment_receipt(self) -> str:
            return "from-tuple"

    assert extract_mppx_receipt_header_from_raw(("credential", Receipt())) == "from-tuple"


def test_extract_mppx_receipt_header_none_for_missing_shapes() -> None:
    assert extract_mppx_receipt_header_from_raw(None) is None
    assert extract_mppx_receipt_header_from_raw("string") is None
    assert extract_mppx_receipt_header_from_raw({}) is None


def test_derive_mppx_receipt_method_prefers_direct_attribute() -> None:
    class Receipt:
        method = "tempo"

    class Raw:
        receipt = Receipt()

    assert derive_mppx_receipt_method(Raw()) == "tempo"


@pytest.mark.asyncio
async def test_memoized_redis_no_url_returns_none() -> None:
    getter = memoized_redis(url=None, label="test")
    assert await getter() is None
    # Second call returns the same memoized None
    assert await getter() is None
