"""Tests for the skill.md discovery builder (agentskills.io spec compliance)."""

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
        name="example-merchant-commerce",
        description="Buy from Example Merchant via an AI agent",
        homepage="https://example.com",
        merchant_name="Example Merchant",
        accepted_rails=["tempo_mpp", "x402_base", "solana_mpp", "stripe"],
        endpoints=[
            SkillMdEndpoint(method="GET", path="/api/v1/wines", auth_required=False, description="Wine catalog"),
            SkillMdEndpoint(method="POST", path="/api/v1/orders", auth_required=True, description="Place order"),
        ],
        triggers=["User wants to buy from Example Merchant"],
    )


class TestFrontmatter:
    def test_emits_yaml_block_with_required_fields(self) -> None:
        out = build_skill_md(_base())
        assert out.startswith("---\n")
        assert "name: example-merchant-commerce" in out
        assert 'description: "Buy from Example Merchant via an AI agent"' in out
        assert "metadata:" in out
        assert '  version: "1"' in out
        assert '  homepage: "https://example.com"' in out

    def test_version_emitted_as_quoted_string(self) -> None:
        cfg = _base()
        cfg.version = 7
        out = build_skill_md(cfg)
        assert '  version: "7"' in out
        cfg.version = "2.0.1"
        out2 = build_skill_md(cfg)
        assert '  version: "2.0.1"' in out2

    def test_version_zero_passes_through(self) -> None:
        """Parity lock: Node uses ?? (nullish coalescing); Python uses str(); both pass 0 through."""
        cfg = _base()
        cfg.version = 0
        out = build_skill_md(cfg)
        assert '  version: "0"' in out

    def test_quotes_description_with_colons(self) -> None:
        cfg = _base()
        cfg.description = "Use when: buying premium wine"
        out = build_skill_md(cfg)
        assert 'description: "Use when: buying premium wine"' in out

    def test_escapes_double_quotes_in_description(self) -> None:
        cfg = _base()
        cfg.description = 'Buy "Estate" wine'
        out = build_skill_md(cfg)
        assert 'description: "Buy \\"Estate\\" wine"' in out

    def test_escapes_newlines_in_description(self) -> None:
        cfg = _base()
        cfg.description = "line one\nline two"
        out = build_skill_md(cfg)
        assert 'description: "line one\\nline two"' in out

    def test_emits_optional_license_compatibility_allowed_tools(self) -> None:
        cfg = _base()
        cfg.license = "Apache-2.0"
        cfg.compatibility = "Requires Python 3.11+"
        cfg.allowed_tools = "Bash(curl:*)"
        out = build_skill_md(cfg)
        assert 'license: "Apache-2.0"' in out
        assert 'compatibility: "Requires Python 3.11+"' in out
        assert 'allowed-tools: "Bash(curl:*)"' in out

    def test_omits_optional_fields_by_default(self) -> None:
        out = build_skill_md(_base())
        assert not re.search(r"^license:", out, re.MULTILINE)
        assert not re.search(r"^compatibility:", out, re.MULTILINE)
        assert not re.search(r"^allowed-tools:", out, re.MULTILINE)

    def test_metadata_extras_with_protected_keys(self) -> None:
        cfg = _base()
        cfg.metadata = {"author": "agentscore", "vendor_id": "me-001", "version": "IGNORED", "homepage": "IGNORED"}
        out = build_skill_md(cfg)
        assert '  author: "agentscore"' in out
        assert '  vendor_id: "me-001"' in out
        assert '  version: "1"' in out
        assert '  homepage: "https://example.com"' in out
        assert "IGNORED" not in out


class TestValidation:
    def test_rejects_empty_name(self) -> None:
        cfg = _base()
        cfg.name = ""
        with pytest.raises(ValueError, match=r"1-64"):
            build_skill_md(cfg)

    def test_rejects_name_over_64_chars(self) -> None:
        cfg = _base()
        cfg.name = "a" * 65
        with pytest.raises(ValueError, match=r"1-64"):
            build_skill_md(cfg)

    def test_rejects_uppercase_name(self) -> None:
        cfg = _base()
        cfg.name = "Example-Merchant"
        with pytest.raises(ValueError, match=r"lowercase"):
            build_skill_md(cfg)

    def test_rejects_leading_hyphen(self) -> None:
        cfg = _base()
        cfg.name = "-foo"
        with pytest.raises(ValueError, match=r"hyphens"):
            build_skill_md(cfg)

    def test_rejects_trailing_hyphen(self) -> None:
        cfg = _base()
        cfg.name = "foo-"
        with pytest.raises(ValueError, match=r"hyphens"):
            build_skill_md(cfg)

    def test_rejects_consecutive_hyphens(self) -> None:
        cfg = _base()
        cfg.name = "foo--bar"
        with pytest.raises(ValueError, match=r"hyphens"):
            build_skill_md(cfg)

    def test_rejects_empty_description(self) -> None:
        cfg = _base()
        cfg.description = ""
        with pytest.raises(ValueError, match=r"non-empty"):
            build_skill_md(cfg)

    def test_rejects_description_over_1024_chars(self) -> None:
        cfg = _base()
        cfg.description = "a" * 1025
        with pytest.raises(ValueError, match=r"1024"):
            build_skill_md(cfg)

    def test_rejects_compatibility_over_500_chars(self) -> None:
        cfg = _base()
        cfg.compatibility = "a" * 501
        with pytest.raises(ValueError, match=r"500"):
            build_skill_md(cfg)


class TestTitleBlock:
    def test_renders_merchant_name_as_h1(self) -> None:
        out = build_skill_md(_base())
        assert "\n# Example Merchant\n" in out

    def test_renders_title_tagline_intro_with_blank_lines(self) -> None:
        cfg = _base()
        cfg.tagline = "A classic is forever"
        cfg.intro = "Napa Valley winery, family-run."
        out = build_skill_md(cfg)
        assert "# Example Merchant\n\n_A classic is forever_\n\nNapa Valley winery, family-run." in out

    def test_renders_tagline_only(self) -> None:
        cfg = _base()
        cfg.tagline = "A classic is forever"
        out = build_skill_md(cfg)
        assert "# Example Merchant\n\n_A classic is forever_" in out

    def test_renders_intro_only(self) -> None:
        cfg = _base()
        cfg.intro = "Napa Valley winery."
        out = build_skill_md(cfg)
        assert "# Example Merchant\n\nNapa Valley winery." in out


class TestImportantFiles:
    def test_emits_self_reference(self) -> None:
        out = build_skill_md(_base())
        assert "## Important Files" in out
        assert "| **SKILL.md** (this file) | `https://example.com/skill.md` |" in out

    def test_appends_caller_files(self) -> None:
        cfg = _base()
        cfg.files = [
            SkillMdLink(label="llms.txt", url="https://example.com/llms.txt"),
            SkillMdLink(label="OpenAPI", url="https://example.com/openapi.json"),
        ]
        out = build_skill_md(cfg)
        assert "| llms.txt | `https://example.com/llms.txt` |" in out
        assert "| OpenAPI | `https://example.com/openapi.json` |" in out

    def test_strips_trailing_slash_from_homepage(self) -> None:
        cfg = _base()
        cfg.homepage = "https://example.com/"
        out = build_skill_md(cfg)
        assert "`https://example.com/skill.md`" in out
        assert "//skill.md" not in out

    def test_escapes_pipes_in_files(self) -> None:
        cfg = _base()
        cfg.files = [SkillMdLink(label="a|b", url="https://x.example/foo|bar")]
        out = build_skill_md(cfg)
        assert "| a\\|b | `https://x.example/foo\\|bar` |" in out

    def test_escapes_backslashes_before_pipes(self) -> None:
        """Backslashes must escape first, otherwise existing `\\` consumes the pipe escape."""
        cfg = _base()
        cfg.files = [SkillMdLink(label="a\\|b", url="https://x.example/c\\d")]
        out = build_skill_md(cfg)
        # Backslash → `\\`, then pipe → `\|`. Combined for `a\|b`: `a\\\|b`.
        assert "| a\\\\\\|b | `https://x.example/c\\\\d` |" in out


class TestPaymentSection:
    def test_renders_one_row_per_rail(self) -> None:
        out = build_skill_md(_base())
        assert "## Payment" in out
        assert "**MPP on Tempo**" in out
        assert "agentscore-pay, tempo request, x402-proxy" in out
        assert "**x402 on Base**" in out
        assert "agentscore-pay, x402-proxy, purl (omit --network flag)" in out
        assert "**MPP on Solana**" in out
        assert "**Stripe Shared Payment Token**" in out
        assert "link-cli" in out

    def test_omits_unaccepted_rails(self) -> None:
        cfg = _base()
        cfg.accepted_rails = ["tempo_mpp", "x402_base", "solana_mpp"]
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

    def test_drops_overrides_for_rails_not_in_accepted(self) -> None:
        cfg = _base()
        cfg.accepted_rails = ["x402_base"]
        cfg.compatible_clients = {"x402_base": ["agentscore-pay"], "stripe": ["rogue-cli"]}
        out = build_skill_md(cfg)
        assert "rogue-cli" not in out
        assert "Stripe Shared Payment Token" not in out

    def test_renders_em_dash_when_clients_empty(self) -> None:
        cfg = _base()
        cfg.accepted_rails = ["x402_base"]
        cfg.compatible_clients = {"x402_base": []}
        out = build_skill_md(cfg)
        assert re.search(r"x402 on Base.+\| —", out)


class TestIdentitySection:
    def test_omits_when_not_declared(self) -> None:
        out = build_skill_md(_base())
        assert "## Identity Prerequisite" not in out

    def test_renders_kyc_age_jurisdictions_sanctions(self) -> None:
        cfg = _base()
        cfg.identity = SkillMdIdentityRequirements(
            kyc_required=True, min_age=21, allowed_jurisdictions=["US"], sanctions_clear=True
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
        cfg.identity_bootstrap_url = "https://identity.example.com/skill.md"
        out = build_skill_md(cfg)
        assert "`https://identity.example.com/skill.md`" in out
        assert "X-Operator-Token" in out

    def test_omits_when_all_flags_falsy(self) -> None:
        cfg = _base()
        cfg.identity = SkillMdIdentityRequirements(kyc_required=False, sanctions_clear=False)
        out = build_skill_md(cfg)
        assert "## Identity Prerequisite" not in out

    def test_no_internal_posture_leak(self) -> None:
        cfg = _base()
        cfg.identity = SkillMdIdentityRequirements(
            kyc_required=True, min_age=21, allowed_jurisdictions=["US"], sanctions_clear=True
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
            assert forbidden not in out, f"leaked: {forbidden}"


class TestShippingSection:
    def test_omits_when_no_shipping(self) -> None:
        out = build_skill_md(_base())
        assert "## Shipping" not in out

    def test_renders_both_halves(self) -> None:
        cfg = _base()
        cfg.shipping = SkillMdShippingPolicy(allowed_countries=["US"], blocked_states=["AK", "HI", "MS"])
        out = build_skill_md(cfg)
        assert "## Shipping" in out
        assert "Ships to: US." in out
        assert "Blocked US states: AK, HI, MS." in out

    def test_renders_only_allowed(self) -> None:
        cfg = _base()
        cfg.shipping = SkillMdShippingPolicy(allowed_countries=["US"])
        out = build_skill_md(cfg)
        assert "Ships to: US." in out
        assert "Blocked US states" not in out

    def test_renders_only_blocked(self) -> None:
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

    def test_omits_when_empty(self) -> None:
        cfg = _base()
        cfg.endpoints = []
        out = build_skill_md(cfg)
        assert "## Endpoints" not in out

    def test_escapes_pipes_in_endpoints(self) -> None:
        cfg = _base()
        cfg.endpoints = [SkillMdEndpoint(method="GET", path="/foo|bar", auth_required=False, description="a|b")]
        out = build_skill_md(cfg)
        assert "| GET | `/foo\\|bar` | anonymous | a\\|b |" in out


class TestTriggersSection:
    def test_emits_each_trigger(self) -> None:
        cfg = _base()
        cfg.triggers = ["Buy from Example Merchant", "Check order status"]
        out = build_skill_md(cfg)
        assert "## Triggers" in out
        assert "- Buy from Example Merchant" in out
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
            SkillMdLink(label="Homepage", url="https://example.com"),
            SkillMdLink(label="Pay CLI", url="https://github.com/agentscore/pay"),
        ]
        out = build_skill_md(cfg)
        assert "## Support" in out
        assert "- **Homepage**: https://example.com" in out
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
        ("solana_mpp", "MPP on Solana"),
        ("stripe", "Stripe Shared Payment Token"),
    ],
)
def test_each_rail_label(rail: str, expected_label: str) -> None:
    cfg = _base()
    cfg.accepted_rails = [rail]
    out = build_skill_md(cfg)
    assert f"**{expected_label}**" in out
