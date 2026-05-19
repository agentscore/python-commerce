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
from agentscore_commerce.identity._response import build_verification_required_body, denial_reason_to_body
from agentscore_commerce.identity.a2a import (
    A2A_DEFAULT_TRANSPORT,
    A2A_PROTOCOL_VERSION,
    UCP_A2A_EXTENSION_URI,
    A2AAgentCard,
    A2AAgentCardCapabilities,
    A2AAgentCardExtension,
    A2AAgentCardSignature,
    A2AAgentInterface,
    A2AAgentProvider,
    A2AAgentSkill,
    build_a2a_agent_card,
    ucp_a2a_extension,
)
from agentscore_commerce.identity.core import AgentScoreCore
from agentscore_commerce.identity.default_denied import (
    DefaultOnDeniedResult,
    create_default_on_denied,
    default_read_only_on_denied,
)
from agentscore_commerce.identity.policy import (
    EnforcementMode,
    GateResult,
    IdentityStatus,
    PolicyBlock,
    build_gate_from_policy,
    run_gate_with_enforcement,
    shipping_country_allowed,
    shipping_state_allowed,
    validate_shipping_against_policy,
)
from agentscore_commerce.identity.signer import extract_x402_signer
from agentscore_commerce.identity.tokens import OwnerScope, extract_owner_scope, hash_operator_token
from agentscore_commerce.identity.types import (
    AgentIdentity,
    AgentMemoryHint,
    AssessResult,
    DenialCode,
    DenialReason,
    OperatorVerification,
    SignerSanctions,
    SignerVerdict,
    VerifyWalletSignerResult,
    build_agent_memory_hint,
)
from agentscore_commerce.identity.ucp import (
    AGENTSCORE_UCP_CAPABILITY,
    AgentScoreGatePolicy,
    UCPCapabilityBinding,
    UCPPaymentHandlerBinding,
    UCPProfile,
    UCPProfileBody,
    UCPServiceBinding,
    UCPSigningKey,
    build_ucp_profile,
    mpp_payment_handler,
    stripe_spt_payment_handler,
    x402_payment_handler,
)
from agentscore_commerce.identity.ucp_jwks import (
    GeneratedUCPKey,
    UCPVerificationError,
    build_jwks_response,
    generate_ucp_signing_key,
    load_ucp_signing_key_from_env,
    sign_ucp_profile,
    verify_ucp_profile,
)


# ASGI middleware is the default import (re-exported as CreateSessionOnMissing too).
# Framework adapters are imported from their own submodules:
#   from agentscore_commerce.identity.fastapi import AgentScoreGate, get_agentscore_data  # native Depends()
#   from agentscore_commerce.identity.flask import agentscore_gate
#   from agentscore_commerce.identity.django import AgentScoreMiddleware
#   from agentscore_commerce.identity.aiohttp import agentscore_gate_middleware
#   from agentscore_commerce.identity.sanic import agentscore_gate
def _load_asgi_middleware() -> tuple[Any, Any, Any]:
    try:
        from agentscore_commerce.identity.middleware import AgentScoreGate as _AgentScoreGate
        from agentscore_commerce.identity.middleware import (
            ConditionalAgentScoreGate as _ConditionalAgentScoreGate,
        )
        from agentscore_commerce.identity.middleware import CreateSessionOnMissing as _CreateSessionOnMissing

        return _AgentScoreGate, _ConditionalAgentScoreGate, _CreateSessionOnMissing
    except ImportError:
        # starlette not installed
        return None, None, None


AgentScoreGate, ConditionalAgentScoreGate, CreateSessionOnMissing = _load_asgi_middleware()

__all__ = [
    "A2A_DEFAULT_TRANSPORT",
    "A2A_PROTOCOL_VERSION",
    "AGENTSCORE_UCP_CAPABILITY",
    "FIXABLE_DENIAL_REASONS",
    "UCP_A2A_EXTENSION_URI",
    "A2AAgentCard",
    "A2AAgentCardCapabilities",
    "A2AAgentCardExtension",
    "A2AAgentCardSignature",
    "A2AAgentInterface",
    "A2AAgentProvider",
    "A2AAgentSkill",
    "AgentIdentity",
    "AgentMemoryHint",
    "AgentScoreCore",
    "AgentScoreGate",
    "AgentScoreGatePolicy",
    "AssessResult",
    "ConditionalAgentScoreGate",
    "CreateSessionOnMissing",
    "DefaultOnDeniedResult",
    "DenialCode",
    "DenialReason",
    "EnforcementMode",
    "GateResult",
    "GeneratedUCPKey",
    "IdentityStatus",
    "OperatorVerification",
    "OwnerScope",
    "PolicyBlock",
    "SignerSanctions",
    "SignerVerdict",
    "UCPCapabilityBinding",
    "UCPPaymentHandlerBinding",
    "UCPProfile",
    "UCPProfileBody",
    "UCPServiceBinding",
    "UCPSigningKey",
    "UCPVerificationError",
    "VerifyWalletSignerResult",
    "build_a2a_agent_card",
    "build_agent_memory_hint",
    "build_contact_support_next_steps",
    "build_gate_from_policy",
    "build_jwks_response",
    "build_signer_mismatch_body",
    "build_ucp_profile",
    "build_verification_required_body",
    "create_default_on_denied",
    "default_read_only_on_denied",
    "denial_reason_status",
    "denial_reason_to_body",
    "extract_owner_scope",
    "extract_x402_signer",
    "generate_ucp_signing_key",
    "hash_operator_token",
    "is_fixable_denial",
    "load_ucp_signing_key_from_env",
    "mpp_payment_handler",
    "run_gate_with_enforcement",
    "shipping_country_allowed",
    "shipping_state_allowed",
    "sign_ucp_profile",
    "stripe_spt_payment_handler",
    "ucp_a2a_extension",
    "validate_shipping_against_policy",
    "verification_agent_instructions",
    "verify_ucp_profile",
    "x402_payment_handler",
]
