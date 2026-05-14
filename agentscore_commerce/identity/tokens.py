"""Operator-token hashing.

Plaintext operator tokens (``opc_...``) never persist on disk. Merchants hash
them before storing in DB columns and before comparing against persisted hashes.
This helper exposes the canonical hash so every consumer agrees on the shape.
"""

from __future__ import annotations

import hashlib


def hash_operator_token(plaintext: str) -> str:
    """sha256 hex digest of a plaintext operator token.

    Use at every persistence boundary (INSERT) AND every comparison boundary
    (SELECT WHERE operator_token_id = ...) so plaintext tokens never land in
    durable storage.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
