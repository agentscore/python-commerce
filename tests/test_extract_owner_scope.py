"""Tests for ``extract_owner_scope`` — canonical owner identity from headers."""

from agentscore_commerce.identity.tokens import (
    OwnerScope,
    extract_owner_scope,
    hash_operator_token,
)


def test_returns_wallet_address_verbatim() -> None:
    scope = extract_owner_scope({"x-wallet-address": "0xABCDEF"})
    assert scope.wallet_address == "0xABCDEF"
    assert scope.operator_token_hash is None


def test_hashes_operator_token_never_returns_plaintext() -> None:
    scope = extract_owner_scope({"x-operator-token": "opc_secret123"})
    assert scope.wallet_address is None
    assert scope.operator_token_hash == hash_operator_token("opc_secret123")
    assert "opc_" not in (scope.operator_token_hash or "")


def test_both_headers_present() -> None:
    scope = extract_owner_scope(
        {
            "x-wallet-address": "0xeb2Ca790F72787c7e61bC6c861353a1e4ACDFCa5",
            "x-operator-token": "opc_a",
        }
    )
    assert scope.wallet_address == "0xeb2Ca790F72787c7e61bC6c861353a1e4ACDFCa5"
    assert scope.operator_token_hash == hash_operator_token("opc_a")


def test_empty_when_no_headers() -> None:
    scope = extract_owner_scope({})
    assert scope == OwnerScope()


def test_unwraps_request_with_headers_attr() -> None:
    class _Req:
        headers = {"x-wallet-address": "0xfeed"}

    assert extract_owner_scope(_Req()).wallet_address == "0xfeed"


def test_accepts_titlecase_headers() -> None:
    """Some frameworks (e.g. requests) preserve title-case header names."""
    scope = extract_owner_scope({"X-Wallet-Address": "0xfeed"})
    assert scope.wallet_address == "0xfeed"
