"""build_402_body — full enriched 402 response body builder."""

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Literal

from agentscore_commerce.challenge.pricing import PricingBlock


@dataclass
class X402ResourceInfo:
    """x402 v2 ``ResourceInfo`` resource metadata for the 402 body.

    Surfaced on the 402 body (and the PAYMENT-REQUIRED header) so spec-compliant
    crawlers and discovery clients can read what the paid resource is. Mirrors
    x402's ``ResourceInfoSchema``.
    """

    url: str
    description: str | None = None
    mime_type: str | None = None
    service_name: str | None = None
    tags: list[str] | None = None
    icon_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict with x402 camelCase keys, omitting unset fields."""
        out: dict[str, Any] = {"url": self.url}
        if self.description is not None:
            out["description"] = self.description
        if self.mime_type is not None:
            out["mimeType"] = self.mime_type
        if self.service_name is not None:
            out["serviceName"] = self.service_name
        if self.tags is not None:
            out["tags"] = self.tags
        if self.icon_url is not None:
            out["iconUrl"] = self.icon_url
        return out


@dataclass
class X402PaymentRequired:
    accepts: list[Any]
    version: Literal[1, 2] = 2
    resource: X402ResourceInfo | dict[str, Any] | None = None
    """x402 v2 ``resource`` field: resource metadata (url + service_name / tags /
    description / mime_type / icon_url). Emitted on the 402 body as ``body.resource``
    so spec-compliant crawlers and discovery clients can read what's being sold."""
    extensions: dict[str, Any] | None = None
    """Per-endpoint x402 ``extensions`` block (e.g. Bazaar discovery declared
    via ``build_bazaar_discovery_payload``). Emitted on the 402 body as
    ``body.extensions`` per x402 spec when non-empty."""


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
        # No v1<->v2 amount alias: strict x402 v2 settlement matches the echoed
        # requirement against the rebuilt one by exact comparison, so an extra
        # maxAmountRequired the rebuild lacks silently fails settle. Keep accepts
        # identical to build_payment_requirements output.
        body["accepts"] = x402.accepts
        if x402.resource is not None:
            body["resource"] = x402.resource.to_dict() if isinstance(x402.resource, X402ResourceInfo) else x402.resource
        if x402.extensions:
            body["extensions"] = x402.extensions
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
