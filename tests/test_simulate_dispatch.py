"""Tests for ``agentscore_commerce.stripe_multichain.simulate_dispatch``."""

from dataclasses import dataclass

import pytest

from agentscore_commerce.stripe_multichain.simulate_dispatch import (
    network_for_outcome,
    simulate_deposit_for_outcome,
)


@dataclass
class Outcome:
    rail: str = ""
    rail_key: str = ""
    mpp_method: str = ""


def test_x402_outcome_returns_base() -> None:
    assert network_for_outcome(Outcome(rail="x402")) == "base"


def test_accepts_bare_tempo_and_full_directive() -> None:
    assert network_for_outcome(Outcome(rail="mpp", mpp_method="tempo")) == "tempo"
    assert network_for_outcome(Outcome(rail="mpp", mpp_method="tempo/charge")) == "tempo"


def test_accepts_bare_solana_and_full_directive() -> None:
    assert network_for_outcome(Outcome(rail="mpp", mpp_method="solana")) == "solana"
    assert network_for_outcome(Outcome(rail="mpp", mpp_method="solana/charge")) == "solana"


def test_stripe_returns_none() -> None:
    assert network_for_outcome(Outcome(rail="mpp", mpp_method="stripe")) is None
    assert network_for_outcome(Outcome(rail="mpp", mpp_method="stripe/charge")) is None


def test_falls_back_to_rail_key() -> None:
    assert network_for_outcome(Outcome(rail="mpp", rail_key="solana_mpp")) == "solana"
    assert network_for_outcome(Outcome(rail="mpp", rail_key="tempo_mpp")) == "tempo"
    assert network_for_outcome(Outcome(rail="mpp", rail_key="stripe")) is None


def test_unknown_outcome_returns_none() -> None:
    assert network_for_outcome(Outcome()) is None
    assert network_for_outcome(Outcome(rail="mpp", mpp_method="unknown")) is None


def test_accepts_dict_outcomes() -> None:
    assert network_for_outcome({"rail": "x402"}) == "base"
    assert network_for_outcome({"rail": "mpp", "mpp_method": "solana"}) == "solana"


@pytest.mark.asyncio
async def test_dispatcher_noop_on_stripe_spt() -> None:
    called: list[str] = []

    def get_pi(_addr: str) -> str | None:
        called.append("pi")
        return "pi_x"

    await simulate_deposit_for_outcome(
        outcome=Outcome(rail="mpp", mpp_method="stripe"),
        deposit_address="0xabc",
        get_payment_intent_id=get_pi,
        stripe_secret_key="sk_test_dummy",
    )
    assert called == []


@pytest.mark.asyncio
async def test_dispatcher_noop_on_live_stripe_key() -> None:
    called: list[str] = []

    def get_pi(_addr: str) -> str | None:
        called.append("pi")
        return "pi_x"

    await simulate_deposit_for_outcome(
        outcome=Outcome(rail="x402"),
        deposit_address="0xabc",
        get_payment_intent_id=get_pi,
        stripe_secret_key="sk_live_real",
    )
    # simulate_deposit_if_test_mode early-returns on sk_live_*; getter not called
    assert called == []


def test_none_outcome_returns_none() -> None:
    assert network_for_outcome(None) is None


def test_rail_key_x402_base_returns_base() -> None:
    assert network_for_outcome(Outcome(rail="mpp", rail_key="x402_base")) == "base"


@pytest.mark.asyncio
async def test_dispatcher_forwards_buyer_wallet_and_stripe_version(monkeypatch) -> None:
    import agentscore_commerce.stripe_multichain.simulate_dispatch as mod

    captured: dict = {}

    async def _fake_simulate(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(mod, "simulate_deposit_if_test_mode", _fake_simulate)
    await simulate_deposit_for_outcome(
        outcome=Outcome(rail="x402"),
        deposit_address="0xabc",
        get_payment_intent_id=lambda _a: "pi_x",
        stripe_secret_key="sk_test_dummy",
        stripe_version="2024-01-01",
        buyer_wallet="0xbuyer",
    )
    assert captured["network"] == "base"
    assert captured["buyer_wallet"] == "0xbuyer"
    assert captured["stripe_version"] == "2024-01-01"
