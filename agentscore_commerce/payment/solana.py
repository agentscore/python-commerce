"""Solana MPP fee-payer signer loader.

Buyers paying via Solana MPP USDC don't typically carry SOL for transaction
fees, so merchants commonly co-sign the buyer's ``solana/charge`` tx as the
fee payer (~5000 lamports per tx; negligible vs the USDC value moved).

``load_solana_fee_payer(private_key=...)`` accepts a Solana keypair in any of
the three forms agents commonly export it as:

* **base58** (Phantom export format) — 64-byte secret+public, or 32-byte
  secret-only
* **hex** — 128-char string (64 bytes hex: 32-byte secret + 32-byte public)

Returns a ``KeyPairSigner`` from ``solders`` ready to pass to ``mppx``'s
``solana/charge`` rail. Returns ``None`` when ``private_key`` is empty / absent
(so consumers can use ``os.environ.get(...)`` directly without null-checks).

Requires the ``solders`` peer dependency (transitively via ``pympp[solana]``).
"""

from __future__ import annotations

import re
from typing import Any


def load_solana_fee_payer(private_key: str | None) -> Any | None:
    """Load a Solana fee-payer signer from a keypair string.

    Accepts:
    * 128-char hex (64 bytes: 32-byte secret + 32-byte public; pretrunc to 32)
    * base58 (Phantom export: 64 bytes secret+public OR 32 bytes secret-only)

    Returns ``None`` when ``private_key`` is empty/None.
    """
    if not private_key:
        return None

    try:
        from solders.keypair import Keypair  # type: ignore[import-not-found]
    except ImportError as err:
        msg = "solders not installed — run `pip install 'pympp[solana]>=0.6'` for load_solana_fee_payer."
        raise ImportError(msg) from err

    if re.fullmatch(r"[0-9a-fA-F]{128}", private_key):
        secret = bytes.fromhex(private_key)[:32]
        return Keypair.from_seed(secret)

    try:
        import base58  # type: ignore[import-not-found]
    except ImportError as err:
        msg = "base58 not installed — required for base58-encoded Solana fee-payer keys."
        raise ImportError(msg) from err

    decoded = base58.b58decode(private_key)
    if len(decoded) == 64:
        return Keypair.from_bytes(decoded)
    if len(decoded) == 32:
        return Keypair.from_seed(decoded)
    msg = f"load_solana_fee_payer: base58 keypair must decode to 32 or 64 bytes, got {len(decoded)}"
    raise ValueError(msg)


__all__ = ["load_solana_fee_payer"]
