import json

import pytest

from agentscore_commerce.discovery import (
    BuildAgentScoreOpenApiSnippetsInput,
    BuildWellKnownX402Input,
    DiscoveryProbeOptions,
    LlmsTxtIdentitySectionInput,
    LlmsTxtPaymentSectionInput,
    LlmsTxtSection,
    PaymentMethodConfig,
    WellKnownMppInput,
    WellKnownX402Resource,
    XPaymentInfoDynamicPrice,
    XPaymentInfoFixedPrice,
    XPaymentInfoInput,
    agentscore_openapi_snippets,
    agentscore_security_schemes,
    build_discovery_probe_response,
    build_llms_txt,
    build_well_known_mpp,
    build_well_known_x402,
    is_discovery_probe_request,
    llms_txt_payment_section,
    siwx_security_scheme,
    x_guidance_extension,
    x_payment_info_extension,
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
        assert "agentscore-pay pay POST https://my.merchant" in section

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
            LlmsTxtPaymentSectionInput(rails=["mpp-solana-mainnet"], app_url="https://x", verbose=True)
        )
        assert "### How to pay with x402 (Solana)" in section
        assert "--chain solana" in section
        assert "--chain base" not in section


# ── sample_x402_accept_for_network: every registry branch ───────────────────


def test_sample_accept_base_mainnet() -> None:
    from agentscore_commerce.discovery.probe import sample_x402_accept_for_network

    e = sample_x402_accept_for_network("eip155:8453")
    assert e is not None
    assert e["network"] == "eip155:8453"
    assert e["scheme"] == "exact"
    assert e["extra"] == {"name": "USDC", "version": "2"}


def test_sample_accept_base_sepolia() -> None:
    from agentscore_commerce.discovery.probe import sample_x402_accept_for_network

    e = sample_x402_accept_for_network("eip155:84532")
    assert e is not None
    assert e["network"] == "eip155:84532"


def test_sample_accept_solana_mainnet() -> None:
    from agentscore_commerce.discovery.probe import sample_x402_accept_for_network

    e = sample_x402_accept_for_network("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
    assert e is not None
    assert "extra" not in e
    assert e["payTo"] == "11111111111111111111111111111111"


def test_sample_accept_solana_devnet() -> None:
    from agentscore_commerce.discovery.probe import sample_x402_accept_for_network

    e = sample_x402_accept_for_network("solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1")
    assert e is not None
    assert e["network"].startswith("solana:")


def test_sample_accept_unknown_network_returns_none() -> None:
    from agentscore_commerce.discovery.probe import sample_x402_accept_for_network

    assert sample_x402_accept_for_network("eip155:1") is None


# ── build_discovery_probe_response: x402 sample paths ───────────────────────


def _probe_opts(**overrides: object):
    from agentscore_commerce.discovery.probe import DiscoveryProbeOptions

    base: dict[str, object] = {
        "realm": "https://example.com",
        "sample_rail": "tempo-mainnet",
        "sample_amount_usd": 1.00,
        "sample_recipient": "0x0000000000000000000000000000000000000001",
    }
    base.update(overrides)
    return DiscoveryProbeOptions(**base)  # type: ignore[arg-type]


def test_probe_response_without_x402_sample() -> None:
    from agentscore_commerce.discovery.probe import build_discovery_probe_response

    resp = build_discovery_probe_response(_probe_opts())
    assert resp.status == 402
    assert "www-authenticate" in resp.headers
    assert "payment-required" not in resp.headers


def test_probe_response_with_x402_sample_via_networks_shorthand() -> None:
    import json as _json

    from agentscore_commerce.discovery.probe import X402SampleProbe, build_discovery_probe_response

    resp = build_discovery_probe_response(
        _probe_opts(x402_sample=X402SampleProbe(networks=["eip155:84532", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"]))
    )
    assert resp.status == 402
    assert "payment-required" in resp.headers
    body = _json.loads(resp.body)
    assert body["x402Version"] == 2
    assert len(body["accepts"]) == 2


def test_probe_response_with_explicit_accepts_overrides_networks_shorthand() -> None:
    import json as _json

    from agentscore_commerce.discovery.probe import X402SampleProbe, build_discovery_probe_response

    custom = [{"scheme": "exact", "network": "fake", "asset": "X", "payTo": "Y"}]
    resp = build_discovery_probe_response(_probe_opts(x402_sample=X402SampleProbe(accepts=custom, version=1)))
    body = _json.loads(resp.body)
    assert body["x402Version"] == 1
    assert body["accepts"][0]["network"] == "fake"


def test_probe_response_with_resource_url() -> None:
    import base64
    import json as _json

    from agentscore_commerce.discovery.probe import X402SampleProbe, build_discovery_probe_response

    resp = build_discovery_probe_response(
        _probe_opts(x402_sample=X402SampleProbe(networks=["eip155:84532"], resource_url="https://example.com/api"))
    )
    decoded = _json.loads(base64.b64decode(resp.headers["payment-required"]).decode())
    assert decoded["resource"]["url"] == "https://example.com/api"


def test_probe_response_with_docs_url() -> None:
    import json as _json

    from agentscore_commerce.discovery.probe import build_discovery_probe_response

    resp = build_discovery_probe_response(_probe_opts(docs_url="https://docs.example.com"))
    body = _json.loads(resp.body)
    assert body["docs"] == "https://docs.example.com"


def test_probe_response_unknown_network_filtered_out() -> None:
    import json as _json

    from agentscore_commerce.discovery.probe import X402SampleProbe, build_discovery_probe_response

    resp = build_discovery_probe_response(
        _probe_opts(x402_sample=X402SampleProbe(networks=["eip155:84532", "eip155:99999"]))
    )
    body = _json.loads(resp.body)
    assert len(body["accepts"]) == 1


# ── is_discovery_probe_request ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_probe_empty_post() -> None:
    from agentscore_commerce.discovery.probe import is_discovery_probe_request

    assert await is_discovery_probe_request("POST", None, "") is True


@pytest.mark.asyncio
async def test_is_probe_empty_object_post() -> None:
    from agentscore_commerce.discovery.probe import is_discovery_probe_request

    assert await is_discovery_probe_request("POST", None, "{}") is True


@pytest.mark.asyncio
async def test_is_probe_non_post_rejected() -> None:
    from agentscore_commerce.discovery.probe import is_discovery_probe_request

    assert await is_discovery_probe_request("GET", None, "") is False


@pytest.mark.asyncio
async def test_is_probe_with_payment_authz_rejected() -> None:
    from agentscore_commerce.discovery.probe import is_discovery_probe_request

    assert await is_discovery_probe_request("POST", "Payment foo", "") is False


@pytest.mark.asyncio
async def test_is_probe_with_real_body_rejected() -> None:
    from agentscore_commerce.discovery.probe import is_discovery_probe_request

    assert await is_discovery_probe_request("POST", None, '{"product": "x"}') is False


def test_build_well_known_x402_emits_v1_shape():
    doc = build_well_known_x402(
        BuildWellKnownX402Input(
            resources=[
                WellKnownX402Resource(method="POST", path="/purchase"),
                WellKnownX402Resource(method="GET", path="/catalog"),
            ]
        )
    )
    assert doc == {"version": 1, "resources": ["POST /purchase", "GET /catalog"]}


def test_build_well_known_x402_uppercases_methods():
    doc = build_well_known_x402(BuildWellKnownX402Input(resources=[WellKnownX402Resource(method="post", path="/x")]))
    assert doc["resources"] == ["POST /x"]


def test_build_well_known_x402_empty_resources():
    assert build_well_known_x402(BuildWellKnownX402Input(resources=[])) == {"version": 1, "resources": []}


def test_siwx_security_scheme_is_http_bearer_siwx():
    scheme = siwx_security_scheme()
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "SIWX"


def test_agentscore_security_schemes_includes_siwx():
    schemes = agentscore_security_schemes()
    assert "siwx" in schemes
    assert schemes["siwx"]["bearerFormat"] == "SIWX"


def test_x_payment_info_extension_fixed_price():
    ext = x_payment_info_extension(
        XPaymentInfoInput(
            price=XPaymentInfoFixedPrice(currency="USD", amount="0.10"),
            protocols=[{"x402": {}}],
        )
    )
    assert ext["x-payment-info"]["price"] == {"mode": "fixed", "currency": "USD", "amount": "0.10"}
    assert ext["x-payment-info"]["protocols"] == [{"x402": {}}]


def test_x_payment_info_extension_dynamic_price_with_mpp():
    ext = x_payment_info_extension(
        XPaymentInfoInput(
            price=XPaymentInfoDynamicPrice(currency="USD", min="0.01", max="5.00"),
            protocols=[
                {"x402": {}},
                {"mpp": {"method": "tempo/charge", "intent": "pay", "currency": "USD"}},
            ],
        )
    )
    assert ext["x-payment-info"]["price"]["mode"] == "dynamic"
    assert ext["x-payment-info"]["price"]["min"] == "0.01"
    assert len(ext["x-payment-info"]["protocols"]) == 2


def test_x_guidance_extension_wraps_text():
    assert x_guidance_extension("Use POST /purchase with operator token") == {
        "x-guidance": "Use POST /purchase with operator token"
    }
