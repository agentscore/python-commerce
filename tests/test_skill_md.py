"""Tests for the skill.md discovery builder."""

import re

import pytest

from agentscore_commerce.discovery import (
    BuildSkillMdInput,
    SkillMdEndpoint,
    SkillMdIdentityRequirements,
    SkillMdLink,
    SkillMdShippingPolicy,
    build_skill_md,
)


def _base() -> BuildSkillMdInput:
    return BuildSkillMdInput(
        name="martin-estate-wine-commerce",
        description="Buy wine from Martin Estate via an AI agent",
        homepage="https://martin-estate.com",
        merchant_name="Martin Estate",
        accepted_rails=["tempo_mpp", "x402_base", "x402_solana", "stripe"],
        endpoints=[
            SkillMdEndpoint(
                method="GET",
                path="/api/v1/wines",
                auth_required=False,
                description="Wine catalog",
            ),
            SkillMdEndpoint(
                method="POST",
                path="/api/v1/orders",
                auth_required=True,
                description="Place order",
            ),
        ],
        triggers=["User wants to buy wine from Martin Estate"],
    )


class TestFrontmatter:
    def test_emits_yaml_block_with_required_fields(self) -> None:
        out = build_skill_md(_base())
        assert out.startswith("---\n")
        assert re.search(
            r"^---\nname: martin-estate-wine-commerce\ndescription: .+\nhomepage: https://martin-estate\.com\nmetadata:\n {2}version: 1\n---",
            out,
        )

    def test_honors_version_override(self) -> None:
        cfg = _base()
        cfg.version = 7
        out = build_skill_md(cfg)
        assert "  version: 7" in out


class TestTitle:
    def test_renders_merchant_name_as_h1(self) -> None:
        out = build_skill_md(_base())
        assert "\n# Martin Estate\n" in out

    def test_renders_tagline_in_italics(self) -> None:
        cfg = _base()
        cfg.tagline = "A classic is forever"
        out = build_skill_md(cfg)
        assert "_A classic is forever_" in out

    def test_renders_intro_paragraph(self) -> None:
        cfg = _base()
        cfg.intro = "Napa Valley winery, family-run."
        out = build_skill_md(cfg)
        assert "Napa Valley winery, family-run." in out


class TestImportantFiles:
    def test_emits_self_reference(self) -> None:
        out = build_skill_md(_base())
        assert "## Important Files" in out
        assert "| **SKILL.md** (this file) | `https://martin-estate.com/skill.md` |" in out

    def test_appends_caller_supplied_files(self) -> None:
        cfg = _base()
        cfg.files = [
            SkillMdLink(label="llms.txt", url="https://martin-estate.com/llms.txt"),
            SkillMdLink(label="OpenAPI", url="https://martin-estate.com/openapi.json"),
        ]
        out = build_skill_md(cfg)
        assert "| llms.txt | `https://martin-estate.com/llms.txt` |" in out
        assert "| OpenAPI | `https://martin-estate.com/openapi.json` |" in out

    def test_strips_trailing_slash_from_homepage(self) -> None:
        cfg = _base()
        cfg.homepage = "https://martin-estate.com/"
        out = build_skill_md(cfg)
        assert "`https://martin-estate.com/skill.md`" in out
        assert "//skill.md" not in out


class TestPaymentSection:
    def test_renders_one_row_per_accepted_rail_with_smoke_verified_clients(self) -> None:
        out = build_skill_md(_base())
        assert "## Payment" in out
        assert "**MPP on Tempo**" in out
        assert "agentscore-pay, tempo request, x402-proxy" in out
        assert "**x402 on Base**" in out
        assert "agentscore-pay, x402-proxy, purl (omit --network flag)" in out
        assert "**x402 on Solana**" in out
        assert "**Stripe Shared Payment Token**" in out
        assert "link-cli" in out

    def test_omits_rails_not_declared(self) -> None:
        cfg = _base()
        cfg.accepted_rails = ["tempo_mpp", "x402_base", "x402_solana"]
        out = build_skill_md(cfg)
        assert "**MPP on Tempo**" in out
        assert "**Stripe Shared Payment Token**" not in out
        assert "link-cli" not in out

    def test_honors_compatible_clients_override(self) -> None:
        cfg = _base()
        cfg.accepted_rails = ["x402_base"]
        cfg.compatible_clients = {"x402_base": ["agentscore-pay", "merchant-custom-cli"]}
        out = build_skill_md(cfg)
        assert "agentscore-pay, merchant-custom-cli" in out
        assert "purl" not in out

    def test_renders_em_dash_when_client_list_explicitly_empty(self) -> None:
        cfg = _base()
        cfg.accepted_rails = ["x402_base"]
        cfg.compatible_clients = {"x402_base": []}
        out = build_skill_md(cfg)
        assert re.search(r"x402 on Base.+\| —", out)


class TestIdentitySection:
    def test_omits_when_identity_not_declared(self) -> None:
        out = build_skill_md(_base())
        assert "## Identity Prerequisite" not in out

    def test_renders_kyc_age_jurisdictions_sanctions(self) -> None:
        cfg = _base()
        cfg.identity = SkillMdIdentityRequirements(
            kyc_required=True,
            min_age=21,
            allowed_jurisdictions=["US"],
            sanctions_clear=True,
        )
        out = build_skill_md(cfg)
        assert "## Identity Prerequisite" in out
        assert "KYC verified Passport" in out
        assert "age 21+" in out
        assert "US only" in out
        assert "sanctions clear" in out

    def test_renders_bootstrap_pointer(self) -> None:
        cfg = _base()
        cfg.identity = SkillMdIdentityRequirements(kyc_required=True)
        cfg.identity_bootstrap_url = "https://agentscore.sh/skill.md"
        out = build_skill_md(cfg)
        assert "`https://agentscore.sh/skill.md`" in out
        assert "X-Operator-Token" in out

    def test_omits_when_every_flag_falsy(self) -> None:
        cfg = _base()
        cfg.identity = SkillMdIdentityRequirements(kyc_required=False, sanctions_clear=False)
        out = build_skill_md(cfg)
        assert "## Identity Prerequisite" not in out

    def test_does_not_leak_internal_posture(self) -> None:
        cfg = _base()
        cfg.identity = SkillMdIdentityRequirements(
            kyc_required=True,
            min_age=21,
            allowed_jurisdictions=["US"],
            sanctions_clear=True,
        )
        out = build_skill_md(cfg)
        for forbidden in [
            "fail_open",
            "fail-open",
            "failOpen",
            "gate-conditional",
            "gate-first",
            "Persona",
            "Stripe Identity",
        ]:
            assert forbidden not in out, f"leaked internal: {forbidden}"


class TestShippingSection:
    def test_omits_for_digital_merchants(self) -> None:
        out = build_skill_md(_base())
        assert "## Shipping" not in out

    def test_renders_allowed_countries_and_blocked_states(self) -> None:
        cfg = _base()
        cfg.shipping = SkillMdShippingPolicy(allowed_countries=["US"], blocked_states=["AK", "HI", "MS"])
        out = build_skill_md(cfg)
        assert "## Shipping" in out
        assert "Ships to: US." in out
        assert "Blocked US states: AK, HI, MS." in out

    def test_renders_only_populated_half(self) -> None:
        cfg = _base()
        cfg.shipping = SkillMdShippingPolicy(allowed_countries=["US"])
        out = build_skill_md(cfg)
        assert "Ships to: US." in out
        assert "Blocked US states" not in out

    def test_renders_blocked_only_with_no_allowed(self) -> None:
        cfg = _base()
        cfg.shipping = SkillMdShippingPolicy(blocked_states=["UT", "AK"])
        out = build_skill_md(cfg)
        assert "## Shipping" in out
        assert "Blocked US states: UT, AK." in out
        assert "Ships to:" not in out


class TestEndpointsSection:
    def test_emits_one_row_per_endpoint(self) -> None:
        out = build_skill_md(_base())
        assert "## Endpoints" in out
        assert "| GET | `/api/v1/wines` | anonymous | Wine catalog |" in out
        assert "| POST | `/api/v1/orders` | identity required | Place order |" in out

    def test_omits_section_when_endpoints_empty(self) -> None:
        cfg = _base()
        cfg.endpoints = []
        out = build_skill_md(cfg)
        assert "## Endpoints" not in out


class TestTriggersSection:
    def test_emits_each_trigger(self) -> None:
        cfg = _base()
        cfg.triggers = ["Buy wine from Martin Estate", "Check order status"]
        out = build_skill_md(cfg)
        assert "## Triggers" in out
        assert "- Buy wine from Martin Estate" in out
        assert "- Check order status" in out

    def test_omits_when_empty(self) -> None:
        cfg = _base()
        cfg.triggers = []
        out = build_skill_md(cfg)
        assert "## Triggers" not in out


class TestOnboardingAndSupport:
    def test_emits_numbered_onboarding(self) -> None:
        cfg = _base()
        cfg.onboarding_steps = ["Install agentscore-pay", "Get a Passport", "Pay any 402"]
        out = build_skill_md(cfg)
        assert "## Onboarding Flow" in out
        assert "1. Install agentscore-pay" in out
        assert "2. Get a Passport" in out
        assert "3. Pay any 402" in out

    def test_emits_support_links(self) -> None:
        cfg = _base()
        cfg.support_links = [
            SkillMdLink(label="Homepage", url="https://martin-estate.com"),
            SkillMdLink(label="Pay CLI", url="https://github.com/agentscore/pay"),
        ]
        out = build_skill_md(cfg)
        assert "## Support" in out
        assert "- **Homepage**: https://martin-estate.com" in out
        assert "- **Pay CLI**: https://github.com/agentscore/pay" in out


class TestRefreshFooter:
    def test_appends_by_default(self) -> None:
        out = build_skill_md(_base())
        assert "Re-fetch this file" in out

    def test_suppresses_when_disabled(self) -> None:
        cfg = _base()
        cfg.refresh_footer = False
        out = build_skill_md(cfg)
        assert "Re-fetch this file" not in out


class TestOutputHygiene:
    def test_ends_with_single_trailing_newline(self) -> None:
        out = build_skill_md(_base())
        assert out.endswith("\n")
        assert not out.endswith("\n\n")

    def test_no_triple_newline_runs(self) -> None:
        out = build_skill_md(_base())
        assert "\n\n\n" not in out


@pytest.mark.parametrize(
    "rail,expected_label",
    [
        ("tempo_mpp", "MPP on Tempo"),
        ("x402_base", "x402 on Base"),
        ("x402_solana", "x402 on Solana"),
        ("stripe", "Stripe Shared Payment Token"),
    ],
)
def test_each_rail_label(rail: str, expected_label: str) -> None:
    cfg = _base()
    cfg.accepted_rails = [rail]
    out = build_skill_md(cfg)
    assert f"**{expected_label}**" in out
