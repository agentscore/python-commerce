"""Canonical `*RailSpec` types — one shape per rail, consumed by every helper.

A merchant accepting Tempo + Base + Solana + Stripe declares one `*RailSpec`
per rail and passes it to every helper (`build_accepted_methods`,
`build_how_to_pay`, `mpp_payment_handler`, `create_mppx_server`, ...). One
canonical shape per rail means the recipient address, network identifier, and
token defaults are declared once and reused everywhere.

`RecipientLike` is polymorphic over `str | Callable[[], Awaitable[str]]` so
per-order recipients (Stripe-multichain mints fresh deposit addresses per
PaymentIntent) flow through identically to static-treasury recipients. The
factory is called once per helper invocation; callers cache externally.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from agentscore_commerce.payment.networks import networks
from agentscore_commerce.payment.usdc import USDC

RecipientLike = str | Callable[[], Awaitable[str]] | Callable[[], str]


async def resolve_recipient(r: RecipientLike) -> str:
    """Resolve a `RecipientLike` to a concrete address string.

    Accepts a string (returned verbatim), a sync callable (called once), or an
    async callable (awaited once). Helpers call this on every invocation;
    callers that want once-per-session resolution should cache externally.
    """
    if isinstance(r, str):
        return r
    result = r()
    if inspect.isawaitable(result):
        return cast("str", await result)
    return cast("str", result)


@dataclass
class TempoRailSpec:
    """Canonical config for the Tempo MPP rail."""

    recipient: RecipientLike
    network: str = "tempo-mainnet"
    chain_id: int = 4217
    token: str = USDC.tempo.mainnet.address
    symbol: str = "USDC.e"
    decimals: int = 6
    testnet: bool = False
    recommend: Literal["tempo", "agentscore-pay", "both"] = "both"


@dataclass
class X402BaseRailSpec:
    """Canonical config for the x402 EVM (Base) rail."""

    recipient: RecipientLike
    network: str = "eip155:8453"  # CAIP-2 canonical
    chain_id: int = 8453
    token: str = USDC.base.mainnet.address
    symbol: str = "USDC"
    decimals: int = 6
    mode: Literal["exact", "upto"] = "exact"


@dataclass
class SolanaMppRailSpec:
    """Canonical config for the Solana MPP rail.

    `signer` is an optional fee-payer signer for server-side fee sponsorship —
    typed as `Any` to avoid hard-importing `@solana/kit`-equivalent types here.
    """

    recipient: RecipientLike
    network: str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    token: str = USDC.solana.mainnet.mint
    symbol: str = "USDC"
    decimals: int = 6
    rpc_url: str | None = None
    signer: Any | None = None
    token_program: str | None = None


@dataclass
class StripeRailSpec:
    """Canonical config for the Stripe SPT rail.

    `recipient` is intentionally absent — Stripe rails use `profile_id` as the
    merchant-side network identifier the agent's SPT is scoped to; the
    transaction recipient is the merchant's Stripe account, not an on-chain
    address.
    """

    profile_id: str | None = None
    rails: list[str] = field(default_factory=lambda: ["card", "link", "shared_payment_token"])
    payment_method_types: list[str] | None = None
    product_name: str | None = None
    secret_key: str | None = None


@dataclass
class TempoSessionRailSpec:
    """Canonical config for the Tempo session MPP rail (pay-as-you-go channels).

    `escrow_contract` is the merchant-deployed on-chain escrow that holds
    channel deposits + pays out cumulative vouchers on settlement.
    `store` is a `ChannelStore` instance (in-memory default for dev; Postgres /
    Redis-backed in production) — typed as `Any` to avoid hard-importing
    `mppx`'s store interface here.
    """

    recipient: RecipientLike
    escrow_contract: str
    store: Any
    currency: str = USDC.tempo.mainnet.address
    testnet: bool = False
    chains: Any | None = None


__all__ = [
    "RecipientLike",
    "SolanaMppRailSpec",
    "StripeRailSpec",
    "TempoRailSpec",
    "TempoSessionRailSpec",
    "X402BaseRailSpec",
    "resolve_recipient",
]


# Reference the networks module to keep an explicit dependency edge — the
# CAIP-2 default values above are sourced from `networks.tempo.mainnet.caip2`
# and `networks.base.mainnet.caip2` semantics. Asserted via tests.
_ = networks
