"""accepted_methods[] builder for enriched 402 bodies."""

from typing import Any

from agentscore_commerce.payment.rail_spec import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
    resolve_recipient,
)


async def build_accepted_methods(
    *,
    tempo: TempoRailSpec | None = None,
    x402_base: X402BaseRailSpec | None = None,
    solana_mpp: SolanaMppRailSpec | None = None,
    stripe: StripeRailSpec | None = None,
) -> list[dict[str, Any]]:
    """Build the accepted_methods[] array.

    Each rail entry is conditionally included when the vendor passed a `*RailSpec`
    for that rail. Each spec's `recipient` is resolved via `resolve_recipient` so
    per-order factories (e.g. Stripe-multichain mints fresh deposits per
    PaymentIntent) flow through identically to static-treasury strings.
    """
    out: list[dict[str, Any]] = []
    if tempo is not None:
        out.append(
            {
                "method": "tempo/charge",
                "network": tempo.network,
                "chain_id": tempo.chain_id,
                "token": tempo.token,
                "symbol": tempo.symbol,
                "decimals": tempo.decimals,
                "pay_to": await resolve_recipient(tempo.recipient),
            }
        )
    if x402_base is not None:
        out.append(
            {
                "method": "x402/exact",
                "network": x402_base.network,
                "chain_id": x402_base.chain_id,
                "token": x402_base.token,
                "symbol": x402_base.symbol,
                "decimals": x402_base.decimals,
                "pay_to": await resolve_recipient(x402_base.recipient),
            }
        )
    if solana_mpp is not None:
        out.append(
            {
                "method": "x402/exact",
                "network": solana_mpp.network,
                "token": solana_mpp.token,
                "symbol": solana_mpp.symbol,
                "decimals": solana_mpp.decimals,
                "pay_to": await resolve_recipient(solana_mpp.recipient),
            }
        )
    if stripe is not None:
        out.append(
            {
                "method": "stripe/charge",
                "rails": list(stripe.rails),
                "profile_id": stripe.profile_id,
            }
        )
    return out
