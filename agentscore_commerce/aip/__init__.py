"""AIP (Agentic Identity Protocol) — AIT verification (verifier role) + RFC 9421 signing.

This package is the AgentScore verifier for Agent Identity Tokens (AITs): a merchant gate
hands a parsed request plus a trusted-issuer :class:`JwksCache` to the orchestrator and gets
back the signature-checked, structurally-valid claims (or a typed failure mapped onto the AIP
wire error taxonomy). It also exposes the RFC 9421 HTTP Message Signature primitives an agent
uses to prove possession of its ``cnf``-bound key.

Submodules:
    agentscore_commerce.aip.types          - AIT claim contract + structural validation
    agentscore_commerce.aip.http_signature - RFC 9421 sign / verify (the AIP subset)
    agentscore_commerce.aip.jwks           - trusted-issuer enforcement + JWKS key discovery
    agentscore_commerce.aip.verify         - the AIT verification pipeline (orchestrator)
    agentscore_commerce.aip.request        - build a VerifyRequestContext from a framework request
    agentscore_commerce.aip.gate           - framework-agnostic gate + RFC 9457 denial bodies

The public surface mirrors the reference top-level AIP exports; where the reference
folds an input shape into an options interface (``VerifyAitOptions`` / ``SignMessageInput`` /
``VerifyMessageSignatureInput`` / ``JwksCacheOptions``), Python passes keyword args and the
corresponding concrete dataclasses (:class:`SignatureParams`, :class:`SignedMessage`,
:class:`VerifyRequestContext`) are surfaced instead.
"""

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
from agentscore_commerce.aip.http_signature import (
    AIP_COVERED_COMPONENTS,
    AIP_SIGNATURE_TAG,
    MAX_POP_WINDOW_SECONDS,
    ParsedSignatureInput,
    SignatureParams,
    SignedMessage,
    VerifyFailureReason,
    VerifyMessageSignatureResult,
    build_signature_base,
    normalize_authority,
    parse_signature_input,
    parse_signature_value,
    sign_message,
    verify_message_signature,
)
from agentscore_commerce.aip.jwks import (
    AGENTSCORE_CANONICAL_ISSUER,
    DEFAULT_CACHE_SECONDS,
    HARD_MAX_CACHE_SECONDS,
    JWKS_REFETCH_COOLDOWN_SECONDS,
    JWKS_WELL_KNOWN_PATH,
    FetchResponse,
    JwksCache,
    JwksLookupFailure,
    JwksLookupResult,
    canonicalize_issuer,
    resolve_cache_seconds,
)
from agentscore_commerce.aip.request import (
    HeadersLike,
    RequestLike,
    VerifyContextParts,
    build_verify_context_from_parts,
    build_verify_context_from_request,
    has_agent_identity_header,
    has_agent_identity_header_parts,
)
from agentscore_commerce.aip.types import (
    AitHeader,
    AitPayload,
    AitValidationResult,
    AmrValue,
    IdentityClaim,
    IntentClaim,
    TrustLevel,
    is_ait_shape,
    validate_ait_payload,
)
from agentscore_commerce.aip.verify import (
    AGENT_IDENTITY_HEADER,
    AIT_SIGNING_ALGS,
    SignatureMaterial,
    VerifiedAit,
    VerifyAitFailure,
    VerifyAitFailureResult,
    VerifyAitResult,
    VerifyAitSuccess,
    VerifyRequestContext,
    verify_ait,
)

__all__ = [
    "AGENTSCORE_CANONICAL_ISSUER",
    "AGENT_IDENTITY_HEADER",
    "AIP_COVERED_COMPONENTS",
    "AIP_SIGNATURE_TAG",
    "AIT_SIGNING_ALGS",
    "DEFAULT_CACHE_SECONDS",
    "HARD_MAX_CACHE_SECONDS",
    "JWKS_REFETCH_COOLDOWN_SECONDS",
    "JWKS_WELL_KNOWN_PATH",
    "MAX_POP_WINDOW_SECONDS",
    "AipErrorBody",
    "AipErrorRequirements",
    "AipGateEvaluation",
    "AipGateOptions",
    "AipGateResult",
    "AitHeader",
    "AitPayload",
    "AitValidationResult",
    "AmrValue",
    "FetchResponse",
    "HeadersLike",
    "IdentityClaim",
    "IntentClaim",
    "JwksCache",
    "JwksLookupFailure",
    "JwksLookupResult",
    "ParsedSignatureInput",
    "RequestLike",
    "SignatureMaterial",
    "SignatureParams",
    "SignedMessage",
    "TrustLevel",
    "VerifiedAit",
    "VerifyAitFailure",
    "VerifyAitFailureResult",
    "VerifyAitResult",
    "VerifyAitSuccess",
    "VerifyContextParts",
    "VerifyFailureReason",
    "VerifyMessageSignatureResult",
    "VerifyRequestContext",
    "aip_error_code",
    "aip_error_status",
    "build_aip_error_body",
    "build_aip_weak_auth_body",
    "build_signature_base",
    "build_verify_context_from_parts",
    "build_verify_context_from_request",
    "canonicalize_issuer",
    "check_trust_requirements",
    "evaluate_aip_parts",
    "evaluate_aip_request",
    "has_agent_identity_header",
    "has_agent_identity_header_parts",
    "is_ait_shape",
    "normalize_authority",
    "parse_signature_input",
    "parse_signature_value",
    "resolve_cache_seconds",
    "sign_message",
    "validate_ait_payload",
    "verify_ait",
    "verify_ait_parts",
    "verify_ait_request",
    "verify_message_signature",
]
