"""Tests for ``agentscore_commerce.payment.compose_rails``."""

import pytest

from agentscore_commerce.payment.compose_rails import build_mppx_compose_rails


def test_emits_single_tempo_intent_when_only_tempo_recipient() -> None:
    rails = build_mppx_compose_rails(amount_usd="1.50", tempo_recipient="0x1234")
    assert len(rails) == 2  # tempo + stripe
    directive, payload = rails[0]
    assert directive == "tempo/charge"
    assert payload["amount"] == "1.50"
    assert payload["recipient"] == "0x1234"
    assert payload["decimals"] == 6


def test_adds_solana_intent_with_atomic_conversion() -> None:
    rails = build_mppx_compose_rails(
        amount_usd="2.00",
        tempo_recipient="0xabc",
        solana_recipient="SolAddr",
    )
    sol = next(r for r in rails if r[0] == "solana/charge")
    assert sol[1]["amount"] == "2000000"
    assert sol[1]["recipient"] == "SolAddr"
    assert sol[1]["decimals"] == 6


def test_omits_stripe_when_include_stripe_false() -> None:
    rails = build_mppx_compose_rails(
        amount_usd="0.10",
        tempo_recipient="0xabc",
        include_stripe=False,
    )
    assert all(r[0] != "stripe/charge" for r in rails)


def test_caller_provided_solana_network_wins() -> None:
    rails = build_mppx_compose_rails(
        amount_usd="1",
        tempo_recipient="0xabc",
        solana_recipient="SolAddr",
        solana_network="solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    )
    sol = next(r for r in rails if r[0] == "solana/charge")
    assert sol[1]["network"] == "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"


def test_raises_when_amount_unparseable_with_solana_rail() -> None:
    with pytest.raises(ValueError):
        build_mppx_compose_rails(
            amount_usd="nope",
            tempo_recipient="0xabc",
            solana_recipient="SolAddr",
        )


def test_auto_drops_stripe_when_amount_below_min(caplog: pytest.LogCaptureFixture) -> None:
    import agentscore_commerce.payment.compose_rails as mod

    mod._warned_stripe_below_minimum = False  # reset module-level warn-once flag
    with caplog.at_level("WARNING"):
        rails = build_mppx_compose_rails(amount_usd="0.01", tempo_recipient="0xabc")
    assert all(r[0] != "stripe/charge" for r in rails)


def test_keeps_stripe_at_50_cent_boundary() -> None:
    rails = build_mppx_compose_rails(amount_usd="0.50", tempo_recipient="0xabc")
    assert any(r[0] == "stripe/charge" for r in rails)
