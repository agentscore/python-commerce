"""AIP presentation surfaces.

Ports node-commerce ``tests/aip_presentation.test.ts``. AgentScore's own issuer is always
trusted, so the AIP path is advertised even with no external issuers configured.

The discovery/agent_memory presentation hooks (``llms_txt``, the OpenAPI ``AgentIdentity``
security scheme, ``skill.md``, and ``first_encounter_agent_memory``'s ``aip_trusted_issuers``
forwarding) are ported here and mirror node's expectations exactly.
"""

from __future__ import annotations

from agentscore_commerce.aip import AGENTSCORE_CANONICAL_ISSUER
from agentscore_commerce.challenge import first_encounter_agent_memory
from agentscore_commerce.checkout import build_aip_trusted_issuers
from agentscore_commerce.discovery.llms_txt import llms_txt_identity_section
from agentscore_commerce.discovery.openapi import agentscore_security_schemes
from agentscore_commerce.discovery.skill_md import (
    SkillMdIdentityRequirements,
    build_skill_md,
)
from agentscore_commerce.identity import build_agent_memory_hint

# ── build_aip_trusted_issuers (direct node analog) ──


class TestBuildAipTrustedIssuers:
    def test_always_includes_the_canonical_agentscore_issuer(self) -> None:
        assert build_aip_trusted_issuers() == [AGENTSCORE_CANONICAL_ISSUER]
        assert build_aip_trusted_issuers([]) == [AGENTSCORE_CANONICAL_ISSUER]

    def test_appends_externals_and_dedupes_the_canonical_issuer(self) -> None:
        out = build_aip_trusted_issuers(["https://issuer.example", "https://www.agentscore.com/"])
        assert AGENTSCORE_CANONICAL_ISSUER in out
        assert any(i == "https://issuer.example" for i in out)
        assert len([i for i in out if i == AGENTSCORE_CANONICAL_ISSUER]) == 1


# ── agent_memory AIP path (direct node analog) ──


class TestAgentMemoryAipPath:
    def test_omits_agent_identity_when_no_aip_issuers_are_passed(self) -> None:
        hint = build_agent_memory_hint()
        assert hint.identity_paths.get("agent_identity") is None
        assert hint.aip_trusted_issuers is None

    def test_advertises_agent_identity_and_issuers_when_aip_is_accepted_canonical_only(self) -> None:
        hint = build_agent_memory_hint(build_aip_trusted_issuers())
        assert "Agent-Identity" in hint.identity_paths["agent_identity"]
        assert hint.aip_trusted_issuers == [AGENTSCORE_CANONICAL_ISSUER]

    def test_first_encounter_agent_memory_forwards_the_aip_issuers(self) -> None:
        hint = first_encounter_agent_memory(first_encounter=True, aip_trusted_issuers=build_aip_trusted_issuers())
        assert hint is not None
        assert "RFC 9421" in hint.identity_paths["agent_identity"]
        assert hint.aip_trusted_issuers == [AGENTSCORE_CANONICAL_ISSUER]

    def test_first_encounter_agent_memory_without_aip_stays_wallet_operator_only(self) -> None:
        hint = first_encounter_agent_memory(first_encounter=True)
        assert hint is not None
        assert hint.identity_paths.get("agent_identity") is None


# ── llms.txt identity section (ported node AIP bullet) ──


class TestLlmsTxtIdentitySection:
    def test_adds_the_agent_identity_bullet_when_aip_is_true(self) -> None:
        out = llms_txt_identity_section(agentscore=True, aip=True)
        assert "Agent-Identity" in out
        assert "RFC 9421" in out

    def test_omits_the_aip_bullet_by_default(self) -> None:
        out = llms_txt_identity_section(agentscore=True)
        assert "Agent-Identity" not in out
        assert "X-Operator-Token" in out


# ── skill.md identity section (ported node AIT path) ──


class TestSkillMdIdentitySection:
    @staticmethod
    def _build(identity: SkillMdIdentityRequirements) -> str:
        return build_skill_md(
            name="test-merchant",
            description="Test merchant skill for AIP presentation.",
            homepage="https://merchant.example",
            merchant_name="Test Merchant",
            accepted_rails=["tempo_mpp"],
            endpoints=[{"method": "POST", "path": "/purchase", "auth_required": True, "description": "Buy"}],
            triggers=["buy something"],
            identity=identity,
        )

    def test_documents_the_ait_path_when_identity_aip_is_set(self) -> None:
        md = self._build({"kyc_required": True, "aip": True})
        assert "Agent Identity Token" in md
        assert "Agent-Identity" in md

    def test_does_not_mention_aip_when_identity_aip_is_unset(self) -> None:
        md = self._build({"kyc_required": True})
        assert "Agent-Identity" not in md


# ── openapi security schemes (ported node AgentIdentity scheme) ──


class TestOpenApiSecuritySchemes:
    def test_includes_the_agent_identity_scheme(self) -> None:
        schemes = agentscore_security_schemes()
        assert schemes["AgentIdentity"]["type"] == "apiKey"
        assert schemes["AgentIdentity"]["in"] == "header"
        assert schemes["AgentIdentity"]["name"] == "Agent-Identity"
        assert {"OperatorToken", "WalletAddress", "siwx"} <= set(schemes)
