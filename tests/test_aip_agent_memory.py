"""build_agent_memory_hint x AIP.

Ports node-commerce ``tests/agent_memory_aip.test.ts``. The cross-merchant memory hint advertises
the AIT identity path ONLY when the merchant opted into AIP (a non-empty trusted-issuer list).
Merchants that don't accept AITs must not tell agents to present one. The Python
``build_agent_memory_hint`` carries ``identity_paths`` as a dict (node uses an object) and exposes
``aip_trusted_issuers`` on the ``AgentMemoryHint`` dataclass.
"""

from __future__ import annotations

from agentscore_commerce.identity import build_agent_memory_hint


class TestBuildAgentMemoryHintAip:
    def test_omits_agent_identity_path_and_issuers_when_no_issuers_configured(self) -> None:
        hint = build_agent_memory_hint()
        assert hint.identity_paths["wallet"]
        assert hint.identity_paths["operator_token"]
        assert hint.identity_paths.get("agent_identity") is None
        assert hint.aip_trusted_issuers is None

    def test_omits_aip_guidance_for_an_empty_issuer_list_opted_out(self) -> None:
        hint = build_agent_memory_hint([])
        assert hint.identity_paths.get("agent_identity") is None
        assert hint.aip_trusted_issuers is None

    def test_advertises_the_agent_identity_path_and_issuers_when_aip_is_configured(self) -> None:
        issuers = ["https://issuer.example", "https://www.agentscore.com"]
        hint = build_agent_memory_hint(issuers)
        assert hint.aip_trusted_issuers == issuers
        assert "Agent-Identity" in hint.identity_paths["agent_identity"]
        assert "RFC 9421" in hint.identity_paths["agent_identity"]
        # The opaque-token + wallet paths remain present (AIP is additive, not a replacement).
        assert hint.identity_paths["wallet"]
        assert hint.identity_paths["operator_token"]
