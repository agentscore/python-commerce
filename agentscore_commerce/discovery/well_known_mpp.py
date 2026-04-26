"""`/.well-known/mpp.json` discovery document builder."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaymentMethodConfig:
    methods: list[str]
    x402: dict[str, Any] | None = None
    identity: list[str] | None = None
    identity_paths: dict[str, Any] | None = None
    compliance: dict[str, Any] | None = None
    required_fields: list[str] | None = None
    optional_fields: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WellKnownMppInput:
    name: str
    url: str
    endpoints: dict[str, dict[str, str]]
    purchase: PaymentMethodConfig
    description: str | None = None
    openapi: str | None = None
    catalog: dict[str, Any] | None = None
    shipping: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_well_known_mpp(input: WellKnownMppInput) -> dict[str, Any]:
    """Build the standard `.well-known/mpp.json` discovery document."""
    out: dict[str, Any] = {"name": input.name}
    if input.description:
        out["description"] = input.description
    out["url"] = input.url
    if input.openapi:
        out["openapi"] = input.openapi
    out["endpoints"] = input.endpoints
    if input.catalog:
        out["catalog"] = input.catalog

    purchase: dict[str, Any] = {}
    if input.purchase.required_fields:
        purchase["required_fields"] = input.purchase.required_fields
    if input.purchase.optional_fields:
        purchase["optional_fields"] = input.purchase.optional_fields
    purchase.update(input.purchase.extra)
    if input.purchase.identity:
        purchase["identity"] = input.purchase.identity
    if input.purchase.identity_paths:
        purchase["identity_paths"] = input.purchase.identity_paths
    purchase["payment_methods"] = input.purchase.methods
    if input.purchase.x402:
        purchase["x402"] = input.purchase.x402
    if input.purchase.compliance:
        purchase["compliance"] = input.purchase.compliance
    out["purchase"] = purchase

    if input.shipping:
        out["shipping"] = input.shipping
    out.update(input.extra)
    return out
