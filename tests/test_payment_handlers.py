"""Tests for the UCP payment-handler builders consuming *RailSpec."""

from __future__ import annotations

import pytest

from agentscore_commerce.identity.ucp import (
    mpp_payment_handler,
    stripe_spt_payment_handler,
    x402_payment_handler,
)
from agentscore_commerce.payment import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    TempoSessionRailSpec,
    X402BaseRailSpec,
)


def test_mpp_tempo_static_recipient() -> None:
    out = mpp_payment_handler(networks=[TempoRailSpec(recipient="0xfeedface")])
    binding = out["com.agentscore.payment.mpp"][0]
    assert binding.config is not None
    assert binding.config["networks"] == [
        {"network": "tempo-mainnet", "chain_id": 4217, "recipient": "0xfeedface"},
    ]


def test_mpp_tempo_testnet_overrides_network_name() -> None:
    out = mpp_payment_handler(networks=[TempoRailSpec(recipient="0xfeedface", testnet=True)])
    binding = out["com.agentscore.payment.mpp"][0]
    assert binding.config is not None
    entry = binding.config["networks"][0]
    assert entry["network"] == "tempo-testnet"


def test_mpp_tempo_factory_recipient_omitted_from_static_profile() -> None:
    """Per-order factory recipients are omitted from the UCP profile (only the 402 body carries them)."""

    async def factory() -> str:
        return "0xdynamic"

    out = mpp_payment_handler(networks=[TempoRailSpec(recipient=factory)])
    binding = out["com.agentscore.payment.mpp"][0]
    assert binding.config is not None
    entry = binding.config["networks"][0]
    assert "recipient" not in entry
    assert entry["network"] == "tempo-mainnet"
    assert entry["chain_id"] == 4217


def test_mpp_solana_caip2_to_ucp_namespace() -> None:
    spec = SolanaMppRailSpec(recipient="solanaaddr")
    out = mpp_payment_handler(networks=[spec])
    binding = out["com.agentscore.payment.mpp"][0]
    assert binding.config is not None
    entry = binding.config["networks"][0]
    assert entry["network"] == "solana-mainnet-beta"
    assert entry["recipient"] == "solanaaddr"


def test_mpp_solana_devnet_caip2_to_ucp_namespace() -> None:
    from agentscore_commerce.payment import networks

    spec = SolanaMppRailSpec(recipient="solanaaddr", network=networks.solana.devnet.caip2)
    out = mpp_payment_handler(networks=[spec])
    binding = out["com.agentscore.payment.mpp"][0]
    assert binding.config is not None
    assert binding.config["networks"][0]["network"] == "solana-devnet"


def test_mpp_mixed_tempo_solana_session() -> None:
    """One call can mix Tempo, Solana MPP, and Tempo session rails."""
    out = mpp_payment_handler(
        networks=[
            TempoRailSpec(recipient="0xtempo"),
            SolanaMppRailSpec(recipient="solanaaddr"),
            TempoSessionRailSpec(recipient="0xsession", escrow_contract="0xescrow", store=object()),
        ],
    )
    binding = out["com.agentscore.payment.mpp"][0]
    assert binding.config is not None
    entries = binding.config["networks"]
    assert len(entries) == 3
    assert entries[2]["escrow_contract"] == "0xescrow"


def test_mpp_unknown_spec_type_raises() -> None:
    with pytest.raises(TypeError, match="unsupported rail spec type"):
        mpp_payment_handler(networks=["not-a-spec"])  # type: ignore[list-item]


def test_x402_base_mainnet_caip2_to_ucp_namespace() -> None:
    out = x402_payment_handler(networks=[X402BaseRailSpec(recipient="0xbase")])
    binding = out["com.agentscore.payment.x402"][0]
    assert binding.config is not None
    entry = binding.config["networks"][0]
    assert entry["network"] == "base-8453"
    assert entry["recipient"] == "0xbase"


def test_x402_base_sepolia_caip2_to_ucp_namespace() -> None:
    out = x402_payment_handler(
        networks=[X402BaseRailSpec(recipient="0xbase", network="eip155:84532")],
    )
    binding = out["com.agentscore.payment.x402"][0]
    assert binding.config is not None
    assert binding.config["networks"][0]["network"] == "base-84532"


def test_x402_factory_recipient_omitted() -> None:
    async def factory() -> str:
        return "0xdynamic"

    out = x402_payment_handler(networks=[X402BaseRailSpec(recipient=factory)])
    binding = out["com.agentscore.payment.x402"][0]
    assert binding.config is not None
    assert "recipient" not in binding.config["networks"][0]


def test_x402_unknown_network_passes_through_verbatim() -> None:
    """A non-standard CAIP-2 (e.g. an unsupported chain) ships through unchanged."""
    out = x402_payment_handler(
        networks=[X402BaseRailSpec(recipient="0xbase", network="custom-rail-id")],
    )
    binding = out["com.agentscore.payment.x402"][0]
    assert binding.config is not None
    assert binding.config["networks"][0]["network"] == "custom-rail-id"


def test_stripe_spt_handler_emits_profile_id() -> None:
    out = stripe_spt_payment_handler(spec=StripeRailSpec(profile_id="profile_5xKvNqM9BaH"))
    binding = out["com.agentscore.payment.stripe_spt"][0]
    assert binding.config == {"rail": "stripe-spt", "profile_id": "profile_5xKvNqM9BaH"}


def test_handler_metadata_versioning() -> None:
    """All three handlers share the same handler-version constant and spec URL prefix."""
    mpp = mpp_payment_handler(networks=[TempoRailSpec(recipient="0xt")])
    x402 = x402_payment_handler(networks=[X402BaseRailSpec(recipient="0xb")])
    stripe = stripe_spt_payment_handler(spec=StripeRailSpec(profile_id="profile_x"))
    mpp_binding = mpp["com.agentscore.payment.mpp"][0]
    x402_binding = x402["com.agentscore.payment.x402"][0]
    stripe_binding = stripe["com.agentscore.payment.stripe_spt"][0]
    # All same version + spec/schema base.
    assert mpp_binding.version == x402_binding.version == stripe_binding.version
    assert all(
        b.spec.startswith("https://www.agentscore.com/specification/payment-handlers/")
        for b in [mpp_binding, x402_binding, stripe_binding]
    )
