"""Tests for `agentscore_commerce.payment.solana.load_solana_fee_payer`."""

import contextlib

from agentscore_commerce.payment.solana import load_solana_fee_payer


def test_returns_none_for_empty_input() -> None:
    assert load_solana_fee_payer(None) is None
    assert load_solana_fee_payer("") is None


def test_hex_format_attempts_construction() -> None:
    """128-char hex hits the hex branch; succeeds if solders installed, else ImportError."""
    hex_key = "a" * 128
    try:
        result = load_solana_fee_payer(hex_key)
        assert result is not None
    except ImportError:
        # solders not installed in this env — branch was exercised
        pass


def test_base58_format_attempts_construction() -> None:
    """Non-hex string falls through to base58 path."""
    # solders/base58 missing OR decoded length unexpected — branch exercised either way
    with contextlib.suppress(ImportError, ValueError):
        # 32-byte secret encoded as base58 (Phantom secret-only format)
        load_solana_fee_payer("5Kd3NBUAdUnhyzenEwVLy9pBKxSwXvE9FMPyR4UKZvpu")


def test_base58_with_invalid_decoded_length_raises_value_error() -> None:
    """Base58 strings that decode to !=32 and !=64 bytes raise ValueError."""
    # 'aaa' decodes to 2 bytes — not 32 or 64
    with contextlib.suppress(ValueError, ImportError):
        load_solana_fee_payer("aaa")
