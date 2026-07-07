"""Operator-token hashing + owner-scope extraction.

Plaintext operator tokens (``opc_...``) never persist on disk. Merchants hash
them before storing in DB columns and before comparing against persisted hashes.
This module exposes the canonical hash so every consumer agrees on the shape,
plus :func:`extract_owner_scope` for owner-scoped resource lookups.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from agentscore_commerce.identity.address import normalize_address
from agentscore_commerce.payment.payment_header import _read_header, _unwrap_headers


def hash_operator_token(plaintext: str) -> str:
    """sha256 hex digest of a plaintext operator token.

    Use at every persistence boundary (INSERT) AND every comparison boundary
    (SELECT WHERE operator_token_id = ...) so plaintext tokens never land in
    durable storage.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OwnerScope:
    """Canonical owner identity for a caller-scoped resource lookup."""

    wallet_address: str | None = None
    operator_token_hash: str | None = None


def extract_owner_scope(request_or_headers: Any) -> OwnerScope:
    """Pull the canonical owner identity from request headers.

    Reads ``X-Wallet-Address`` and ``X-Operator-Token``; returns the
    network-normalized wallet address and the sha256 hash of the token. Either
    or both may be ``None``.

    The wallet address is normalized via :func:`normalize_address` (EVM
    lowercased, Solana base58 preserved) so it matches the stored
    ``orders.wallet_address`` column, which persists the lowercased signer.
    Without normalization, a checksummed (EIP-55) ``X-Wallet-Address`` would
    miss its own order rows (404).

    Use at owner-scoped resource queries (``GET /orders/:id``, ...) so
    persistence + lookup agree on the hashed column shape and plaintext tokens
    never leave the request.
    """
    headers = _unwrap_headers(request_or_headers)
    wallet_address = _read_header(headers, "x-wallet-address")
    operator_token = _read_header(headers, "x-operator-token")
    return OwnerScope(
        wallet_address=normalize_address(wallet_address) if wallet_address else None,
        operator_token_hash=hash_operator_token(operator_token) if operator_token else None,
    )
