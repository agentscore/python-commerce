"""Tests for the shared denial-body marshaller.

Covers the fallback agent_instructions injection added in PR-fix-wallet-not-trusted —
every denial code that doesn't already get instructions from the gate must come out
of ``denial_reason_to_body`` with a machine-readable next-step block.
"""

from __future__ import annotations

import json

from agentscore_commerce.identity._response import denial_reason_to_body
from agentscore_commerce.identity.types import DenialReason


def test_injects_canonical_wallet_not_trusted_instructions() -> None:
    # wallet_not_trusted reaches the agent ONLY for unfixable reasons (sanctions /
    # age / jurisdiction_restricted). Fixable reasons (kyc_required, etc.) are
    # rerouted to identity_verification_required by the gate adapter.
    body = denial_reason_to_body(
        DenialReason(
            code="wallet_not_trusted",
            reasons=["sanctions_flagged"],
            verify_url="https://agentscore.sh/dashboard/verify?address=0xabc&chain=base",
        )
    )
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "contact_support"
    assert isinstance(instructions["steps"], list)
    assert "merchant" in instructions["user_message"].lower() or "support" in instructions["user_message"].lower()


def test_injects_canonical_payment_required_instructions() -> None:
    body = denial_reason_to_body(DenialReason(code="payment_required"))
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "contact_merchant"


def test_injects_fallback_identity_verification_required_instructions() -> None:
    body = denial_reason_to_body(
        DenialReason(
            code="identity_verification_required",
            verify_url="https://agentscore.sh/verify?session=sess_abc",
        )
    )
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "deliver_verify_url_and_poll"


def test_injects_fallback_token_expired_instructions() -> None:
    body = denial_reason_to_body(
        DenialReason(
            code="token_expired",
            verify_url="https://agentscore.sh/verify?session=sess_abc",
        )
    )
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "deliver_verify_url_and_poll"


def test_explicit_agent_instructions_takes_precedence_over_default() -> None:
    custom = json.dumps({"action": "custom_action", "steps": ["custom"]})
    body = denial_reason_to_body(DenialReason(code="wallet_not_trusted", agent_instructions=custom))
    assert body["agent_instructions"] == custom


def test_api_error_emits_retry_with_backoff_instructions() -> None:
    # api_error denials get a structured agent_instructions block with retry-with-backoff
    # guidance so agents distinguish transient AgentScore-side issues from compliance denials.
    # agent_instructions is the single retry channel — no separate next_steps block.
    body = denial_reason_to_body(DenialReason(code="api_error"))
    assert "agent_instructions" in body
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "retry_with_backoff"
    assert "Verification is temporarily unavailable" in instructions["steps"][0]
    assert "next_steps" not in body


def test_api_error_with_quota_instructions_overrides_retry_default() -> None:
    # Adapters explicitly pass QUOTA_EXCEEDED_INSTRUCTIONS on the DenialReason for the 429
    # path so the agent gets contact_merchant guidance instead of an infinite retry loop.
    from agentscore_commerce.identity._response import QUOTA_EXCEEDED_INSTRUCTIONS

    body = denial_reason_to_body(DenialReason(code="api_error", agent_instructions=QUOTA_EXCEEDED_INSTRUCTIONS))
    instructions = json.loads(body["agent_instructions"])
    assert instructions["action"] == "contact_merchant"
    assert "merchant-side issue" in instructions["steps"][0]
