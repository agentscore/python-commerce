"""Helpers for emitting the cross-merchant ``agent_memory`` hint on merchant 402 responses.

The gate (:mod:`agentscore_commerce.identity`) emits ``agent_memory`` on identity-related
responses (sessions, credentials, missing_identity bootstraps). Merchants can ALSO include
the hint in their own 402 challenge bodies on first-encounter requests so agents persist
the cross-merchant pattern even when entering the ecosystem through a merchant-side
endpoint rather than a direct AgentScore API call.

Usage pattern:
    - Merchant tracks per-operator (or per-IP / per-fingerprint) "have I seen this agent
      before?" in their own DB
    - On first encounter, include the hint so the agent saves the pattern
    - On subsequent encounters, skip — the agent already has it (or never will)

The hint contents come from :func:`build_agent_memory_hint` (re-exported here for
convenience). Keep it stateless: AgentScore's pattern doesn't depend on the merchant's
identity, so every merchant emits the same shape.
"""

from __future__ import annotations

from agentscore_commerce.identity.types import AgentMemoryHint, build_agent_memory_hint


def first_encounter_agent_memory(
    first_encounter: bool,
    base_url: str = "",
) -> AgentMemoryHint | None:
    """Return the ``agent_memory`` hint when this is a first encounter, otherwise ``None``.

    Use directly with the ``agent_memory`` field of :func:`build_402_body`::

        body = build_402_body(Build402BodyInput(
            accepted_methods=accepted,
            agent_instructions=instructions,
            pricing=pricing,
            agent_memory=first_encounter_agent_memory(
                first_encounter=not has_seen_operator(operator_token),
            ),
        ))

    Returning ``None`` means ``build_402_body`` cleanly skips the field instead of
    emitting ``agent_memory: null`` (which would imply "I tried but failed" rather than
    "didn't apply").
    """
    if not first_encounter:
        return None
    return build_agent_memory_hint(base_url)


__all__ = ["AgentMemoryHint", "build_agent_memory_hint", "first_encounter_agent_memory"]
