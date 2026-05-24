"""Tests for ``agentscore_commerce.payment.payment_header``."""

from collections.abc import Mapping
from typing import Any

from agentscore_commerce.payment.payment_header import (
    _read_header,
    has_mppx_header,
    has_payment_header,
    has_x402_header,
)


def test_detects_payment_signature_header() -> None:
    assert has_payment_header({"Payment-Signature": "deadbeef"}) is True
    assert has_payment_header({"payment-signature": "deadbeef"}) is True


def test_detects_x_payment_header() -> None:
    assert has_payment_header({"X-Payment": "<base64>"}) is True
    assert has_payment_header({"x-payment": "<base64>"}) is True


def test_detects_authorization_payment_scheme() -> None:
    assert has_payment_header({"Authorization": "Payment <jwt>"}) is True
    assert has_payment_header({"authorization": "Payment <jwt>"}) is True


def test_rejects_bare_authorization_bearer() -> None:
    assert has_payment_header({"Authorization": "Bearer abc"}) is False
    assert has_payment_header({"authorization": "Basic xyz"}) is False


def test_returns_false_when_no_payment_credential() -> None:
    assert has_payment_header({}) is False
    assert has_payment_header({"User-Agent": "test"}) is False


def test_accepts_request_like_with_headers_attr() -> None:
    class Req:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    assert has_payment_header(Req({"x-payment": "abc"})) is True
    assert has_payment_header(Req({})) is False


def test_reads_headers_with_get_returning_list_or_tuple() -> None:
    """Headers `.get` returns list values for repeated headers — first hop wins."""

    class MultiHeaders:
        def get(self, name: str) -> list[str] | None:
            if name in ("payment-signature", "Payment-Signature"):
                return ["sig-first", "sig-second"]
            return None

    assert has_payment_header(MultiHeaders()) is True


def test_reads_headers_with_get_returning_none() -> None:
    """Headers `.get` falls back to case variants when first lookup returns None."""

    class TitleCaseHeaders:
        def get(self, name: str) -> str | None:
            # Only respond to Title-Case
            if name == "X-Payment":
                return "abc"
            return None

    assert has_payment_header(TitleCaseHeaders()) is True


def test_reads_mapping_with_list_value() -> None:
    headers = {"X-Payment": ["one", "two"]}
    assert has_payment_header(headers) is True


def test_reads_mapping_case_variants() -> None:
    # Only Title-Case key present in mapping
    assert has_payment_header({"X-Payment": "value"}) is True
    # Only lowercase
    assert has_payment_header({"x-payment": "value"}) is True


def test_read_header_none_headers_returns_none() -> None:
    """`_read_header(None, ...)` short-circuits to None (line 33)."""
    assert _read_header(None, "x-payment") is None


class _NonGetMapping(Mapping):
    """A Mapping whose `.get` is a non-callable attribute, forcing the Mapping
    iteration fallback path (lines 46-53) instead of the `.get` getter path.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.get = None  # type: ignore[assignment]  # shadow Mapping.get with a non-callable

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Any:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def test_mapping_fallback_string_value() -> None:
    """A Mapping without a callable `.get` falls through to the iteration branch
    and returns a string header value (lines 46-51)."""
    headers = _NonGetMapping({"x-payment": "creds"})
    assert _read_header(headers, "x-payment") == "creds"


def test_mapping_fallback_list_value() -> None:
    """The Mapping iteration branch unwraps a list value to its first hop (lines 52-53)."""
    headers = _NonGetMapping({"payment-signature": ["first", "second"]})
    assert _read_header(headers, "payment-signature") == "first"


def test_mapping_fallback_non_string_non_list_returns_none() -> None:
    """A present key whose value is neither str nor list yields None overall."""
    headers = _NonGetMapping({"x-payment": 12345})
    assert _read_header(headers, "x-payment") is None


def test_getter_not_callable_and_not_mapping_returns_none() -> None:
    """An object whose `.get` is non-callable and which is not a Mapping yields None."""

    class _Weird:
        get = "not-callable"

    assert _read_header(_Weird(), "x-payment") is None


def test_has_x402_header_variants() -> None:
    assert has_x402_header({"x-payment": "abc"}) is True
    assert has_x402_header({"payment-signature": "sig"}) is True
    assert has_x402_header({"authorization": "Payment jwt"}) is False
    assert has_x402_header({}) is False


def test_has_mppx_header_variants() -> None:
    assert has_mppx_header({"authorization": "Payment jwt"}) is True
    assert has_mppx_header({"authorization": "Bearer x"}) is False
    assert has_mppx_header({"x-payment": "abc"}) is False
