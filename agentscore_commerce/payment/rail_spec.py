"""Canonical `*RailSpec` types; one shape per rail, consumed by every helper.

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


_DEFAULT: Any = object()


@dataclass
class TempoRailSpec:
    """Canonical config for the Tempo MPP rail.

    Setting ``testnet=True`` auto-flips ``network`` / ``chain_id`` / ``token`` to
    their testnet (Moderato, chain 42431) values when those fields are left at
    their defaults. Explicit overrides still win.
    """

    recipient: RecipientLike
    network: str = _DEFAULT
    chain_id: int = _DEFAULT
    token: str = _DEFAULT
    symbol: str = "USDC.e"
    decimals: int = 6
    testnet: bool = False
    recommend: Literal["tempo", "agentscore-pay", "both"] = "both"

    def __post_init__(self) -> None:
        if self.testnet:
            if self.network is _DEFAULT:
                self.network = "tempo-testnet"
            if self.chain_id is _DEFAULT:
                self.chain_id = 42431
            if self.token is _DEFAULT:
                self.token = USDC.tempo.testnet.address
        else:
            if self.network is _DEFAULT:
                self.network = "tempo-mainnet"
            if self.chain_id is _DEFAULT:
                self.chain_id = 4217
            if self.token is _DEFAULT:
                self.token = USDC.tempo.mainnet.address


@dataclass
class X402BaseRailSpec:
    """Canonical config for the x402 EVM (Base) rail.

    Setting ``network`` to a known CAIP-2 (``eip155:8453`` mainnet,
    ``eip155:84532`` sepolia) auto-flips ``chain_id`` / ``token`` to the right
    values when those fields are left at their defaults. Explicit overrides
    still win.
    """

    recipient: RecipientLike
    network: str = "eip155:8453"
    chain_id: int = _DEFAULT
    token: str = _DEFAULT
    symbol: str = "USDC"
    decimals: int = 6
    mode: Literal["exact", "upto"] = "exact"

    def __post_init__(self) -> None:
        if self.network == "eip155:84532":
            if self.chain_id is _DEFAULT:
                self.chain_id = 84532
            if self.token is _DEFAULT:
                self.token = USDC.base.sepolia.address
        else:
            if self.chain_id is _DEFAULT:
                self.chain_id = 8453
            if self.token is _DEFAULT:
                self.token = USDC.base.mainnet.address


@dataclass
class SolanaMppRailSpec:
    """Canonical config for the Solana MPP rail.

    `signer` is an optional fee-payer signer for server-side fee sponsorship ;
    typed as `Any` to avoid hard-importing `@solana/kit`-equivalent types here.
    """

    recipient: RecipientLike
    network: str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    token: str = _DEFAULT
    symbol: str = "USDC"
    decimals: int = 6
    rpc_url: str | None = None
    signer: Any | None = None
    token_program: str | None = None
    # Whether the recipient's ATA may be auto-created on first payment. Default True.
    # When True (default), the SDK passes
    # ``splits=[{"recipient": recipient, "amount": "0", "ataCreationRequired": True}]``
    # to ``solana.charge``, putting the recipient in the MPP spec §13.6
    # ``allowedAtaOwners`` allow-list. Required on ``@solana/mpp >= 0.6.0`` /
    # ``pympp[solana] >= 0.6`` with a sponsored (fee-payer) setup — without it,
    # every settle that emits a ``CreateIdempotent`` ATA instruction is rejected.
    # On older runtimes the field is unknown and silently ignored, so the
    # default is safe across versions.
    #
    # Opt out (``False``) only when every recipient's ATA is guaranteed to
    # exist out-of-band (typically when the merchant pre-creates the ATA from
    # an external wallet and refuses to let the fee-payer fund creation).
    #
    # Economic note: with rotating recipients (Stripe-multichain per-PI deposit
    # addresses), the sponsor pays ~0.002 SOL (~$0.50) of rent per call into
    # accounts the merchant can't close. Acceptable when settle amounts
    # dominate ($50+); not viable for sub-dollar merchants.
    #
    # NOTE: SolanaMppRailSpec isn't yet wired through ``create_mppx_server``, so
    # this field is data-only today. Merchants building the solana method directly
    # via ``pympp`` should pass ``ata_creation_required`` to the charge factory.
    ata_creation_required: bool = True

    def __post_init__(self) -> None:
        # Mirror X402BaseRailSpec: when ``network`` flips to the devnet CAIP-2 (or the
        # raw ``'devnet'`` form @solana/mpp accepts), default ``token`` to devnet's USDC
        # mint instead of mainnet's. Explicit overrides still win.
        is_devnet = self.network in ("devnet", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1")
        if self.token is _DEFAULT:
            self.token = USDC.solana.devnet.mint if is_devnet else USDC.solana.mainnet.mint


@dataclass
class StripeRailSpec:
    """Canonical config for the Stripe SPT rail.

    `recipient` is intentionally absent; Stripe rails use `profile_id` as the
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
    Redis-backed in production); typed as `Any` to avoid hard-importing
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
