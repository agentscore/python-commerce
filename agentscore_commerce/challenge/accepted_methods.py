"""accepted_methods[] builder for enriched 402 bodies."""

from typing import Any

_DEFAULT_TEMPO = {
    "network": "tempo-mainnet",
    "chain_id": 4217,
    "token": "0x20C000000000000000000000b9537d11c60E8b50",
    "symbol": "USDC.e",
    "decimals": 6,
}
_DEFAULT_X402_BASE = {
    "network": "eip155:8453",
    "chain_id": 8453,
    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "symbol": "USDC",
    "decimals": 6,
}
_DEFAULT_SOLANA_MPP = {
    "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "token": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "symbol": "USDC",
    "decimals": 6,
}
_DEFAULT_STRIPE_RAILS = ["card", "link", "shared_payment_token"]


def build_accepted_methods(
    *,
    tempo: dict[str, Any] | None = None,
    x402_base: dict[str, Any] | None = None,
    solana_mpp: dict[str, Any] | None = None,
    stripe: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the accepted_methods[] array. Each rail entry conditionally included if vendor passed it.

    Each rail value is a plain dict. Required key: ``recipient`` (or ``profile_id`` for stripe).
    Optional keys override the rail's protocol defaults: ``network``, ``chain_id``, ``token``,
    ``symbol``, ``decimals`` (for chain rails) or ``rails`` (for stripe).
    """
    out: list[dict[str, Any]] = []
    if tempo:
        out.append(
            {
                "method": "tempo/charge",
                "network": tempo.get("network", _DEFAULT_TEMPO["network"]),
                "chain_id": tempo.get("chain_id", _DEFAULT_TEMPO["chain_id"]),
                "token": tempo.get("token", _DEFAULT_TEMPO["token"]),
                "symbol": tempo.get("symbol", _DEFAULT_TEMPO["symbol"]),
                "decimals": tempo.get("decimals", _DEFAULT_TEMPO["decimals"]),
                "pay_to": tempo["recipient"],
            }
        )
    if x402_base:
        out.append(
            {
                "method": "x402/exact",
                "network": x402_base.get("network", _DEFAULT_X402_BASE["network"]),
                "chain_id": x402_base.get("chain_id", _DEFAULT_X402_BASE["chain_id"]),
                "token": x402_base.get("token", _DEFAULT_X402_BASE["token"]),
                "symbol": x402_base.get("symbol", _DEFAULT_X402_BASE["symbol"]),
                "decimals": x402_base.get("decimals", _DEFAULT_X402_BASE["decimals"]),
                "pay_to": x402_base["recipient"],
            }
        )
    if solana_mpp:
        out.append(
            {
                "method": "x402/exact",
                "network": solana_mpp.get("network", _DEFAULT_SOLANA_MPP["network"]),
                "token": solana_mpp.get("token", _DEFAULT_SOLANA_MPP["token"]),
                "symbol": solana_mpp.get("symbol", _DEFAULT_SOLANA_MPP["symbol"]),
                "decimals": solana_mpp.get("decimals", _DEFAULT_SOLANA_MPP["decimals"]),
                "pay_to": solana_mpp["recipient"],
            }
        )
    if stripe:
        out.append(
            {
                "method": "stripe/charge",
                "rails": stripe.get("rails", _DEFAULT_STRIPE_RAILS),
                "profile_id": stripe.get("profile_id"),
            }
        )
    return out
