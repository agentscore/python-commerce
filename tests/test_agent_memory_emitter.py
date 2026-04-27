"""Tests for first_encounter_agent_memory."""

from agentscore_commerce.challenge import build_agent_memory_hint, first_encounter_agent_memory


def test_returns_none_when_first_encounter_false():
    assert first_encounter_agent_memory(first_encounter=False) is None


def test_returns_canonical_hint_when_first_encounter_true():
    hint = first_encounter_agent_memory(first_encounter=True)
    assert hint is not None
    assert hint == build_agent_memory_hint()


def test_hint_strings_byte_match_node_commerce_for_wire_parity():
    """Cross-language wire parity — these exact strings appear in agent_memory bodies emitted
    by both @agent-score/commerce (node-commerce/src/core.ts buildAgentMemoryHint) and this
    package. Agents that memorize the pattern from one merchant must recognize it byte-for-byte
    from another regardless of which SDK the merchant runs.
    """
    hint = build_agent_memory_hint()
    # Backticks-around-header markdown is intentional — node-commerce uses them for monospace
    # rendering in markdown-aware viewers (chat surfaces, dashboards). Python must match.
    assert hint.identity_paths["wallet"].endswith(
        "`X-Wallet-Address: 0x...`. Shortest path; no token lifecycle to manage."
    )
    assert hint.identity_paths["operator_token"].endswith(
        "`X-Operator-Token: opc_...`. Reusable across merchants until the token expires."
    )
    assert "`agent_instructions` block" in hint.bootstrap
    assert hint.do_not_persist_in_memory == ["operator_token", "poll_secret"]
    assert hint.persist_in_credential_store == ["operator_token"]
