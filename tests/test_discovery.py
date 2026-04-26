import json

from agentscore_commerce.discovery import (
    BuildAgentScoreOpenApiSnippetsInput,
    DiscoveryProbeOptions,
    LlmsTxtIdentitySectionInput,
    LlmsTxtPaymentSectionInput,
    LlmsTxtSection,
    PaymentMethodConfig,
    WellKnownMppInput,
    agentscore_openapi_snippets,
    build_discovery_probe_response,
    build_llms_txt,
    build_well_known_mpp,
    is_discovery_probe_request,
    llms_txt_payment_section,
)


def test_build_discovery_probe_response_returns_402_with_directive():
    resp = build_discovery_probe_response(
        DiscoveryProbeOptions(
            realm="ex.com", sample_rail="tempo-mainnet", sample_amount_usd=1.0, sample_recipient="0xabc"
        )
    )
    assert resp.status == 402
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["www-authenticate"].startswith("Payment ")
    body = json.loads(resp.body)
    assert body["error"]["code"] == "payment_required"
    assert body["discovery"] is True


async def test_is_discovery_probe_request_true_for_empty_post():
    assert await is_discovery_probe_request("POST", None, "") is True
    assert await is_discovery_probe_request("POST", None, "{}") is True


async def test_is_discovery_probe_request_false_for_get_or_with_payment_or_with_body():
    assert await is_discovery_probe_request("GET", None, "") is False
    assert await is_discovery_probe_request("POST", "Payment xxx", "") is False
    assert await is_discovery_probe_request("POST", None, '{"a":1}') is False


def test_build_well_known_mpp_assembles_purchase_block():
    out = build_well_known_mpp(
        WellKnownMppInput(
            name="Ex",
            url="https://ex.com",
            endpoints={"buy": {"method": "POST", "url": "/buy"}},
            purchase=PaymentMethodConfig(methods=["tempo", "x402"], identity=["X-Operator-Token"]),
        )
    )
    assert out["name"] == "Ex"
    assert out["purchase"]["payment_methods"] == ["tempo", "x402"]
    assert out["purchase"]["identity"] == ["X-Operator-Token"]


def test_build_well_known_mpp_passes_through_extras():
    out = build_well_known_mpp(
        WellKnownMppInput(
            name="Ex",
            url="https://ex.com",
            endpoints={},
            purchase=PaymentMethodConfig(methods=["tempo"], extra={"gift_note": {"max_length": 200}}),
            extra={"version": "1.0"},
        )
    )
    assert out["purchase"]["gift_note"]["max_length"] == 200
    assert out["version"] == "1.0"


def test_build_llms_txt_assembles_full_document():
    doc = build_llms_txt(
        type(
            "I",
            (),
            {
                "merchant_name": "Ex",
                "tagline": "Wines",
                "sections": [LlmsTxtSection("About", "We sell wine.")],
                "agentscore_identity": LlmsTxtIdentitySectionInput(agentscore=True),
                "payment": LlmsTxtPaymentSectionInput(rails=["tempo-mainnet"], app_url="https://ex.com"),
            },
        )()
    )
    assert "# Ex" in doc
    assert "## About" in doc
    assert "Choose your identity header" in doc
    assert "Tempo USDC via MPP" in doc


def test_agentscore_openapi_snippets_includes_security_and_schemas():
    snip = agentscore_openapi_snippets()
    assert "OperatorToken" in snip["securitySchemes"]
    assert "AgentScoreDenialReason" in snip["schemas"]
    assert "AgentScorePaymentRequired" in snip["schemas"]


def test_agentscore_openapi_snippets_can_disable_sections():
    snip = agentscore_openapi_snippets(BuildAgentScoreOpenApiSnippetsInput(security=False, payment_required=False))
    assert "securitySchemes" not in snip
    assert "AgentScoreDenialReason" in snip["schemas"]


class TestLlmsTxtPaymentSectionVerbose:
    def test_emits_multi_step_setup_per_rail(self):
        section = llms_txt_payment_section(
            LlmsTxtPaymentSectionInput(
                rails=["tempo-mainnet", "x402-base-mainnet"],
                app_url="https://my.merchant",
                verbose=True,
                tempo_network_name="tempo-mainnet",
                tempo_chain_id=4217,
            )
        )
        assert "### How to pay with Tempo" in section
        assert "curl -fsSL https://tempo.xyz/install" in section
        assert "tempo wallet login" in section
        assert "USDC.e on tempo-mainnet, chain 4217" in section
        assert "tempo request -X POST" in section
        assert "### How to pay with x402" in section
        assert "npm install -g @agent-score/pay" in section
        assert "agentscore-pay wallet create" in section
        assert "https://my.merchant" in section

    def test_omits_sections_for_unconfigured_rails(self):
        section = llms_txt_payment_section(
            LlmsTxtPaymentSectionInput(rails=["tempo-mainnet"], app_url="https://x", verbose=True)
        )
        assert "Tempo USDC" in section
        assert "### How to pay with x402" not in section
        assert "### How to pay with Stripe" not in section

    def test_emits_exact_amount_warning_for_x402(self):
        section = llms_txt_payment_section(
            LlmsTxtPaymentSectionInput(rails=["x402-base-mainnet"], app_url="https://x", verbose=True)
        )
        assert "exact amount specified in the 402 challenge" in section

    def test_emits_stripe_section(self):
        section = llms_txt_payment_section(
            LlmsTxtPaymentSectionInput(rails=["stripe-spt"], app_url="https://x", verbose=True)
        )
        assert "### How to pay with Stripe SPT" in section
        assert "SharedPaymentToken" in section

    def test_solana_only_no_base(self):
        section = llms_txt_payment_section(
            LlmsTxtPaymentSectionInput(rails=["x402-solana-mainnet"], app_url="https://x", verbose=True)
        )
        assert "### How to pay with x402 (Solana)" in section
        assert "--chain solana" in section
        assert "--chain base" not in section
