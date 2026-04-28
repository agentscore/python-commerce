"""x402 boot-time + per-request validation helpers.

Two layers of validation every x402-accepting merchant repeats:

- **Boot-time**: validate the configured ``X402_BASE_NETWORK`` + ``X402_SVM_NETWORK``
  env vars are in the supported set, and aren't pointing at the same network family.
  Failing loud at boot is much better than per-request "unsupported network" errors
  after a misconfigured deploy.

- **Per-request**: when an x402 X-Payment header arrives, parse the base64 payload,
  extract the signed network + payTo, validate against the merchant's accepted
  networks, validate the payTo address shape per network family, and check that the
  payTo was minted by THIS merchant (cache hit). Each step has its own denial code
  and ``next_steps`` shape — getting the message right by hand across 4 conditions
  is fiddly.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentscore_commerce.payment.networks import network_family, networks

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: CAIP-2 networks the commerce SDK supports for x402 Base (EVM USDC).
X402_SUPPORTED_BASE_NETWORKS: frozenset[str] = frozenset({networks.base.mainnet.caip2, networks.base.sepolia.caip2})

#: CAIP-2 networks the commerce SDK supports for x402 Solana (SPL Token USDC).
X402_SUPPORTED_SVM_NETWORKS: frozenset[str] = frozenset({networks.solana.mainnet.caip2, networks.solana.devnet.caip2})


@dataclass
class ValidateX402NetworkConfigInput:
    """Input for :func:`validate_x402_network_config`."""

    base_network: str
    svm_network: str


def validate_x402_network_config(input: ValidateX402NetworkConfigInput) -> None:
    """Boot-time guard: raise if either network isn't supported, or if both share a family.

    Raises ``ValueError`` with a message that names the unsupported value AND lists the
    valid options — agents tracking down a misconfigured deploy don't need to grep for
    the supported list.
    """
    if input.base_network not in X402_SUPPORTED_BASE_NETWORKS:
        raise ValueError(
            f"X402_BASE_NETWORK={input.base_network} is not supported. "
            f"Use one of: {', '.join(sorted(X402_SUPPORTED_BASE_NETWORKS))}"
        )
    if input.svm_network not in X402_SUPPORTED_SVM_NETWORKS:
        raise ValueError(
            f"X402_SVM_NETWORK={input.svm_network} is not supported. "
            f"Use one of: {', '.join(sorted(X402_SUPPORTED_SVM_NETWORKS))}"
        )
    if input.base_network == input.svm_network:
        raise ValueError(
            f"X402_BASE_NETWORK and X402_SVM_NETWORK must be different (both set to {input.base_network})."
        )


_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass
class VerifyX402RequestInput:
    """Input for :func:`verify_x402_request`."""

    #: The incoming request headers (case-insensitive lookup).
    headers: dict[str, str]
    #: Async lookup that returns ``True`` when the address was minted by this merchant
    #: (typically ``pi_cache.has_address``).
    is_cached_address: Callable[[str], Awaitable[bool]]
    #: The merchant's accepted CAIP-2 networks per family.
    accepted_base_network: str
    accepted_svm_network: str


@dataclass
class VerifyX402RequestSuccess:
    """Successful verification — caller passes ``payload`` straight into ``process_x402_settle``."""

    payload: dict[str, Any]
    signed_network: str
    signed_pay_to: str
    is_solana: bool
    ok: Literal[True] = True


@dataclass
class VerifyX402RequestFailure:
    """Failed verification — caller returns ``body`` with HTTP ``status``."""

    body: dict[str, Any]
    status: Literal[400] = 400
    ok: Literal[False] = False


VerifyX402RequestResult = VerifyX402RequestSuccess | VerifyX402RequestFailure


def _header_lookup(headers: dict[str, str], *names: str) -> str | None:
    lower = {k.lower(): v for k, v in headers.items()}
    for name in names:
        v = lower.get(name.lower())
        if v:
            return v
    return None


_REGENERATE_WARNING = (
    "If you're trying to pay with Tempo USDC, use `tempo request` (sends Authorization: Payment), "
    "not a manual X-Payment header. Do NOT use `tempo wallet transfer` — that sends USDC on-chain "
    "but will not complete the MPP handshake. For x402 on Base/Solana, use `agentscore-pay pay` so "
    "the X-Payment credential is signed and submitted; bare wallet transfers do not complete the handshake."
)


def _regenerate_body(message: str, user_message: str) -> dict[str, Any]:
    return {
        "error": {"code": "payment_proof_invalid", "message": message},
        "next_steps": {
            "action": "regenerate_payment_credential",
            "user_message": user_message,
            "warning": _REGENERATE_WARNING,
        },
    }


async def verify_x402_request(input: VerifyX402RequestInput) -> VerifyX402RequestResult:
    """Parse the x402 X-Payment header and validate network + payTo + cache hit.

    Returns ``VerifyX402RequestSuccess`` when valid; the caller passes ``payload``
    straight into :func:`process_x402_settle`. Returns ``VerifyX402RequestFailure``
    when invalid — ``body`` includes ``next_steps`` with ``regenerate_payment_credential``
    so agents can recover deterministically from the response alone.

    Reads the header from ``payment-signature`` first, falling back to ``x-payment``
    (both are in the wild as the binary-friendly transport name evolved).
    """
    header_value = _header_lookup(input.headers, "payment-signature", "x-payment")
    if not header_value:
        return VerifyX402RequestFailure(
            body=_regenerate_body(
                "X-Payment header missing",
                (
                    "No X-Payment header was sent. Generate the credential from the 402 "
                    "challenge and resubmit on the same endpoint."
                ),
            ),
        )

    try:
        payload = json.loads(base64.b64decode(header_value).decode())
    except Exception:
        return VerifyX402RequestFailure(
            body=_regenerate_body(
                "X-Payment header is not valid base64 JSON",
                (
                    "The payment credential could not be decoded. Reconstruct the "
                    "credential from the 402 challenge and retry."
                ),
            ),
        )

    accepted = payload.get("accepted") or {}
    signed_network = accepted.get("network")
    signed_pay_to = accepted.get("payTo")

    if not signed_network or signed_network not in (
        input.accepted_base_network,
        input.accepted_svm_network,
    ):
        return VerifyX402RequestFailure(
            body=_regenerate_body(
                (
                    f"Unsupported x402 network {signed_network or '<missing>'}; "
                    f"this server accepts {input.accepted_base_network} (Base) "
                    f"and {input.accepted_svm_network} (Solana)"
                ),
                (
                    "The credential signed for an unsupported network. Pick one of the "
                    "accepted networks from the 402 challenge and re-sign."
                ),
            ),
        )

    is_solana = network_family(signed_network) == "solana"
    re_match = _SOLANA_ADDRESS_RE if is_solana else _EVM_ADDRESS_RE
    if not signed_pay_to or not isinstance(signed_pay_to, str) or not re_match.match(signed_pay_to):
        return VerifyX402RequestFailure(
            body=_regenerate_body(
                f"Payment payload missing or malformed accepted.payTo address for network {signed_network}",
                (
                    "The credential payload is missing or malformed payTo for the signed "
                    "network. Reconstruct the credential from the 402 challenge."
                ),
            ),
        )

    if not await input.is_cached_address(signed_pay_to):
        return VerifyX402RequestFailure(
            body=_regenerate_body(
                "payTo address not found in cache or expired. Request a fresh 402 challenge and retry.",
                (
                    "The deposit address is unknown or expired on this server. Request a "
                    "fresh 402 challenge and re-sign against the new payTo."
                ),
            ),
        )

    return VerifyX402RequestSuccess(
        payload=payload,
        signed_network=signed_network,
        signed_pay_to=signed_pay_to,
        is_solana=is_solana,
    )
