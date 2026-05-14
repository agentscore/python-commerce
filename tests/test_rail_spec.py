"""Tests for the canonical *RailSpec types + RecipientLike resolution."""

from __future__ import annotations

import pytest

from agentscore_commerce.payment import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    TempoSessionRailSpec,
    X402BaseRailSpec,
    resolve_recipient,
)


def test_tempo_rail_spec_defaults() -> None:
    """Mainnet defaults match the USDC + networks registries."""
    spec = TempoRailSpec(recipient="0xfeedface")
    assert spec.recipient == "0xfeedface"
    assert spec.network == "tempo-mainnet"
    assert spec.chain_id == 4217
    assert spec.symbol == "USDC.e"
    assert spec.decimals == 6
    assert spec.testnet is False
    assert spec.recommend == "both"


def test_x402_base_rail_spec_defaults() -> None:
    """Defaults pin Base mainnet (CAIP-2 `eip155:8453`) + USDC."""
    spec = X402BaseRailSpec(recipient="0xfeedface")
    assert spec.network == "eip155:8453"
    assert spec.chain_id == 8453
    assert spec.symbol == "USDC"
    assert spec.decimals == 6
    assert spec.mode == "exact"


def test_x402_base_rail_spec_upto_mode() -> None:
    """`mode='upto'` is the Permit2 + Settlement-Overrides variant."""
    spec = X402BaseRailSpec(recipient="0xfeedface", mode="upto")
    assert spec.mode == "upto"


def test_solana_mpp_rail_spec_defaults() -> None:
    spec = SolanaMppRailSpec(recipient="GEQg2TM4VL315Bd4LLkGrhBjdNfoatKjCJYHBDPM3D74")
    assert spec.network.startswith("solana:")
    assert spec.symbol == "USDC"
    assert spec.decimals == 6
    assert spec.rpc_url is None
    assert spec.signer is None
    assert spec.token_program is None


def test_solana_mpp_rail_spec_with_fee_payer_signer() -> None:
    """Fee-payer signer roundtrips through the spec — opaque object."""
    sentinel_signer = object()
    spec = SolanaMppRailSpec(recipient="GEQg2TM4VL315Bd4LLkGrhBjdNfoatKjCJYHBDPM3D74", signer=sentinel_signer)
    assert spec.signer is sentinel_signer


def test_stripe_rail_spec_defaults() -> None:
    """Stripe has no on-chain recipient; profile_id replaces it."""
    spec = StripeRailSpec(profile_id="profile_abc")
    assert spec.profile_id == "profile_abc"
    assert spec.rails == ["card", "link", "shared_payment_token"]
    assert spec.payment_method_types is None
    assert spec.product_name is None
    assert spec.secret_key is None


def test_stripe_rail_spec_default_factory_isolation() -> None:
    """Each instance gets its own rails list (no shared mutable default)."""
    a = StripeRailSpec()
    b = StripeRailSpec()
    a.rails.append("custom")
    assert b.rails == ["card", "link", "shared_payment_token"]


def test_tempo_session_rail_spec_defaults() -> None:
    """Session rail requires escrow + store; defaults mirror tempo."""
    spec = TempoSessionRailSpec(
        recipient="0xfeedface",
        escrow_contract="0xescrow",
        store=object(),
    )
    assert spec.escrow_contract == "0xescrow"
    assert spec.testnet is False
    assert spec.chains is None


@pytest.mark.asyncio
async def test_resolve_recipient_string_returns_verbatim() -> None:
    assert await resolve_recipient("0xfeedface") == "0xfeedface"


@pytest.mark.asyncio
async def test_resolve_recipient_sync_callable() -> None:
    """Sync factory: called once, return value used directly."""
    calls = 0

    def factory() -> str:
        nonlocal calls
        calls += 1
        return "0xdynamic"

    assert await resolve_recipient(factory) == "0xdynamic"
    assert calls == 1


@pytest.mark.asyncio
async def test_resolve_recipient_async_callable() -> None:
    """Async factory: awaited once."""
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return "0xdynamic"

    assert await resolve_recipient(factory) == "0xdynamic"
    assert calls == 1


@pytest.mark.asyncio
async def test_resolve_recipient_called_once_per_resolution() -> None:
    """Each `resolve_recipient` call invokes the factory once — caching is caller-side."""
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return f"0xattempt-{calls}"

    assert await resolve_recipient(factory) == "0xattempt-1"
    assert await resolve_recipient(factory) == "0xattempt-2"
    assert calls == 2
