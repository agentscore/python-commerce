"""Tests for ``agentscore_commerce.payment.dispatch.detect_rail_from_headers``.

The fixture corpus below is locked as the cross-language contract with the
Node sibling at ``node-commerce/tests/payment/detect_rail_from_headers.test.ts``.
Both files reference identical header maps + expected results. A drift in either
language (case-handling, empty-value treatment, scheme-prefix matching) fails
that language's test against the locked value.
"""

from __future__ import annotations

import pytest

from agentscore_commerce.payment import detect_rail_from_headers

# Cross-language fixtures: (label, headers_dict, expected_rail).
_FIXTURES: list[tuple[str, dict[str, str], str | None]] = [
    ("empty", {}, None),
    ("payment_signature_only", {"payment-signature": "abc"}, "x402"),
    ("x_payment_only", {"x-payment": "abc"}, "x402"),
    ("authorization_payment", {"authorization": "Payment abc"}, "mpp"),
    ("authorization_bearer", {"authorization": "Bearer xyz"}, None),
    ("authorization_lowercase_scheme", {"authorization": "payment abc"}, "mpp"),
    ("authorization_uppercase_name", {"Authorization": "Payment abc"}, "mpp"),
    ("x_payment_uppercase_name", {"X-Payment": "abc"}, "x402"),
    ("empty_values_dont_count", {"payment-signature": "", "x-payment": ""}, None),
    (
        "mpp_wins_when_both_present",
        {
            "x-payment": "abc",
            "authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19",
        },
        "mpp",
    ),
    ("payment_without_space_is_not_mpp", {"authorization": "PaymentNoSpace"}, None),
    ("payment_with_only_space_is_mpp", {"authorization": "Payment "}, "mpp"),
    ("mixed_case_authorization_name", {"AUTHORIZATION": "Payment abc"}, "mpp"),
    ("authorization_uppercase_scheme", {"authorization": "PAYMENT abc"}, "mpp"),
]


@pytest.mark.parametrize(
    ("label", "headers", "expected"),
    _FIXTURES,
    ids=[label for label, _, _ in _FIXTURES],
)
def test_locked_cross_language_fixture(
    label: str,
    headers: dict[str, str],
    expected: str | None,
) -> None:
    del label  # `label` is consumed by parametrize ids; bind locally so linters don't flag it.
    """Each fixture header set maps to the locked cross-language rail value."""
    assert detect_rail_from_headers(headers) == expected


def test_returns_x402_for_non_string_truthy_value() -> None:
    """Any non-empty header value is treated as present (no validation of contents)."""
    assert detect_rail_from_headers({"x-payment": "0"}) == "x402"


def test_does_not_mutate_input_headers() -> None:
    headers = {
        "X-Payment": "abc",
        "Authorization": "Payment eyJjaGFsbGVuZ2UiOiB7ImlkIjogImNoXzEiLCAicmVhbG0iOiAiYXBpLmV4YW1wbGUifSwgInBheWxvYWQiOiB7InR5cGUiOiAiaGFzaCIsICJoYXNoIjogIjB4YWJjIn19",
    }
    detect_rail_from_headers(headers)
    # Keys preserved verbatim; helper only reads via a lowercase projection.
    assert "X-Payment" in headers
    assert "Authorization" in headers
