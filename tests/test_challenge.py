import pytest

from agentscore_commerce.challenge import (
    PricingBlock,
    SignerMatchResult,
    X402PaymentRequired,
    build_402_body,
    build_accepted_methods,
    build_agent_instructions,
    build_how_to_pay,
    build_identity_metadata,
)
from agentscore_commerce.payment import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
)


@pytest.mark.asyncio
async def test_build_accepted_methods_includes_only_provided_rails():
    out = await build_accepted_methods(
        tempo=TempoRailSpec(recipient="0xT"),
        stripe=StripeRailSpec(profile_id="acct_x"),
    )
    methods = [e["method"] for e in out]
    assert "tempo/charge" in methods
    assert "stripe/charge" in methods
    assert all(e["method"] != "x402/exact" for e in out)


@pytest.mark.asyncio
async def test_build_accepted_methods_full_set():
    out = await build_accepted_methods(
        tempo=TempoRailSpec(recipient="0xT"),
        x402_base=X402BaseRailSpec(recipient="0xB"),
        solana_mpp=SolanaMppRailSpec(recipient="solanaaddr"),
        stripe=StripeRailSpec(profile_id="acct_x"),
    )
    assert len(out) == 4
    assert out[1]["pay_to"] == "0xB"
    assert out[2]["network"].startswith("solana:")
    assert out[2]["method"] == "solana/charge"


@pytest.mark.asyncio
async def test_build_accepted_methods_overrides_defaults():
    """Per-rail spec fields override the rail's protocol defaults."""
    out = await build_accepted_methods(
        tempo=TempoRailSpec(recipient="0xT", network="tempo-testnet", chain_id=42431),
    )
    assert out[0]["network"] == "tempo-testnet"
    assert out[0]["chain_id"] == 42431


@pytest.mark.asyncio
async def test_build_accepted_methods_recipient_factory_called_per_helper():
    """Async recipient factory resolves to a fresh address on each invocation."""
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return f"0xfresh-{calls}"

    first = await build_accepted_methods(tempo=TempoRailSpec(recipient=factory))
    second = await build_accepted_methods(tempo=TempoRailSpec(recipient=factory))
    assert first[0]["pay_to"] == "0xfresh-1"
    assert second[0]["pay_to"] == "0xfresh-2"


def test_build_identity_metadata_wallet_mode_emits_signer_constraint():
    md = build_identity_metadata(mode="wallet", wallet="0xClaim", linked_wallets=["0xSibling"])
    assert md["identity_mode"] == "wallet"
    assert md["required_signer"] == "0xClaim"
    assert md["linked_wallets"] == ["0xSibling"]
    assert "signer_constraint" in md


def test_build_identity_metadata_signer_match_overrides_required():
    md = build_identity_metadata(
        mode="wallet",
        wallet="0xClaim",
        signer_match_result=SignerMatchResult(kind="pass", expected_signer="0xExpected"),
    )
    assert md["required_signer"] == "0xExpected"


def test_build_identity_metadata_token_mode_only_returns_mode():
    md = build_identity_metadata(mode="operator_token")
    assert md == {"identity_mode": "operator_token"}


@pytest.mark.asyncio
async def test_build_how_to_pay_honors_decimals_for_sub_cent_totals():
    # $0.0005 total → default 2-decimal precision would round to "0.00";
    # with decimals=4 the agent sees the real cap.
    out = await build_how_to_pay(
        url="https://ex.com/buy",
        retry_body_json="{}",
        total_usd=0.0005,
        decimals=4,
        rails={"x402_base": X402BaseRailSpec(recipient="0xB")},
    )
    assert "--max-spend 0.0005" in out["x402_base"]["command"]


@pytest.mark.asyncio
async def test_build_how_to_pay_emits_per_rail_blocks():
    out = await build_how_to_pay(
        url="https://ex.com/buy",
        retry_body_json='{"x":1}',
        total_usd=10.0,
        rails={
            "tempo": TempoRailSpec(recipient="0xT"),
            "x402_base": X402BaseRailSpec(recipient="0xB"),
            "stripe": StripeRailSpec(profile_id="acct_x"),
        },
    )
    assert "tempo" in out
    assert out["tempo"]["command"].startswith("tempo request")
    assert "x402_base" in out
    assert "agentscore-pay pay POST" in out["x402_base"]["command"]
    assert "stripe" in out
    assert "setup_link_cli" in out["stripe"]


@pytest.mark.asyncio
async def test_build_how_to_pay_blocks_link_cli_above_500():
    out = await build_how_to_pay(
        url="https://ex.com/buy",
        retry_body_json="{}",
        total_usd=750.0,
        rails={"stripe": StripeRailSpec(profile_id="acct_x")},
    )
    assert "note" in out["stripe"]
    assert "setup_link_cli" not in out["stripe"]


@pytest.mark.asyncio
async def test_build_how_to_pay_recommend_kwarg_switches_command():
    """`recommend='agentscore-pay'` puts the pay CLI command as primary; `'tempo'` uses tempo request."""
    out = await build_how_to_pay(
        url="https://ex.com/buy",
        retry_body_json="{}",
        total_usd=5.0,
        rails={"tempo": TempoRailSpec(recipient="0xT", recommend="agentscore-pay")},
    )
    assert "agentscore-pay pay POST" in out["tempo"]["command"]
    assert "tempo request" in out["tempo"]["alternative_command"]


@pytest.mark.asyncio
async def test_build_how_to_pay_testnet_flag_swaps_network_name():
    """`TempoRailSpec(testnet=True)` surfaces 'tempo-testnet' in the prerequisite copy."""
    out = await build_how_to_pay(
        url="https://ex.com/buy",
        retry_body_json="{}",
        total_usd=5.0,
        rails={"tempo": TempoRailSpec(recipient="0xT", testnet=True)},
    )
    assert "tempo-testnet" in out["tempo"]["prerequisite"]


def test_build_agent_instructions_uses_defaults():
    out = build_agent_instructions(how_to_pay={"tempo": {}})
    assert out["timeout_seconds"] == 300
    assert any("agentscore-pay" in t for t in out["recommended_tools"])
    assert any("tempo wallet transfer" in w for w in out["warnings"])


def test_build_agent_instructions_warnings_match_rails():
    """Defaults adapt to which rails are present in how_to_pay."""
    x402_only = build_agent_instructions(how_to_pay={"x402_base": {}})
    assert not any("tempo wallet transfer" in w for w in x402_only["warnings"])
    assert any("x402 deposit addresses" in w for w in x402_only["warnings"])
    assert not any("tempo request" in t for t in x402_only["recommended_tools"])
    assert any("agentscore-pay" in t for t in x402_only["recommended_tools"])

    tempo_only = build_agent_instructions(how_to_pay={"tempo": {}})
    assert not any("x402 deposit addresses" in w for w in tempo_only["warnings"])

    stripe_only = build_agent_instructions(how_to_pay={"stripe": {}})
    assert stripe_only["warnings"] == []
    assert stripe_only["recommended_tools"] == []


def test_build_agent_instructions_appends_extra_warnings():
    """extra_warnings is appended to the rail-derived defaults."""
    out = build_agent_instructions(
        how_to_pay={"tempo": {}, "x402_base": {}},
        extra_warnings=["Solana unavailable for this order; use base or tempo."],
    )
    assert len(out["warnings"]) == 3
    assert "tempo wallet transfer" in out["warnings"][0]
    assert "Solana unavailable" in out["warnings"][2]


def test_build_agent_instructions_extra_warnings_ignored_when_warnings_set():
    """Explicit warnings override defaults AND extra_warnings."""
    out = build_agent_instructions(
        how_to_pay={"tempo": {}},
        warnings=["custom only"],
        extra_warnings=["ignored"],
    )
    assert out["warnings"] == ["custom only"]


def test_build_402_body_assembles_full_response():
    body = build_402_body(
        accepted_methods=[{"method": "tempo/charge"}],
        agent_instructions={"how_to_pay": {}},
        identity_metadata={"identity_mode": "wallet"},
        pricing=PricingBlock(subtotal="100", tax="8", tax_rate=0.08, tax_state="CA", total="108"),
        amount_usd="108",
        order_id="ord_1",
        x402=X402PaymentRequired(accepts=[{}]),
    )
    assert body["payment_required"] is True
    assert body["x402Version"] == 2
    assert body["pricing"]["total"] == "108"
    assert body["identity_mode"] == "wallet"
    assert body["agent_instructions"]["how_to_pay"] == {}


def test_build_402_body_keeps_accepts_byte_identical():
    """accepts entries pass through unchanged — no v1 maxAmountRequired alias.

    @x402/core matches v2 by whole-object deepEqual of the echoed requirement, so an
    extra maxAmountRequired the server's rebuild lacks silently fails settle.
    """
    body = build_402_body(
        accepted_methods=[],
        x402=X402PaymentRequired(
            accepts=[{"scheme": "exact", "network": "eip155:84532", "amount": "110000"}],
            version=2,
        ),
    )
    entry = body["accepts"][0]
    assert entry["amount"] == "110000"
    assert "maxAmountRequired" not in entry


def test_build_402_body_emits_extensions_when_present():
    """A non-empty x402.extensions block is surfaced on body.extensions per x402 spec."""
    body = build_402_body(
        accepted_methods=[],
        x402=X402PaymentRequired(
            accepts=[{"scheme": "exact"}],
            version=2,
            extensions={"bazaar": {"discoverable": True}},
        ),
    )
    assert body["extensions"] == {"bazaar": {"discoverable": True}}
