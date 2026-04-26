"""Network-aware signer extraction from x402 (EVM EIP-3009) credentials.

Mirror of node-commerce's `extract_payment_signer` shape — returns `{address, network}` so
vendors can pass the network into `capture_wallet(...)` without inferring it themselves.
For Tempo MPP and Solana SPL Token signers, callers must extract the signer themselves
(no pip-installable equivalent of `mppx` / `@x402/svm` today) and pass it directly to
`verify_wallet_signer_match` via the `signer=` argument.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Literal

from agentscore_commerce.identity.signer import extract_x402_signer

SignerNetwork = Literal["evm", "solana"]
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class PaymentSigner:
    """Recovered wallet signer + the network family it belongs to.

    `network` tells `capture_wallet(...)` which key family to attribute the signer to.
    """

    address: str
    network: SignerNetwork


def extract_payment_signer(x402_payment_header: str | None) -> PaymentSigner | None:
    """Decode an x402 header and return `{address, network}` or None.

    Returns the EVM `from` address with `network='evm'` when the payload is EIP-3009 shape.
    Returns None for Solana payloads (caller extracts SPL Token payer separately) or any
    malformed/missing header.
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

    accepted = parsed.get("accepted") if isinstance(parsed.get("accepted"), dict) else {}
    network = accepted.get("network") if isinstance(accepted, dict) else None
    if isinstance(network, str) and network.startswith("solana:"):
        # Caller must extract SPL Token payer themselves.
        return None

    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        return None
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict):
        return None
    sender = authorization.get("from")
    if isinstance(sender, str) and _EVM_RE.match(sender):
        return PaymentSigner(address=sender.lower(), network="evm")
    return None


__all__ = ["PaymentSigner", "SignerNetwork", "extract_payment_signer", "extract_x402_signer"]
