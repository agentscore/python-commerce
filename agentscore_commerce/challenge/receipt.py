"""Canonical receipt shape returned to agents on the 200 after settlement.

Universal across vendor types: goods merchants populate the shipping +
fulfillment slots, API merchants populate only the core fields (id, created_at,
pricing, payment_status, next_steps). All goods-only fields are optional.

Merchants own their order schema, but converging on this shape across
AgentScore-gated merchants means agents can render and post-process receipts
consistently regardless of whether the seller ships product or returns API
output. Lift this type, fill the fields you care about, and ignore (or extend
via ``extras``) what you don't.

All money fields are dollar-strings. Use :func:`build_pricing_block` from
:mod:`agentscore_commerce.challenge` to compose the pricing fields from cents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentscore_commerce.challenge.pricing import PricingBlock


@dataclass
class ShippingAddress:
    """Physical-goods shipping address. Omit for digital goods or API receipts."""

    name: str | None = None
    address_1: str | None = None
    address_2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None


@dataclass
class ProductInfo:
    """Product info echoed on the receipt.

    Goods merchants populate; API merchants typically omit (per-call billing
    has no product concept).
    """

    id: str | None = None
    name: str | None = None
    slug: str | None = None


@dataclass
class ReceiptNextSteps:
    """Next-steps block guiding the agent post-settlement.

    ``order_status_url`` works for both: goods merchants point at their order
    detail route, API merchants can point at a usage / billing dashboard.
    ``fulfillment_eta`` is goods-only; omit for API or digital receipts.
    """

    user_message: str | None = None
    order_status_url: str | None = None
    fulfillment_eta: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Receipt:
    """Receipt returned on 200 after a successful settlement.

    Universal: goods merchants fill the shipping + fulfillment + product slots,
    API merchants populate only id + created_at + pricing + payment_status +
    next_steps. All goods-only fields below are optional.
    """

    id: str
    """Stable receipt id; order UUID for goods, request id for API merchants."""

    created_at: str
    """ISO-8601 timestamp of settlement."""

    quantity: int | None = None
    """Goods: units purchased. API: usage count (calls, tokens, requests)."""

    product: ProductInfo | None = None
    """Goods-shaped. Omit for API merchants."""

    pricing: PricingBlock | None = None
    email: str | None = None

    payment_status: str | None = None
    """Typically ``"completed"``, ``"pending"``, ``"failed"``."""

    fulfillment_status: str | None = None
    """Goods-only. Typically ``"pending"``, ``"shipped"``, ``"delivered"``, ``"cancelled"``."""

    tracking_number: str | None = None
    """Goods-only. Carrier tracking number when fulfillment_status >= shipped."""

    shipping: ShippingAddress | None = None
    """Goods-only. Omit for digital goods, services, or API receipts."""

    gift_note: str | None = None
    """Goods-only. Omit for API receipts."""

    extras: dict[str, Any] = field(default_factory=dict)
    """Vendor-specific extras merged at the top level (loyalty points,
    warranty, per-call usage breakdown, etc.)."""

    next_steps: ReceiptNextSteps | None = None


__all__ = ["ProductInfo", "Receipt", "ReceiptNextSteps", "ShippingAddress"]
