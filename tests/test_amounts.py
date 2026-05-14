"""Tests for ``agentscore_commerce.payment.amounts.usd_to_atomic``.

The fixture corpus below is locked as the cross-language contract with the
Node sibling at ``node-commerce/tests/payment/amounts.test.ts``. Both files
reference identical fixed-notation inputs + decimals + expected atomic values.
A drift in either language (rounding mode, encoding, edge-case handling) fails
that language's test against the locked value.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agentscore_commerce.payment import usd_to_atomic

# Cross-language fixtures: (input_string, decimals, expected_atomic).
# Inputs are fixed-notation strings so Python's Decimal and the Node sibling's
# regex-based parser produce identical results.
_FIXTURES = [
    # Plain whole + simple decimals
    ("0", 6, 0),
    ("1", 6, 1_000_000),
    ("1.0", 6, 1_000_000),
    ("1.00", 6, 1_000_000),
    ("0.5", 6, 500_000),
    ("10.00", 6, 10_000_000),
    ("270.00", 6, 270_000_000),
    # Exact decimal precision
    ("1.234567", 6, 1_234_567),
    # Round-half-up at the boundary (USDC tail of 5)
    ("1.2345675", 6, 1_234_568),
    ("1.2345674", 6, 1_234_567),
    ("1.2345679", 6, 1_234_568),
    # Sub-precision rounding
    ("0.0000005", 6, 1),
    ("0.0000004", 6, 0),
    # Different decimals tail
    ("1.23", 2, 123),
    ("1.5", 0, 2),
    ("1.4", 0, 1),
    ("0.5", 0, 1),
    ("0.4999999999", 0, 0),
    ("0.5000000001", 0, 1),
    # Leading-zero and trailing-dot forms
    (".5", 6, 500_000),
    ("5.", 6, 5_000_000),
    ("001", 6, 1_000_000),
]


@pytest.mark.parametrize(
    ("usd", "decimals", "expected"),
    _FIXTURES,
    ids=[f"{u!r}@{d}" for u, d, _ in _FIXTURES],
)
def test_locked_cross_language_fixture(usd: str, decimals: int, expected: int) -> None:
    """Each fixture input maps to the locked cross-language atomic value."""
    assert usd_to_atomic(usd, decimals=decimals) == expected


def test_accepts_float_input() -> None:
    """Float input is converted via ``str()`` then parsed by Decimal."""
    assert usd_to_atomic(1.23, decimals=6) == 1_230_000


def test_accepts_decimal_input() -> None:
    """``Decimal`` input is passed through (matches the float path's precision)."""
    assert usd_to_atomic(Decimal("1.234567"), decimals=6) == 1_234_567


def test_accepts_int_input() -> None:
    """Plain ``int`` is treated as a whole-USD amount."""
    assert usd_to_atomic(5, decimals=6) == 5_000_000


def test_zero_input_returns_zero() -> None:
    assert usd_to_atomic("0", decimals=6) == 0
    assert usd_to_atomic(0, decimals=6) == 0
    assert usd_to_atomic(0.0, decimals=6) == 0


def test_decimals_zero_returns_whole_dollars() -> None:
    """``decimals=0`` returns the (rounded) whole-USD value."""
    assert usd_to_atomic("123.4", decimals=0) == 123
    assert usd_to_atomic("123.5", decimals=0) == 124


def test_negative_string_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        usd_to_atomic("-1.00", decimals=6)


def test_negative_float_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        usd_to_atomic(-1.0, decimals=6)


def test_nan_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        usd_to_atomic(float("nan"), decimals=6)


def test_positive_infinity_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        usd_to_atomic(float("inf"), decimals=6)


def test_negative_infinity_rejected() -> None:
    # Negative-infinity fails the finite check before the non-negative check; either error is OK.
    with pytest.raises(ValueError):
        usd_to_atomic(float("-inf"), decimals=6)


def test_empty_string_rejected() -> None:
    with pytest.raises(ValueError, match="invalid usd value"):
        usd_to_atomic("", decimals=6)


def test_garbage_string_rejected() -> None:
    with pytest.raises(ValueError, match="invalid usd value"):
        usd_to_atomic("abc", decimals=6)
    with pytest.raises(ValueError, match="invalid usd value"):
        usd_to_atomic("1.2.3", decimals=6)


def test_whitespace_padded_string_accepted() -> None:
    """String input is trimmed so a leading/trailing space matches the Node sibling."""
    assert usd_to_atomic("  1.00  ", decimals=6) == 1_000_000
    assert usd_to_atomic("\t0.50\n", decimals=6) == 500_000


def test_negative_decimals_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative int"):
        usd_to_atomic("1.00", decimals=-1)


def test_non_int_decimals_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative int"):
        usd_to_atomic("1.00", decimals=6.0)  # type: ignore[arg-type]


def test_bool_decimals_rejected() -> None:
    """``bool`` is a subclass of ``int`` in Python; reject explicitly to avoid surprise."""
    with pytest.raises(ValueError, match="non-negative int"):
        usd_to_atomic("1.00", decimals=True)  # type: ignore[arg-type]
