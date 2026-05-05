"""accepted_methods[] builder for enriched 402 bodies."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TempoConfig:
    recipient: str
    network: str = "tempo-mainnet"
    chain_id: int = 4217
    token: str = "0x20C000000000000000000000b9537d11c60E8b50"
    symbol: str = "USDC.e"
    decimals: int = 6


@dataclass
class X402BaseConfig:
    recipient: str
    network: str = "eip155:8453"
    chain_id: int = 8453
    token: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    symbol: str = "USDC"
    decimals: int = 6


@dataclass
class SolanaMppConfig:
    recipient: str
    network: str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    token: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    symbol: str = "USDC"
    decimals: int = 6


@dataclass
class StripeConfig:
    profile_id: str | None = None
    rails: list[str] = field(default_factory=lambda: ["card", "link", "shared_payment_token"])


@dataclass
class BuildAcceptedMethodsInput:
    tempo: TempoConfig | None = None
    x402_base: X402BaseConfig | None = None
    solana_mpp: SolanaMppConfig | None = None
    stripe: StripeConfig | None = None


def build_accepted_methods(input: BuildAcceptedMethodsInput) -> list[dict[str, Any]]:
    """Build the accepted_methods[] array. Each rail entry conditionally included if vendor passed it."""
    out: list[dict[str, Any]] = []
    if input.tempo:
        out.append(
            {
                "method": "tempo/charge",
                "network": input.tempo.network,
                "chain_id": input.tempo.chain_id,
                "token": input.tempo.token,
                "symbol": input.tempo.symbol,
                "decimals": input.tempo.decimals,
                "pay_to": input.tempo.recipient,
            }
        )
    if input.x402_base:
        out.append(
            {
                "method": "x402/exact",
                "network": input.x402_base.network,
                "chain_id": input.x402_base.chain_id,
                "token": input.x402_base.token,
                "symbol": input.x402_base.symbol,
                "decimals": input.x402_base.decimals,
                "pay_to": input.x402_base.recipient,
            }
        )
    if input.solana_mpp:
        out.append(
            {
                "method": "x402/exact",
                "network": input.solana_mpp.network,
                "token": input.solana_mpp.token,
                "symbol": input.solana_mpp.symbol,
                "decimals": input.solana_mpp.decimals,
                "pay_to": input.solana_mpp.recipient,
            }
        )
    if input.stripe:
        out.append({"method": "stripe/charge", "rails": input.stripe.rails, "profile_id": input.stripe.profile_id})
    return out
