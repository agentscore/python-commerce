"""Address normalization tests — must mirror node-commerce's tests/address.test.ts so EVM
and Solana addresses are normalized identically across both SDK languages."""

from __future__ import annotations

from agentscore_commerce.identity.address import (
    is_solana_address,
    is_valid_address,
    is_valid_evm_address,
    normalize_address,
)


class TestIsValidEvmAddress:
    def test_accepts_canonical_0x_plus_40_hex(self):
        assert is_valid_evm_address("0x690BF056DA820EF2e74f8943B3Fe5ca4ADEe7a3e")
        assert is_valid_evm_address("0x" + "a" * 40)

    def test_rejects_wrong_shapes(self):
        assert not is_valid_evm_address("0x" + "a" * 39)
        assert not is_valid_evm_address("0x" + "a" * 41)
        assert not is_valid_evm_address("a" * 40)
        assert not is_valid_evm_address("0xZZZ" + "a" * 37)
        assert not is_valid_evm_address("")


class TestIsSolanaAddress:
    def test_accepts_real_solana_pubkeys(self):
        assert is_solana_address("G2ajX7CrLGoaL8ncaDYNCQoV9b7XhwGF1RzAyKDEZgNZ")
        assert is_solana_address("11111111111111111111111111111111")
        assert is_solana_address("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

    def test_rejects_evm_addresses(self):
        # 0x... could match the base58 alphabet — explicit guard prevents routing
        # an EVM address into the Solana code path.
        assert not is_solana_address("0x690BF056DA820EF2e74f8943B3Fe5ca4ADEe7a3e")

    def test_rejects_non_base58_and_wrong_lengths(self):
        assert not is_solana_address("0OIl" + "A" * 28)  # contains 0/O/I/l
        assert not is_solana_address("A" * 31)
        assert not is_solana_address("A" * 45)
        assert not is_solana_address("")


class TestIsValidAddress:
    def test_accepts_both(self):
        assert is_valid_address("0x690BF056DA820EF2e74f8943B3Fe5ca4ADEe7a3e")
        assert is_valid_address("G2ajX7CrLGoaL8ncaDYNCQoV9b7XhwGF1RzAyKDEZgNZ")

    def test_rejects_garbage(self):
        assert not is_valid_address("not-an-address")
        assert not is_valid_address("")


class TestNormalizeAddress:
    def test_lowercases_evm(self):
        assert (
            normalize_address("0x690BF056DA820EF2e74f8943B3Fe5ca4ADEe7a3e")
            == "0x690bf056da820ef2e74f8943b3fe5ca4adee7a3e"
        )

    def test_preserves_solana_case(self):
        sol = "G2ajX7CrLGoaL8ncaDYNCQoV9b7XhwGF1RzAyKDEZgNZ"
        assert normalize_address(sol) == sol
        # Critical: lowering would corrupt the on-chain identity.
        assert normalize_address(sol) != sol.lower()

    def test_falls_through_to_lowercase_for_unrecognized(self):
        # Garbage still returns SOMETHING so callers don't need an is-valid guard
        # before normalizing — DB writes are guarded separately.
        assert normalize_address("NotAnAddress") == "notanaddress"
