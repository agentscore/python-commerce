"""Zero-amount carve-out: skip upstream verify+settle for $0 orders.

CDP rejects EIP-3009 ``transferWithAuthorization`` with ``value=0`` as
``invalid_payload``; pympp's tempo intents accept only ``hash`` and
``transaction`` payload types (rejecting the ``proof`` payload that ``mppx``
emits for $0 settles). Both upstream verify+settle paths fail when the
authorized amount is zero, so merchants that drop the settle to $0 in a
redemption-code flow need a way to skip verify+settle entirely while still
recovering the signer for wallet-capture attribution.

``zero_amount_carve_out`` is that path: parse the credential, lift the signer,
return ``ZeroSettleResult(signer_address, signer_network, tx_hash=None)``.
Identity is still authenticated by the merchant's gate above; the redemption
code is single-use; nothing on-chain to verify.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agentscore_commerce.identity.address import is_valid_evm_address, normalize_address
from agentscore_commerce.payment.signer import SignerNetwork, extract_payment_signer

ZeroSettleRail = Literal["x402-base", "tempo", "solana"]


@dataclass(frozen=True)
class ZeroSettleResult:
    """Result of a zero-amount carve-out: signer info + always-null tx hash.

    ``tx_hash`` is intentionally fixed to ``None``: a zero-amount carve-out
    skips on-chain settlement entirely, so no transaction hash exists. The
    field is present so callers can use ``ZeroSettleResult`` interchangeably
    with the success path of ``process_x402_settle`` etc. without branching.
    """

    signer_address: str | None
    signer_network: SignerNetwork | None
    tx_hash: None = None


def zero_amount_carve_out(
    *,
    rail: ZeroSettleRail,
    payload: dict[str, Any] | None = None,
    authorization_header: str | None = None,
) -> ZeroSettleResult:
    """Skip verify+settle for a zero-amount order; recover the signer from the credential.

    For ``rail="x402-base"``: pass ``payload`` (the verified x402 dict, typically
    ``verify_x402_request(...).payload``). Reads
    ``payload["payload"]["authorization"]["from"]``.

    For ``rail="tempo"`` or ``"solana"``: pass ``authorization_header`` (the full
    ``Authorization: Payment <base64>`` header value). Reads the ``did:pkh:*``
    source DID via :func:`extract_payment_signer`.

    Returns :class:`ZeroSettleResult`. ``signer_address`` and ``signer_network``
    are ``None`` when the credential is malformed, missing required fields,
    or shaped wrong for the requested rail. ``tx_hash`` is always ``None``
    since no on-chain settle runs.
    """
    if rail == "x402-base":
        return _x402_signer_from_payload(payload)
    if rail in ("tempo", "solana"):
        return _mpp_signer_from_auth(authorization_header)
    return ZeroSettleResult(signer_address=None, signer_network=None)


def _x402_signer_from_payload(payload: dict[str, Any] | None) -> ZeroSettleResult:
    """Read the EVM signer from a verified x402 EIP-3009 payload dict."""
    if not isinstance(payload, dict):
        return ZeroSettleResult(signer_address=None, signer_network=None)
    inner = payload.get("payload")
    if not isinstance(inner, dict):
        return ZeroSettleResult(signer_address=None, signer_network=None)
    authorization = inner.get("authorization")
    if not isinstance(authorization, dict):
        return ZeroSettleResult(signer_address=None, signer_network=None)
    from_addr = authorization.get("from")
    if not isinstance(from_addr, str) or not is_valid_evm_address(from_addr):
        return ZeroSettleResult(signer_address=None, signer_network=None)
    return ZeroSettleResult(signer_address=normalize_address(from_addr), signer_network="evm")


def _mpp_signer_from_auth(authorization_header: str | None) -> ZeroSettleResult:
    """Read the signer from an MPP ``Authorization: Payment <base64>`` header."""
    if not isinstance(authorization_header, str):
        return ZeroSettleResult(signer_address=None, signer_network=None)
    signer = extract_payment_signer(authorization_header=authorization_header)
    if signer is None:
        return ZeroSettleResult(signer_address=None, signer_network=None)
    return ZeroSettleResult(signer_address=signer.address, signer_network=signer.network)
