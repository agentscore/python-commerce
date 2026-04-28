"""build_402_body — full enriched 402 response body builder."""

from dataclasses import dataclass, field
from typing import Any, Literal

from agentscore_commerce.challenge.pricing import PricingBlock
from agentscore_commerce.payment.wwwauthenticate import alias_amount_fields


@dataclass
class X402PaymentRequired:
    accepts: list[Any]
    version: Literal[1, 2] = 2


@dataclass
class Build402BodyInput:
    accepted_methods: list[dict[str, Any]]
    agent_instructions: dict[str, Any] | None = None
    identity_metadata: dict[str, Any] | None = None
    agent_memory: Any = None
    pricing: PricingBlock | None = None
    amount_usd: str | None = None
    currency: str | None = None
    order_id: str | None = None
    product: dict[str, str] | None = None
    retry_body: Any = None
    recommended: str | None = None
    x402: X402PaymentRequired | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_402_body(input: Build402BodyInput) -> dict[str, Any]:
    """Assemble the full enriched 402 response body. Each section conditionally included if vendor passed it."""
    body: dict[str, Any] = {"payment_required": True, "accepted_methods": input.accepted_methods}
    if input.x402:
        body["x402Version"] = input.x402.version
        body["accepts"] = alias_amount_fields(input.x402.accepts)
    if input.amount_usd is not None:
        body["amount_usd"] = input.amount_usd
    if input.currency:
        body["currency"] = input.currency
    if input.pricing:
        body["pricing"] = input.pricing.to_dict()
    if input.order_id is not None:
        body["order_id"] = input.order_id
    if input.product:
        body["product"] = input.product
    if input.recommended:
        body["recommended"] = input.recommended
    if input.retry_body is not None:
        body["retry_body"] = input.retry_body
    if input.identity_metadata:
        body.update(input.identity_metadata)
    if input.agent_instructions:
        body["agent_instructions"] = input.agent_instructions
    if input.agent_memory is not None:
        body["agent_memory"] = input.agent_memory
    body.update(input.extra)
    return body
