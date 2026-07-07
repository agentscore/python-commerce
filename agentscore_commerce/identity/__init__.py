"""Trust-gating middleware for Python web frameworks using AgentScore."""

from typing import Any

from agentscore_commerce.aip.gate import (
    AipErrorBody,
    AipErrorRequirements,
    AipGateEvaluation,
    AipGateOptions,
    AipGateResult,
    aip_error_code,
    aip_error_status,
    build_aip_error_body,
    build_aip_weak_auth_body,
    check_trust_requirements,
    evaluate_aip_parts,
    evaluate_aip_request,
    verify_ait_parts,
    verify_ait_request,
)
from agentscore_commerce.aip.jwks import JwksCache
from agentscore_commerce.aip.request import (
    build_verify_context_from_parts,
    build_verify_context_from_request,
    has_agent_identity_header,
    has_agent_identity_header_parts,
)
from agentscore_commerce.aip.verify import VerifiedAit, VerifyRequestContext
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
    AIP_A2A_EXTENSION_URI,
    UCP_A2A_EXTENSION_URI,
    A2AAgentCard,
    A2AAgentCardCapabilities,
    A2AAgentCardExtension,
    A2AAgentCardSignature,
    A2AAgentInterface,
    A2AAgentProvider,
    A2AAgentSkill,
    aip_a2a_extension,
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
    "AIP_A2A_EXTENSION_URI",
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
    "AipErrorBody",
    "AipErrorRequirements",
    "AipGateEvaluation",
    "AipGateOptions",
    "AipGateResult",
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
    "JwksCache",
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
    "VerifiedAit",
    "VerifyRequestContext",
    "VerifyWalletSignerResult",
    "aip_a2a_extension",
    "aip_error_code",
    "aip_error_status",
    "build_a2a_agent_card",
    "build_agent_memory_hint",
    "build_aip_error_body",
    "build_aip_weak_auth_body",
    "build_contact_support_next_steps",
    "build_gate_from_policy",
    "build_jwks_response",
    "build_signer_mismatch_body",
    "build_ucp_profile",
    "build_verification_required_body",
    "build_verify_context_from_parts",
    "build_verify_context_from_request",
    "check_trust_requirements",
    "create_default_on_denied",
    "default_read_only_on_denied",
    "denial_reason_status",
    "denial_reason_to_body",
    "evaluate_aip_parts",
    "evaluate_aip_request",
    "extract_owner_scope",
    "extract_x402_signer",
    "generate_ucp_signing_key",
    "has_agent_identity_header",
    "has_agent_identity_header_parts",
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
    "verify_ait_parts",
    "verify_ait_request",
    "verify_ucp_profile",
    "x402_payment_handler",
]
