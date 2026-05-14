"""`/.well-known/mpp.json` discovery document builder."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaymentMethodConfig:
    """``purchase`` block input for :func:`build_well_known_mpp`."""

    methods: list[str]
    x402: dict[str, Any] | None = None
    identity: list[str] | None = None
    identity_paths: dict[str, Any] | None = None
    compliance: dict[str, Any] | None = None
    required_fields: list[str] | None = None
    optional_fields: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_well_known_mpp(
    *,
    name: str,
    url: str,
    endpoints: dict[str, dict[str, str]],
    purchase: PaymentMethodConfig,
    description: str | None = None,
    openapi: str | None = None,
    catalog: dict[str, Any] | None = None,
    shipping: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard `.well-known/mpp.json` discovery document.

    ``purchase`` carries the payment-methods + identity-paths + compliance config.
    Use :class:`PaymentMethodConfig` since it carries nested structure that vendors
    construct discriminated; the surrounding wrapper has been flattened.
    """
    out: dict[str, Any] = {"name": name}
    if description:
        out["description"] = description
    out["url"] = url
    if openapi:
        out["openapi"] = openapi
    out["endpoints"] = endpoints
    if catalog:
        out["catalog"] = catalog

    purchase_block: dict[str, Any] = {}
    if purchase.required_fields:
        purchase_block["required_fields"] = purchase.required_fields
    if purchase.optional_fields:
        purchase_block["optional_fields"] = purchase.optional_fields
    purchase_block.update(purchase.extra)
    if purchase.identity:
        purchase_block["identity"] = purchase.identity
    if purchase.identity_paths:
        purchase_block["identity_paths"] = purchase.identity_paths
    purchase_block["payment_methods"] = purchase.methods
    if purchase.x402:
        purchase_block["x402"] = purchase.x402
    if purchase.compliance:
        purchase_block["compliance"] = purchase.compliance
    out["purchase"] = purchase_block

    if shipping:
        out["shipping"] = shipping
    if extra:
        out.update(extra)
    return out
