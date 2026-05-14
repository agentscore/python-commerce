"""Tests for ``classify_orchestration_error`` — string-match classification of
arbitrary thrown errors during the 402 orchestration.

Locked cross-language fixtures shared with the Node sibling at
``node-commerce/tests/payment/classify_orchestration_error.test.ts``. Both
files reference identical error messages + expected ClassifiedX402Error
codes/statuses. Drift in either language (matcher list, case-insensitivity,
None-on-unknown) fails that language's test against the locked value.
"""

from __future__ import annotations

import pytest

from agentscore_commerce.payment import ClassifiedX402Error, classify_orchestration_error

# Cross-language fixtures: (label, error_message, expected_code_or_None).
# When the helper returns a ClassifiedX402Error, its `code` matches the third
# tuple element; `None` means the helper returns None (caller rethrows).
_FIXTURES: list[tuple[str, str, str | None]] = [
    # payment_proof_invalid family
    ("x402version_lowercase", "Unsupported x402Version 3", "payment_proof_invalid"),
    ("x402version_uppercase", "UNSUPPORTED X402VERSION 3", "payment_proof_invalid"),
    ("invalid_payment", "Invalid payment payload", "payment_proof_invalid"),
    ("unsupported_x402", "Unsupported x402 method", "payment_proof_invalid"),
    # payment_provider_unavailable family
    ("stripe_lowercase", "Stripe API returned 502", "payment_provider_unavailable"),
    ("facilitator_lowercase", "Facilitator unreachable", "payment_provider_unavailable"),
    ("cdp_lowercase", "CDP JWT expired", "payment_provider_unavailable"),
    ("stripe_uppercase", "STRIPE timeout", "payment_provider_unavailable"),
    # Unknown — caller rethrows
    ("database_error", "duplicate key value violates unique constraint", None),
    ("network_error", "ECONNREFUSED", None),
    ("empty_string", "", None),
    ("generic_unknown", "something went wrong", None),
]


@pytest.mark.parametrize(
    ("label", "message", "expected_code"),
    _FIXTURES,
    ids=[label for label, _, _ in _FIXTURES],
)
def test_locked_cross_language_fixture(label, message, expected_code) -> None:
    del label
    result = classify_orchestration_error(message)
    if expected_code is None:
        assert result is None
    else:
        assert result is not None
        assert result.code == expected_code


def test_accepts_exception_instance() -> None:
    """An Exception is stringified via ``str()`` before classification."""
    err = ValueError("Unsupported x402Version 3")
    result = classify_orchestration_error(err)
    assert result is not None
    assert result.code == "payment_proof_invalid"


def test_accepts_baseexception() -> None:
    """``BaseException`` (e.g. KeyboardInterrupt) is also accepted, for completeness."""
    err = BaseException("stripe API error")
    result = classify_orchestration_error(err)
    assert result is not None
    assert result.code == "payment_provider_unavailable"


def test_returns_400_for_payment_proof_invalid() -> None:
    result = classify_orchestration_error("x402Version mismatch")
    assert result is not None
    assert result.status == 400


def test_returns_503_for_payment_provider_unavailable() -> None:
    result = classify_orchestration_error("Stripe error")
    assert result is not None
    assert result.status == 503


def test_classified_carries_next_steps() -> None:
    result = classify_orchestration_error("invalid payment")
    assert result is not None
    assert result.next_steps.get("action") == "regenerate_payment_credential"
    assert "user_message" in result.next_steps


def test_provider_classified_carries_retry_after_seconds() -> None:
    result = classify_orchestration_error("CDP facilitator timeout")
    assert result is not None
    assert result.next_steps.get("retry_after_seconds") == 10


def test_payment_proof_takes_precedence_when_both_keywords_present() -> None:
    """An error message containing both pattern families resolves to the first matched."""
    # "x402Version" is checked first; "stripe" present too but doesn't reach the second branch
    result = classify_orchestration_error("Unsupported x402Version returned by stripe")
    assert result is not None
    assert result.code == "payment_proof_invalid"


def test_returns_classified_x402_error_type() -> None:
    """The return type is the same ``ClassifiedX402Error`` that ``classify_x402_settle_result`` returns."""
    result = classify_orchestration_error("invalid payment")
    assert isinstance(result, ClassifiedX402Error)


def test_returns_none_for_non_string_non_exception_input() -> None:
    """Defensive: non-str / non-Exception input returns None."""
    assert classify_orchestration_error(None) is None  # type: ignore[arg-type]
    assert classify_orchestration_error(42) is None  # type: ignore[arg-type]
