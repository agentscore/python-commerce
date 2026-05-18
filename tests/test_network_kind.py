"""Tests for ``agentscore_commerce.payment.network_kind``."""

from agentscore_commerce.payment.network_kind import is_evm_network, is_solana_network


def test_is_evm_network_string() -> None:
    assert is_evm_network("eip155:8453") is True
    assert is_evm_network("eip155:84532") is True
    assert is_evm_network("solana:5eykt") is False
    assert is_evm_network("") is False


def test_is_solana_network_string() -> None:
    assert is_solana_network("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp") is True
    assert is_solana_network("eip155:8453") is False
    # bare "solana" (no `:`) is mppx-internal, not CAIP-2 — should be False
    assert is_solana_network("solana") is False


def test_accepts_dict_with_network_field() -> None:
    assert is_evm_network({"network": "eip155:8453"}) is True
    assert is_solana_network({"network": "solana:abc"}) is True


def test_accepts_object_with_network_attribute() -> None:
    class Spec:
        network = "eip155:84532"

    assert is_evm_network(Spec()) is True
    assert is_solana_network(Spec()) is False


def test_handles_none_and_unknown_shapes() -> None:
    assert is_evm_network(None) is False
    assert is_solana_network(None) is False
    assert is_evm_network({}) is False
