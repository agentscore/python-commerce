from agentscore_commerce.challenge import (
    Build402BodyInput,
    BuildAcceptedMethodsInput,
    BuildAgentInstructionsInput,
    BuildHowToPayInput,
    HowToPayRails,
    IdentityMetadataInput,
    PricingBlock,
    SignerMatchResult,
    StripeConfig,
    StripeRailConfig,
    TempoConfig,
    TempoRailConfig,
    X402BaseConfig,
    X402BaseRailConfig,
    X402PaymentRequired,
    X402SolanaConfig,
    build_402_body,
    build_accepted_methods,
    build_agent_instructions,
    build_how_to_pay,
    build_identity_metadata,
)


def test_build_accepted_methods_includes_only_provided_rails():
    out = build_accepted_methods(
        BuildAcceptedMethodsInput(
            tempo=TempoConfig(recipient="0xT"),
            stripe=StripeConfig(profile_id="acct_x"),
        )
    )
    methods = [e["method"] for e in out]
    assert "tempo/charge" in methods
    assert "stripe/charge" in methods
    assert all(e["method"] != "x402/exact" for e in out)


def test_build_accepted_methods_full_set():
    out = build_accepted_methods(
        BuildAcceptedMethodsInput(
            tempo=TempoConfig(recipient="0xT"),
            x402_base=X402BaseConfig(recipient="0xB"),
            x402_solana=X402SolanaConfig(recipient="solanaaddr"),
            stripe=StripeConfig(profile_id="acct_x"),
        )
    )
    assert len(out) == 4
    assert out[1]["pay_to"] == "0xB"
    assert out[2]["network"].startswith("solana:")


def test_build_identity_metadata_wallet_mode_emits_signer_constraint():
    md = build_identity_metadata(IdentityMetadataInput(mode="wallet", wallet="0xClaim", linked_wallets=["0xSibling"]))
    assert md["identity_mode"] == "wallet"
    assert md["required_signer"] == "0xClaim"
    assert md["linked_wallets"] == ["0xSibling"]
    assert "signer_constraint" in md


def test_build_identity_metadata_signer_match_overrides_required():
    md = build_identity_metadata(
        IdentityMetadataInput(
            mode="wallet",
            wallet="0xClaim",
            signer_match_result=SignerMatchResult(kind="pass", expected_signer="0xExpected"),
        )
    )
    assert md["required_signer"] == "0xExpected"


def test_build_identity_metadata_token_mode_only_returns_mode():
    md = build_identity_metadata(IdentityMetadataInput(mode="operator_token"))
    assert md == {"identity_mode": "operator_token"}


def test_build_how_to_pay_emits_per_rail_blocks():
    out = build_how_to_pay(
        BuildHowToPayInput(
            url="https://ex.com/buy",
            retry_body_json='{"x":1}',
            total_usd=10.0,
            rails=HowToPayRails(
                tempo=TempoRailConfig(recipient="0xT"),
                x402_base=X402BaseRailConfig(recipient="0xB"),
                stripe=StripeRailConfig(profile_id="acct_x"),
            ),
        )
    )
    assert "tempo" in out
    assert out["tempo"]["command"].startswith("tempo request")
    assert "x402_base" in out
    assert "agentscore-pay pay POST" in out["x402_base"]["command"]
    assert "stripe" in out
    assert "setup_link_cli" in out["stripe"]


def test_build_how_to_pay_blocks_link_cli_above_500():
    out = build_how_to_pay(
        BuildHowToPayInput(
            url="https://ex.com/buy",
            retry_body_json="{}",
            total_usd=750.0,
            rails=HowToPayRails(stripe=StripeRailConfig(profile_id="acct_x")),
        )
    )
    assert "note" in out["stripe"]
    assert "setup_link_cli" not in out["stripe"]


def test_build_agent_instructions_uses_defaults():
    out = build_agent_instructions(BuildAgentInstructionsInput(how_to_pay={"tempo": {}}))
    assert out["timeout_seconds"] == 300
    assert any("agentscore-pay" in t for t in out["recommended_tools"])
    assert any("tempo wallet transfer" in w for w in out["warnings"])


def test_build_agent_instructions_warnings_match_rails():
    """Defaults adapt to which rails are present in how_to_pay."""
    x402_only = build_agent_instructions(BuildAgentInstructionsInput(how_to_pay={"x402_base": {}}))
    assert not any("tempo wallet transfer" in w for w in x402_only["warnings"])
    assert any("x402 deposit addresses" in w for w in x402_only["warnings"])
    assert not any("tempo request" in t for t in x402_only["recommended_tools"])
    assert any("agentscore-pay" in t for t in x402_only["recommended_tools"])

    tempo_only = build_agent_instructions(BuildAgentInstructionsInput(how_to_pay={"tempo": {}}))
    assert not any("x402 deposit addresses" in w for w in tempo_only["warnings"])

    stripe_only = build_agent_instructions(BuildAgentInstructionsInput(how_to_pay={"stripe": {}}))
    assert stripe_only["warnings"] == []
    assert stripe_only["recommended_tools"] == []


def test_build_402_body_assembles_full_response():
    body = build_402_body(
        Build402BodyInput(
            accepted_methods=[{"method": "tempo/charge"}],
            agent_instructions={"how_to_pay": {}},
            identity_metadata={"identity_mode": "wallet"},
            pricing=PricingBlock(subtotal="100", tax="8", tax_rate=0.08, tax_state="CA", total="108"),
            amount_usd="108",
            order_id="ord_1",
            x402=X402PaymentRequired(accepts=[{}]),
        )
    )
    assert body["payment_required"] is True
    assert body["x402Version"] == 2
    assert body["pricing"]["total"] == "108"
    assert body["identity_mode"] == "wallet"
    assert body["agent_instructions"]["how_to_pay"] == {}


def test_build_402_body_emits_v1_alias_on_accepts_entries():
    """Each accepts entry carries both `amount` (v2) and `maxAmountRequired` (v1)."""
    body = build_402_body(
        Build402BodyInput(
            accepted_methods=[],
            x402=X402PaymentRequired(
                accepts=[{"scheme": "exact", "network": "eip155:84532", "amount": "110000"}],
                version=2,
            ),
        )
    )
    entry = body["accepts"][0]
    assert entry["amount"] == "110000"
    assert entry["maxAmountRequired"] == "110000"
