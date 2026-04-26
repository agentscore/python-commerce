"""Payment-signer extraction.

Pure-x402 extractor for Python. Two payload shapes are handled directly:

- **x402 EIP-3009** (EVM, e.g. Base/Sepolia) — `payload.authorization.from` recovered
  from the base64-encoded JSON body. No external deps.
- **x402 SVM** (Solana) — payload carries a base64-encoded Solana transaction; the
  signer is the SPL Token TransferChecked source-account owner. Decoding that
  transaction requires a Solana SDK (`solana-py` / `solders`) which isn't a hard
  dep of this package — merchants who need Solana signer recovery should extract
  the payer themselves and pass it to ``verify_wallet_signer_match`` via the
  ``signer=`` argument. We return ``None`` here so the caller knows we couldn't
  recover it.

Tempo MPP signer extraction is also caller-supplied — there's no pip-installable
equivalent of the node ``mppx`` library today.
"""

from __future__ import annotations

import base64
import json
import re

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def extract_x402_signer(x402_payment_header: str | None) -> str | None:
    """Decode an x402 ``payment-signature`` / ``x-payment`` header and return the signer.

    Currently extracts EVM (EIP-3009) signers only — see module docstring for why
    Solana extraction is left to callers. Returns ``None`` when the header is
    missing, malformed, or the rail isn't EVM x402.
    """
    if not x402_payment_header:
        return None
    try:
        decoded = base64.b64decode(x402_payment_header, validate=False).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    # Network-aware: branch on `accepted.network` so we explicitly skip Solana payloads
    # rather than silently misinterpreting them as malformed EVM.
    accepted = parsed.get("accepted") if isinstance(parsed.get("accepted"), dict) else {}
    network = accepted.get("network") if isinstance(accepted, dict) else None
    if isinstance(network, str) and network.startswith("solana:"):
        # Caller must extract the SPL Token payer themselves and pass it via signer=.
        return None

    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        return None
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict):
        return None
    sender = authorization.get("from")
    # EIP-3009 addresses are case-insensitive in the protocol; lowercasing is safe and
    # matches how the API stores them. Solana would NOT be safe to lowercase, but the
    # network branch above ensures we never reach this line on a Solana payload.
    if isinstance(sender, str) and _EVM_RE.match(sender):
        return sender.lower()
    return None
