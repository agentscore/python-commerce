"""402-body builders + pricing/receipt/agent-memory helpers."""

from agentscore_commerce.challenge.accepted_methods import build_accepted_methods
from agentscore_commerce.challenge.agent_instructions import build_agent_instructions, compatible_clients_by_rails
from agentscore_commerce.challenge.agent_memory import (
    AgentMemoryHint,
    build_agent_memory_hint,
    first_encounter_agent_memory,
)
from agentscore_commerce.challenge.body import X402PaymentRequired, build_402_body
from agentscore_commerce.challenge.how_to_pay import build_how_to_pay
from agentscore_commerce.challenge.identity import IdentityMode, SignerMatchResult, build_identity_metadata
from agentscore_commerce.challenge.pricing import PricingBlock, build_pricing_block
from agentscore_commerce.challenge.receipt import (
    ProductInfo,
    Receipt,
    ReceiptNextSteps,
    ShippingAddress,
)
from agentscore_commerce.challenge.respond_402 import Respond402Result, respond_402
from agentscore_commerce.challenge.validation_error import build_validation_error

__all__ = [
    "AgentMemoryHint",
    "IdentityMode",
    "PricingBlock",
    "ProductInfo",
    "Receipt",
    "ReceiptNextSteps",
    "Respond402Result",
    "ShippingAddress",
    "SignerMatchResult",
    "X402PaymentRequired",
    "build_402_body",
    "build_accepted_methods",
    "build_agent_instructions",
    "build_agent_memory_hint",
    "build_how_to_pay",
    "build_identity_metadata",
    "build_pricing_block",
    "build_validation_error",
    "compatible_clients_by_rails",
    "first_encounter_agent_memory",
    "respond_402",
]
