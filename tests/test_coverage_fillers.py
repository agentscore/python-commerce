"""Targeted tests covering optional-field branches across discovery + challenge builders."""

from agentscore_commerce.challenge import (
    Build402BodyInput,
    BuildAcceptedMethodsInput,
    BuildAgentInstructionsInput,
    BuildHowToPayInput,
    HowToPayRails,
    StripeRailConfig,
    TempoRailConfig,
    X402SolanaConfig,
    X402SolanaRailConfig,
    build_402_body,
    build_accepted_methods,
    build_agent_instructions,
    build_how_to_pay,
)
from agentscore_commerce.discovery import (
    BazaarDiscoveryConfig,
    LlmsTxtIdentitySectionInput,
    LlmsTxtPaymentSectionInput,
    PaymentMethodConfig,
    WellKnownMppInput,
    build_bazaar_discovery_payload,
    build_well_known_mpp,
    llms_txt_identity_section,
    llms_txt_payment_section,
)


def test_bazaar_payload_includes_all_optional_fields():
    payload = build_bazaar_discovery_payload(
        BazaarDiscoveryConfig(
            body_type="json",
            input={"q": "string"},
            output={"results": "array"},
            extra={"version": "1.0"},
        )
    )
    assert payload == {
        "bodyType": "json",
        "input": {"q": "string"},
        "output": {"results": "array"},
        "version": "1.0",
    }


def test_bazaar_payload_omits_empty_fields():
    payload = build_bazaar_discovery_payload(BazaarDiscoveryConfig())
    assert payload == {}


def test_well_known_mpp_includes_all_optional_blocks():
    out = build_well_known_mpp(
        WellKnownMppInput(
            name="Ex",
            description="A merchant",
            url="https://ex.com",
            openapi="https://ex.com/openapi.json",
            endpoints={"buy": {"method": "POST", "url": "/buy"}},
            catalog={"categories": ["wine"]},
            purchase=PaymentMethodConfig(
                methods=["tempo"],
                required_fields=["product_id", "qty"],
                optional_fields=["gift_note"],
                x402={"networks": ["base"]},
                compliance={"require_kyc": True},
            ),
            shipping={"countries": ["US"]},
        )
    )
    assert out["description"] == "A merchant"
    assert out["openapi"].endswith("openapi.json")
    assert out["catalog"] == {"categories": ["wine"]}
    assert out["purchase"]["required_fields"] == ["product_id", "qty"]
    assert out["purchase"]["optional_fields"] == ["gift_note"]
    assert out["purchase"]["x402"] == {"networks": ["base"]}
    assert out["purchase"]["compliance"] == {"require_kyc": True}
    assert out["shipping"] == {"countries": ["US"]}


def test_llms_txt_identity_section_returns_empty_when_agentscore_false():
    assert llms_txt_identity_section(LlmsTxtIdentitySectionInput(agentscore=False)) == ""


def test_llms_txt_identity_section_includes_compliance_note():
    section = llms_txt_identity_section(
        LlmsTxtIdentitySectionInput(
            agentscore=True,
            compliance={
                "require_kyc": True,
                "min_age": 21,
                "allowed_jurisdictions": ["US", "CA"],
                "require_sanctions_clear": True,
            },
        )
    )
    assert "Compliance:" in section
    assert "KYC required" in section
    assert "age 21+" in section
    assert "US/CA only" in section
    assert "sanctions clear" in section


def test_llms_txt_payment_section_includes_all_rails():
    section = llms_txt_payment_section(
        LlmsTxtPaymentSectionInput(
            rails=["tempo-mainnet", "x402-base-mainnet", "mpp-solana-mainnet", "stripe-spt"],
            app_url="https://ex.com/buy",
        )
    )
    assert "Tempo USDC via MPP" in section
    assert "x402 USDC on Base" in section
    assert "x402 USDC on Solana" in section
    assert "Stripe Shared Payment Token" in section


def test_build_accepted_methods_includes_solana_only():
    out = build_accepted_methods(BuildAcceptedMethodsInput(solana_mpp=X402SolanaConfig(recipient="solanaaddr")))
    assert out[0]["network"].startswith("solana:")
    assert out[0]["pay_to"] == "solanaaddr"


def test_build_how_to_pay_solana_only():
    out = build_how_to_pay(
        BuildHowToPayInput(
            url="https://ex.com",
            retry_body_json="{}",
            total_usd=5.0,
            rails=HowToPayRails(solana_mpp=X402SolanaRailConfig(recipient="solanaaddr")),
        )
    )
    assert "solana_mpp" in out
    assert "agentscore-pay pay POST" in out["solana_mpp"]["command"]


def test_build_how_to_pay_tempo_recommend_pay():
    out = build_how_to_pay(
        BuildHowToPayInput(
            url="https://ex.com",
            retry_body_json="{}",
            total_usd=5.0,
            rails=HowToPayRails(tempo=TempoRailConfig(recipient="0xT", recommend="agentscore-pay")),
        )
    )
    assert out["tempo"]["command"].startswith("agentscore-pay pay POST")
    assert out["tempo"]["alternative_command"].startswith("tempo request")


def test_build_how_to_pay_tempo_recommend_tempo_only():
    out = build_how_to_pay(
        BuildHowToPayInput(
            url="https://ex.com",
            retry_body_json="{}",
            total_usd=5.0,
            rails=HowToPayRails(tempo=TempoRailConfig(recipient="0xT", recommend="tempo")),
        )
    )
    assert "alternative_command" not in out["tempo"]


def test_build_how_to_pay_stripe_no_profile_id_skips_link_cli():
    out = build_how_to_pay(
        BuildHowToPayInput(
            url="https://ex.com",
            retry_body_json="{}",
            total_usd=5.0,
            rails=HowToPayRails(stripe=StripeRailConfig(profile_id=None)),
        )
    )
    assert "setup_link_cli" not in out["stripe"]
    assert "note" not in out["stripe"]


def test_build_agent_instructions_with_recommended_and_extra():
    out = build_agent_instructions(
        BuildAgentInstructionsInput(
            how_to_pay={"tempo": {}},
            recommended="tempo",
            extra={"vendor_field": "value"},
        )
    )
    assert out["recommended"] == "tempo"
    assert out["vendor_field"] == "value"


def test_build_402_body_includes_all_optional_blocks():
    body = build_402_body(
        Build402BodyInput(
            accepted_methods=[],
            agent_memory={"pattern": "agentscore-shared-identity"},
            currency="USD",
            product={"id": "p_1", "name": "Wine"},
            recommended="tempo",
            retry_body={"product_id": "p_1"},
            extra={"vendor_field": "value"},
        )
    )
    assert body["agent_memory"] == {"pattern": "agentscore-shared-identity"}
    assert body["currency"] == "USD"
    assert body["product"]["name"] == "Wine"
    assert body["recommended"] == "tempo"
    assert body["retry_body"]["product_id"] == "p_1"
    assert body["vendor_field"] == "value"
