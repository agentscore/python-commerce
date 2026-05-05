"""Symbolic rail names mapped to their protocol details.

Vendors pass `rail='tempo-mainnet'` to the directive builder and the SDK fills in
method/network/decimals/currency from this registry. Custom rails not in this registry
can be passed by setting the lower-level fields directly on the directive builder.
"""

from dataclasses import dataclass

from agentscore_commerce.payment.networks import networks
from agentscore_commerce.payment.usdc import USDC


@dataclass(frozen=True)
class RailDefinition:
    method: str
    currency: str
    decimals: int
    network: str | None = None
    chain_id: int | None = None
    asset: str | None = None


rails: dict[str, RailDefinition] = {
    "tempo-mainnet": RailDefinition(
        method="tempo",
        network=networks.tempo.mainnet.caip2,
        chain_id=networks.tempo.mainnet.chain_id,
        currency=USDC.tempo.mainnet.address,
        decimals=USDC.tempo.mainnet.decimals,
        asset=USDC.tempo.mainnet.address,
    ),
    "tempo-testnet": RailDefinition(
        method="tempo",
        network=networks.tempo.testnet.caip2,
        chain_id=networks.tempo.testnet.chain_id,
        currency=USDC.tempo.testnet.address,
        decimals=USDC.tempo.testnet.decimals,
        asset=USDC.tempo.testnet.address,
    ),
    "x402-base-mainnet": RailDefinition(
        method="x402",
        network=networks.base.mainnet.caip2,
        chain_id=networks.base.mainnet.chain_id,
        currency=USDC.base.mainnet.address,
        decimals=USDC.base.mainnet.decimals,
        asset=USDC.base.mainnet.address,
    ),
    "x402-base-sepolia": RailDefinition(
        method="x402",
        network=networks.base.sepolia.caip2,
        chain_id=networks.base.sepolia.chain_id,
        currency=USDC.base.sepolia.address,
        decimals=USDC.base.sepolia.decimals,
        asset=USDC.base.sepolia.address,
    ),
    "x402-base-mainnet-upto": RailDefinition(
        method="x402-upto",
        network=networks.base.mainnet.caip2,
        chain_id=networks.base.mainnet.chain_id,
        currency=USDC.base.mainnet.address,
        decimals=USDC.base.mainnet.decimals,
        asset=USDC.base.mainnet.address,
    ),
    "x402-base-sepolia-upto": RailDefinition(
        method="x402-upto",
        network=networks.base.sepolia.caip2,
        chain_id=networks.base.sepolia.chain_id,
        currency=USDC.base.sepolia.address,
        decimals=USDC.base.sepolia.decimals,
        asset=USDC.base.sepolia.address,
    ),
    "mpp-solana-mainnet": RailDefinition(
        method="solana",
        network=networks.solana.mainnet.caip2,
        currency=USDC.solana.mainnet.mint,
        decimals=USDC.solana.mainnet.decimals,
        asset=USDC.solana.mainnet.mint,
    ),
    "mpp-solana-devnet": RailDefinition(
        method="solana",
        network=networks.solana.devnet.caip2,
        currency=USDC.solana.devnet.mint,
        decimals=USDC.solana.devnet.decimals,
        asset=USDC.solana.devnet.mint,
    ),
    "stripe-spt": RailDefinition(method="stripe", currency="usd", decimals=2),
}


def lookup_rail(name: str) -> RailDefinition | None:
    """Lookup a rail definition by symbolic name. Returns None if not in the registry."""
    return rails.get(name)
