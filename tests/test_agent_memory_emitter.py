"""Tests for first_encounter_agent_memory."""

from agentscore_commerce.challenge import build_agent_memory_hint, first_encounter_agent_memory


def test_returns_none_when_first_encounter_false():
    assert first_encounter_agent_memory(first_encounter=False) is None


def test_returns_canonical_hint_when_first_encounter_true():
    hint = first_encounter_agent_memory(first_encounter=True)
    assert hint is not None
    assert hint == build_agent_memory_hint()


def test_accepts_optional_base_url_without_crashing():
    hint = first_encounter_agent_memory(first_encounter=True, base_url="https://api.example")
    assert hint is not None
