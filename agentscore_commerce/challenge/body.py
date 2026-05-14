"""build_402_body — full enriched 402 response body builder."""

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Literal

from agentscore_commerce.challenge.pricing import PricingBlock
from agentscore_commerce.payment.wwwauthenticate import alias_amount_fields


@dataclass
class X402PaymentRequired:
    accepts: list[Any]
    version: Literal[1, 2] = 2


def build_402_body(
    *,
    accepted_methods: list[dict[str, Any]],
    agent_instructions: dict[str, Any] | None = None,
    identity_metadata: dict[str, Any] | None = None,
    agent_memory: Any = None,
    pricing: PricingBlock | None = None,
    amount_usd: str | None = None,
    currency: str | None = None,
    order_id: str | None = None,
    product: dict[str, str] | None = None,
    retry_body: Any = None,
    recommended: str | None = None,
    x402: X402PaymentRequired | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full enriched 402 response body. Each section conditionally included if vendor passed it."""
    body: dict[str, Any] = {"payment_required": True, "accepted_methods": accepted_methods}
    if x402:
        body["x402Version"] = x402.version
        body["accepts"] = alias_amount_fields(x402.accepts)
    if amount_usd is not None:
        body["amount_usd"] = amount_usd
    if currency:
        body["currency"] = currency
    if pricing:
        body["pricing"] = pricing.to_dict()
    if order_id is not None:
        body["order_id"] = order_id
    if product:
        body["product"] = product
    if recommended:
        body["recommended"] = recommended
    if retry_body is not None:
        body["retry_body"] = retry_body
    if identity_metadata:
        body.update(identity_metadata)
    if agent_instructions:
        body["agent_instructions"] = agent_instructions
    if agent_memory is not None:
        # AgentMemoryHint is a dataclass; merchants pass it directly via
        # first_encounter_agent_memory(...). Convert here so JSONResponse /
        # json.dumps can serialise without per-merchant boilerplate.
        body["agent_memory"] = (
            asdict(agent_memory) if is_dataclass(agent_memory) and not isinstance(agent_memory, type) else agent_memory
        )
    if extra:
        body.update(extra)
    return body
