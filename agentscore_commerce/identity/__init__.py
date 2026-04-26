"""Trust-gating middleware for Python web frameworks using AgentScore."""

from typing import Any

from agentscore_commerce.identity._denial import (
    FIXABLE_DENIAL_REASONS,
    build_contact_support_next_steps,
    build_signer_mismatch_body,
    denial_reason_status,
    is_fixable_denial,
    verification_agent_instructions,
)
from agentscore_commerce.identity._response import denial_reason_to_body
from agentscore_commerce.identity.a2a import (
    A2AAgentCard,
    A2AAgentCardCapabilities,
    A2AAgentCardIdentity,
    build_a2a_agent_card,
)
from agentscore_commerce.identity.client import GateClient
from agentscore_commerce.identity.erc8004 import (
    AGENTSCORE_ERC8004_SCHEMA,
    AgentScoreERC8004Attribute,
    build_erc8004_attribute,
)
from agentscore_commerce.identity.signer import extract_x402_signer
from agentscore_commerce.identity.types import (
    Activity,
    AgentIdentity,
    AgentMemoryHint,
    AssessResult,
    Classification,
    DenialCode,
    DenialReason,
    Grade,
    Identity,
    OperatorVerification,
    ScoreDetail,
    VerifyWalletSignerMatchOptions,
    VerifyWalletSignerResult,
    build_agent_memory_hint,
)
from agentscore_commerce.identity.ucp import (
    AGENTSCORE_UCP_CAPABILITY,
    UCPCapability,
    UCPPaymentHandler,
    UCPProfile,
    UCPService,
    UCPSigningKey,
    build_ucp_profile,
)


# ASGI middleware is the default import (re-exported as CreateSessionOnMissing too).
# Framework adapters are imported from their own submodules:
#   from agentscore_commerce.identity.fastapi import AgentScoreGate, get_assess_data  # native Depends()
#   from agentscore_commerce.identity.flask import agentscore_gate
#   from agentscore_commerce.identity.django import AgentScoreMiddleware
#   from agentscore_commerce.identity.aiohttp import agentscore_gate_middleware
#   from agentscore_commerce.identity.sanic import agentscore_gate
def _load_asgi_middleware() -> tuple[Any, Any]:
    try:
        from agentscore_commerce.identity.middleware import AgentScoreGate as _AgentScoreGate
        from agentscore_commerce.identity.middleware import CreateSessionOnMissing as _CreateSessionOnMissing

        return _AgentScoreGate, _CreateSessionOnMissing
    except ImportError:
        # starlette not installed
        return None, None


AgentScoreGate, CreateSessionOnMissing = _load_asgi_middleware()

__all__ = [
    "AGENTSCORE_ERC8004_SCHEMA",
    "AGENTSCORE_UCP_CAPABILITY",
    "FIXABLE_DENIAL_REASONS",
    "A2AAgentCard",
    "A2AAgentCardCapabilities",
    "A2AAgentCardIdentity",
    "Activity",
    "AgentIdentity",
    "AgentMemoryHint",
    "AgentScoreERC8004Attribute",
    "AgentScoreGate",
    "AssessResult",
    "Classification",
    "CreateSessionOnMissing",
    "DenialCode",
    "DenialReason",
    "GateClient",
    "Grade",
    "Identity",
    "OperatorVerification",
    "ScoreDetail",
    "UCPCapability",
    "UCPPaymentHandler",
    "UCPProfile",
    "UCPService",
    "UCPSigningKey",
    "VerifyWalletSignerMatchOptions",
    "VerifyWalletSignerResult",
    "build_a2a_agent_card",
    "build_agent_memory_hint",
    "build_contact_support_next_steps",
    "build_erc8004_attribute",
    "build_signer_mismatch_body",
    "build_ucp_profile",
    "denial_reason_status",
    "denial_reason_to_body",
    "extract_x402_signer",
    "is_fixable_denial",
    "verification_agent_instructions",
]
