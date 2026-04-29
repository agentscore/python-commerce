"""Builder for the x402 PAYMENT-REQUIRED ``accepts[]`` array.

Every merchant emitting x402 has to construct this array by hand — base, sepolia,
solana mainnet, solana devnet — each with its own asset address, EIP-712 domain
(EVM only), and feePayer (SVM only). This helper consolidates the per-rail
boilerplate so vendors declare ``X402Accept(network=..., recipient=..., amount=...)``
and get a spec-compliant entry back.

EVM rails get ``extra: {name: "USDC", version: "2"}`` baked in (required for
EIP-3009 TransferWithAuthorization signing). SVM rails take an explicit
``fee_payer`` argument — defaults to recipient (round-trip), but production
merchants typically point it at the Coinbase facilitator's payer address.

Lifted from agentscore/martin-estate + agentscore/store after both repos
hand-rolled the same block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentscore_commerce.payment.networks import network_family, networks
from agentscore_commerce.payment.usdc import USDC


@dataclass
class X402Accept:
    """One entry in the ``accepts[]`` array."""

    network: str
    """CAIP-2 network identifier (e.g. ``eip155:8453``, ``solana:5eykt4...``)."""

    amount_atomic: str
    """Atomic settle amount as a string (e.g. ``"10000"`` for 1 cent USDC at 6dp)."""

    recipient: str
    """Address that receives the settled transfer."""

    asset: str | None = None
    """Token contract / mint. Defaults to USDC for the network family."""

    fee_payer: str | None = None
    """SVM-only: address that pays Solana network fees. Defaults to ``recipient``
    on dev/devnet (round-trip to merchant). Production merchants typically set
    this to the Coinbase facilitator payer. Ignored for EVM networks."""

    max_timeout_seconds: int = 300
    """Window the agent has to sign and submit before the challenge expires."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Override or extend the per-rail extra block. Merged on top of the
    auto-derived values (EIP-712 domain on EVM, feePayer on SVM)."""


def build_x402_accept(input: X402Accept) -> dict[str, Any]:
    """Build a single x402 accept entry. Auto-fills asset + EIP-712 / feePayer extras."""
    family = network_family(input.network)
    asset = input.asset
    extra: dict[str, Any] = {}

    if family == "base":
        if asset is None:
            asset = (
                USDC.base.sepolia.address if input.network == networks.base.sepolia.caip2 else USDC.base.mainnet.address
            )
        # EIP-712 domain — required by every x402 EVM client to sign EIP-3009
        # TransferWithAuthorization. The official USDC contract uses ("USDC", "2")
        # for both mainnet and sepolia.
        extra = {"name": "USDC", "version": "2"}
    elif family == "solana":
        if asset is None:
            asset = (
                USDC.solana.devnet.mint if input.network == networks.solana.devnet.caip2 else USDC.solana.mainnet.mint
            )
        # SVM transactions require a feePayer; default to recipient so the
        # merchant pays gas and receives funds in one tx (round-trip safe for dev).
        extra = {"feePayer": input.fee_payer or input.recipient}

    extra.update(input.extra)

    return {
        "scheme": "exact",
        "network": input.network,
        "amount": input.amount_atomic,
        "asset": asset,
        "payTo": input.recipient,
        "maxTimeoutSeconds": input.max_timeout_seconds,
        "extra": extra,
    }


def build_x402_accepts(rails: list[X402Accept]) -> list[dict[str, Any]]:
    """Build the ``accepts[]`` array for the x402 PAYMENT-REQUIRED header / body."""
    return [build_x402_accept(r) for r in rails]


__all__ = ["X402Accept", "build_x402_accept", "build_x402_accepts"]
