"""Network-aware address normalization.

EVM addresses (0x + 40 hex chars) are case-insensitive in the protocol — we lowercase them
so DB lookups against `address_lower`-style columns work. Solana addresses are base58 and
case-sensitive — we MUST preserve the input verbatim, never lowercase.

Must produce the same normalization the AgentScore API applies. Drift here
silently breaks captured-wallet resolution and signer-match.
"""

from __future__ import annotations

import re

_SOLANA_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_valid_evm_address(address: str) -> bool:
    return bool(_EVM_RE.match(address))


def is_solana_address(address: str) -> bool:
    return bool(_SOLANA_BASE58_RE.match(address)) and not address.startswith("0x")


def is_valid_address(address: str) -> bool:
    return is_valid_evm_address(address) or is_solana_address(address)


def normalize_address(address: str) -> str:
    """Lowercase EVM addresses, preserve Solana base58 verbatim."""
    if is_solana_address(address):
        return address
    return address.lower()
