"""Tests for ``agentscore_commerce.payment.payment_header``."""

from agentscore_commerce.payment.payment_header import has_payment_header


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
