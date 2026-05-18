"""Tests for ``agentscore_commerce.payment.default_rails``."""

from agentscore_commerce.payment.default_rails import build_default_checkout_rails
from agentscore_commerce.payment.rail_spec import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
)


def test_empty_when_nothing_requested() -> None:
    assert build_default_checkout_rails() == {}


def test_tempo_with_sentinel_recipient() -> None:
    rails = build_default_checkout_rails(tempo={})
    assert "tempo" in rails
    assert isinstance(rails["tempo"], TempoRailSpec)
    assert rails["tempo"].recipient == ""
    assert rails["tempo"].network == "tempo-mainnet"


def test_caller_overrides_apply() -> None:
    rails = build_default_checkout_rails(
        tempo={"testnet": True},
    )
    assert rails["tempo"].testnet is True
    # testnet flag flips network/chain via TempoRailSpec.__post_init__
    assert rails["tempo"].network == "tempo-testnet"
    assert rails["tempo"].chain_id == 42431


def test_keys_use_canonical_slugs() -> None:
    rails = build_default_checkout_rails(x402_base={}, solana_mpp={})
    assert isinstance(rails["x402_base"], X402BaseRailSpec)
    assert isinstance(rails["solana_mpp"], SolanaMppRailSpec)


def test_stripe_has_no_recipient_field() -> None:
    rails = build_default_checkout_rails(
        stripe={"profile_id": "p_test", "payment_method_types": ["card", "link"]},
    )
    assert isinstance(rails["stripe"], StripeRailSpec)
    assert rails["stripe"].profile_id == "p_test"
    assert rails["stripe"].payment_method_types == ["card", "link"]
