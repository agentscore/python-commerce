"""CAIP-2 prefix discriminators for chain-family identification.

Replaces the ad-hoc ``startswith("eip155:")`` / ``startswith("solana:")``
checks scattered across ``checkout``, ``identity.ucp``, ``payment.dispatch``.
Pure functions; no peer-dep imports.
"""

from __future__ import annotations

from typing import Any


def _read_network(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    # Accept both attribute access (dataclass / pydantic) and dict.
    net = value.get("network") if isinstance(value, dict) else getattr(value, "network", None)
    return net if isinstance(net, str) else ""


def is_evm_network(value: Any) -> bool:
    """True when the network is a CAIP-2 EVM chain (``eip155:<chainId>``)."""
    return _read_network(value).startswith("eip155:")


def is_solana_network(value: Any) -> bool:
    """True when the network is a CAIP-2 Solana chain (``solana:<genesis>``).

    Note: the bare string ``"solana"`` (no ``:``) is the mppx-internal label,
    NOT a CAIP-2 spec — this helper treats it as ``False``.
    """
    return _read_network(value).startswith("solana:")
