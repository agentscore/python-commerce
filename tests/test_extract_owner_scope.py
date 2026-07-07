"""Tests for ``extract_owner_scope`` — canonical owner identity from headers."""

from agentscore_commerce.identity.tokens import (
    OwnerScope,
    extract_owner_scope,
    hash_operator_token,
)

# A real EIP-55 checksummed EVM address + its lowercase form. The stored ``orders.wallet_address``
# column persists the lowercased signer, so extract_owner_scope MUST lowercase the inbound
# X-Wallet-Address — otherwise a checksummed header misses its own order rows (404).
_CHECKSUMMED = "0xeb2Ca790F72787c7e61bC6c861353a1e4ACDFCa5"
_LOWERCASED = _CHECKSUMMED.lower()


def test_normalizes_evm_wallet_address() -> None:
    scope = extract_owner_scope({"x-wallet-address": _CHECKSUMMED})
    assert scope.wallet_address == _LOWERCASED
    assert scope.operator_token_hash is None


def test_checksummed_wallet_resolves_same_scope_as_lowercase() -> None:
    # The whole point of the fix: both casings collapse to one canonical column value, so a
    # checksummed-EVM read hits the same order rows the lowercased signer was persisted under.
    checksummed = extract_owner_scope({"x-wallet-address": _CHECKSUMMED})
    lower = extract_owner_scope({"x-wallet-address": _LOWERCASED})
    assert checksummed.wallet_address == lower.wallet_address == _LOWERCASED


def test_preserves_solana_address_verbatim() -> None:
    # Solana addresses are base58 and case-sensitive — normalization MUST NOT lowercase them.
    sol = "DQyrAcCrDXQ7iiRTHtPhHkjFmh1mVGwXqUL9F4FUe9YN"
    scope = extract_owner_scope({"x-wallet-address": sol})
    assert scope.wallet_address == sol


def test_hashes_operator_token_never_returns_plaintext() -> None:
    scope = extract_owner_scope({"x-operator-token": "opc_secret123"})
    assert scope.wallet_address is None
    assert scope.operator_token_hash == hash_operator_token("opc_secret123")
    assert "opc_" not in (scope.operator_token_hash or "")


def test_both_headers_present() -> None:
    scope = extract_owner_scope(
        {
            "x-wallet-address": _CHECKSUMMED,
            "x-operator-token": "opc_a",
        }
    )
    assert scope.wallet_address == _LOWERCASED
    assert scope.operator_token_hash == hash_operator_token("opc_a")


def test_empty_when_no_headers() -> None:
    scope = extract_owner_scope({})
    assert scope == OwnerScope()


def test_unwraps_request_with_headers_attr() -> None:
    class _Req:
        headers = {"x-wallet-address": _CHECKSUMMED}

    assert extract_owner_scope(_Req()).wallet_address == _LOWERCASED


def test_accepts_titlecase_headers() -> None:
    """Some frameworks (e.g. requests) preserve title-case header names."""
    scope = extract_owner_scope({"X-Wallet-Address": _CHECKSUMMED})
    assert scope.wallet_address == _LOWERCASED
