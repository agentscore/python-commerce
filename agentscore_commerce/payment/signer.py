"""Network-aware signer extraction from x402 and MPP payment credentials.

`extract_payment_signer(x402_payment_header, *, authorization_header=...)` returns
`{address, network}` so vendors can pass the network into `capture_wallet(...)`
without inferring it themselves. Reads from either:

* the x402 EIP-3009 base64 payload (``payment-signature`` / ``x-payment`` header),
  matching ``payload.authorization.from``; or
* the MPP ``Authorization: Payment <base64>`` header value, matching the
  ``source`` (or ``challenge.source``) DID inside the credential
  (``did:pkh:eip155:<chain>:<addr>`` for EVM, ``did:pkh:solana:<genesis>:<addr>``
  for Solana).

Decoded inline (no ``mpp._parsing`` dependency); falls through to ``None`` for
anything malformed.

The MPP path requires the credential to carry a spec-compliant ``did:pkh``
source (top-level or under ``challenge``). Credentials that omit the source
field and rely on the Solana TransferChecked-authority fallback (extracting
the signer from the signed-tx payload via ``@solana/kit``) are recovered by
the Node sibling, not by this Python helper; Python has no ``@solana/kit``
equivalent. Production MPP clients emit the ``did:pkh`` source field, so
this is a non-issue for spec-compliant traffic.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentscore_commerce.identity.address import is_solana_address, is_valid_evm_address, normalize_address
from agentscore_commerce.identity.signer import extract_x402_signer

if TYPE_CHECKING:
    from collections.abc import Mapping

SignerNetwork = Literal["evm", "solana"]


@dataclass(frozen=True)
class PaymentSigner:
    """Recovered wallet signer + the network family it belongs to.

    `network` tells `capture_wallet(...)` which key family to attribute the signer to.
    """

    address: str
    network: SignerNetwork


def extract_payment_signer(
    x402_payment_header: str | None = None,
    /,
    *,
    authorization_header: str | None = None,
) -> PaymentSigner | None:
    """Decode an x402 or MPP payment header and return ``{address, network}`` or ``None``.

    Tries the x402 base64 payload first (EIP-3009 ``payload.authorization.from``).
    Falls through to the MPP ``Authorization: Payment <base64>`` header if supplied
    (reads ``source`` or ``challenge.source`` DID).

    Returns ``None`` for any missing, malformed, or unsupported payload.
    """
    if x402_payment_header:
        result = _extract_from_x402(x402_payment_header)
        if result is not None:
            return result
    if authorization_header:
        return _extract_from_mpp_auth(authorization_header)
    return None


def _extract_from_x402(x402_payment_header: str) -> PaymentSigner | None:
    """Recover the EVM signer from an x402 EIP-3009 base64 payload."""
    try:
        decoded = base64.b64decode(x402_payment_header, validate=False).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        return None
    authorization = payload.get("authorization")
    if not isinstance(authorization, dict):
        return None
    sender = authorization.get("from")
    if isinstance(sender, str) and is_valid_evm_address(sender):
        return PaymentSigner(address=normalize_address(sender), network="evm")
    return None


def _extract_from_mpp_auth(authorization: str) -> PaymentSigner | None:
    """Recover the signer from an MPP ``Authorization: Payment <base64>`` header value.

    Strips the ``Payment`` scheme prefix (case-insensitive per RFC 7235), base64-decodes
    the remainder, parses as JSON, and reads ``source`` or ``challenge.source`` as a
    ``did:pkh:eip155:<chain>:<addr>`` / ``did:pkh:solana:<genesis>:<addr>`` DID.
    """
    if not authorization.lower().startswith("payment "):
        return None
    token = authorization[len("payment ") :].strip()
    if not token:
        return None
    try:
        decoded = base64.b64decode(token, validate=False).decode("utf-8")
        credential = json.loads(decoded)
    except (ValueError, TypeError):
        return None
    if not isinstance(credential, dict):
        return None
    source = credential.get("source")
    if not isinstance(source, str):
        challenge = credential.get("challenge")
        if isinstance(challenge, dict):
            source = challenge.get("source")
    if not isinstance(source, str):
        return None
    parts = source.split(":")
    if len(parts) < 4 or parts[0] != "did" or parts[1] != "pkh":
        return None
    family = parts[2]
    addr = parts[-1]
    if family == "eip155" and is_valid_evm_address(addr):
        return PaymentSigner(address=normalize_address(addr), network="evm")
    if family == "solana" and is_solana_address(addr):
        return PaymentSigner(address=normalize_address(addr), network="solana")
    return None


def extract_signer_for_precheck(headers: Mapping[str, str]) -> PaymentSigner | None:
    """One-call signer extraction across both supported credential formats.

    Tries the x402 ``X-Payment`` / ``payment-signature`` header first (EIP-3009
    ``payload.authorization.from``), then falls back to the MPP ``Authorization:
    Payment`` header DID. Returns the first one that resolves, or ``None``.

    Use this for wallet-cap prechecks and other "did the agent claim to sign as
    X?" checks where you need the signer BEFORE invoking Checkout; Checkout's
    own settle path runs verification separately and surfaces the verified
    signer on ``SettleOutcome.signer_address``.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    x402 = lower.get("payment-signature") or lower.get("x-payment")
    if x402:
        signer = extract_payment_signer(x402)
        if signer is not None:
            return signer
    authorization = lower.get("authorization")
    if authorization and authorization.lower().startswith("payment "):
        return extract_payment_signer(authorization_header=authorization)
    return None


def read_x402_payment_header(headers: Mapping[str, str]) -> str | None:
    """Read the x402 payment header from a request headers mapping (case-insensitive).

    Tries ``payment-signature`` first, then ``x-payment``; both names appear in the wild
    as the binary-friendly transport name evolved. Takes a mapping rather than a framework
    Request so the same helper works across FastAPI / Flask / Django / aiohttp / Sanic / ASGI.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    return lower.get("payment-signature") or lower.get("x-payment")


__all__ = [
    "PaymentSigner",
    "SignerNetwork",
    "extract_payment_signer",
    "extract_signer_for_precheck",
    "extract_x402_signer",
    "read_x402_payment_header",
]
