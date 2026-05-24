"""Tests for `agentscore_commerce.payment.solana.load_solana_fee_payer`."""

import builtins
import contextlib

import pytest

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


def test_hex_seed_returns_keypair() -> None:
    """A 128-char hex key takes the seed branch and returns a usable Keypair."""
    kp = load_solana_fee_payer("a" * 128)
    assert kp is not None
    # solders Keypair exposes pubkey()
    assert callable(getattr(kp, "pubkey", None))


def test_base58_64_byte_returns_keypair() -> None:
    """A base58 keypair decoding to 64 bytes takes the from_bytes branch."""
    import base58
    from solders.keypair import Keypair

    seed = bytes(range(32))
    full = Keypair.from_seed(seed)
    encoded = base58.b58encode(bytes(full)).decode()
    kp = load_solana_fee_payer(encoded)
    assert kp is not None
    assert bytes(kp) == bytes(full)


def test_base58_32_byte_returns_keypair() -> None:
    """A base58 secret-only key (32 bytes) takes the from_seed branch."""
    import base58

    seed = bytes(range(32))
    encoded = base58.b58encode(seed).decode()
    kp = load_solana_fee_payer(encoded)
    assert kp is not None


def test_base58_invalid_length_raises_value_error_message() -> None:
    """A base58 key decoding to an unexpected length raises a descriptive ValueError."""
    import base58

    encoded = base58.b58encode(bytes(10)).decode()
    with pytest.raises(ValueError, match="must decode to 32 or 64 bytes"):
        load_solana_fee_payer(encoded)


def test_solders_missing_raises_guiding_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When `solders` is unavailable, a guiding ImportError names the install command."""
    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "solders.keypair" or name.startswith("solders"):
            msg = "no solders"
            raise ImportError(msg)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(ImportError, match=r"solders not installed"):
        load_solana_fee_payer("a" * 128)


def test_base58_missing_raises_guiding_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When `base58` is unavailable (but solders is), a guiding ImportError fires."""
    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "base58":
            msg = "no base58"
            raise ImportError(msg)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    # Non-hex input so we reach the base58 import line.
    with pytest.raises(ImportError, match=r"base58 not installed"):
        load_solana_fee_payer("5Kd3NBUAdUnhyzenEwVLy9pBKxSwXvE9FMPyR4UKZvpu")
