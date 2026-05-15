"""Discovery helpers — probe responder, Bazaar payload builder, .well-known/mpp.json, llms.txt, OpenAPI snippets."""

from agentscore_commerce.discovery.agentscore_content import (
    PURCHASE_MODE_NOTES,
    PurchaseMode,
    build_agentscore_onboarding_steps,
    build_order_success_next_steps,
    purchase_mode_note,
    standard_endpoint_descriptions,
)
from agentscore_commerce.discovery.bazaar import build_bazaar_discovery_payload
from agentscore_commerce.discovery.llms_txt import (
    LlmsTxtSection,
    build_llms_txt,
    llms_txt_identity_section,
    llms_txt_payment_section,
)
from agentscore_commerce.discovery.openapi import (
    XPaymentInfoDynamicPrice,
    XPaymentInfoFixedPrice,
    XPaymentInfoMpp,
    agentscore_denial_schemas,
    agentscore_openapi_snippets,
    agentscore_payment_required_schema,
    agentscore_security_schemes,
    siwx_security_scheme,
    x_guidance_extension,
    x_payment_info_extension,
)
from agentscore_commerce.discovery.probe import (
    DiscoveryProbeResponse,
    X402SampleProbe,
    build_discovery_probe_response,
    is_discovery_probe_request,
    sample_x402_accept_for_network,
)
from agentscore_commerce.discovery.redemption_md import build_redemption_skill_md
from agentscore_commerce.discovery.robots_tag import (
    DEFAULT_DISCOVERY_PATHS,
    DEFAULT_ROBOTS_TAG,
    DjangoNoindexMiddleware,
    NoindexNonDiscoveryMiddleware,
    install_flask_noindex,
    is_discovery_path,
)
from agentscore_commerce.discovery.skill_md import (
    RailKey,
    SkillMdEndpoint,
    SkillMdIdentityRequirements,
    SkillMdLink,
    SkillMdShippingPolicy,
    build_skill_md,
    compatible_clients_by_rails,
)
from agentscore_commerce.discovery.well_known import (
    SignedDiscoveryResponse,
    bootstrap_ucp_signing_key,
    build_signed_jwks_response,
    build_signed_ucp_response,
    default_a2a_services,
    well_known_cors_preflight_headers,
)
from agentscore_commerce.discovery.well_known_mpp import (
    PaymentMethodConfig,
    build_well_known_mpp,
)
from agentscore_commerce.discovery.well_known_x402 import (
    WellKnownX402Resource,
    build_well_known_x402,
)

__all__ = [
    "DEFAULT_DISCOVERY_PATHS",
    "DEFAULT_ROBOTS_TAG",
    "PURCHASE_MODE_NOTES",
    "DiscoveryProbeResponse",
    "DjangoNoindexMiddleware",
    "LlmsTxtSection",
    "NoindexNonDiscoveryMiddleware",
    "PaymentMethodConfig",
    "PurchaseMode",
    "RailKey",
    "SignedDiscoveryResponse",
    "SkillMdEndpoint",
    "SkillMdIdentityRequirements",
    "SkillMdLink",
    "SkillMdShippingPolicy",
    "WellKnownX402Resource",
    "X402SampleProbe",
    "XPaymentInfoDynamicPrice",
    "XPaymentInfoFixedPrice",
    "XPaymentInfoMpp",
    "agentscore_denial_schemas",
    "agentscore_openapi_snippets",
    "agentscore_payment_required_schema",
    "agentscore_security_schemes",
    "bootstrap_ucp_signing_key",
    "build_agentscore_onboarding_steps",
    "build_bazaar_discovery_payload",
    "build_discovery_probe_response",
    "build_llms_txt",
    "build_order_success_next_steps",
    "build_redemption_skill_md",
    "build_signed_jwks_response",
    "build_signed_ucp_response",
    "build_skill_md",
    "build_well_known_mpp",
    "build_well_known_x402",
    "compatible_clients_by_rails",
    "default_a2a_services",
    "install_flask_noindex",
    "is_discovery_path",
    "is_discovery_probe_request",
    "llms_txt_identity_section",
    "llms_txt_payment_section",
    "purchase_mode_note",
    "sample_x402_accept_for_network",
    "siwx_security_scheme",
    "standard_endpoint_descriptions",
    "well_known_cors_preflight_headers",
    "x_guidance_extension",
    "x_payment_info_extension",
]
