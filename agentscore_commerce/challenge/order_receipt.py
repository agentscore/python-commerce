"""Canonical order-receipt shape returned to agents on the 200 after settlement.

Merchants own their order schema, but converging on this shape across every AgentScore-gated
merchant (Martin Estate today; Commerce7 / WooCommerce / Shopify plugins tomorrow) means
agents can render and post-process orders consistently. Lift this type, fill the fields you
care about, and ignore (or extend via ``extras``) what you don't.

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
    """Physical-goods shipping address."""

    name: str | None = None
    address_1: str | None = None
    address_2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None


@dataclass
class OrderProductInfo:
    """Product info echoed on the receipt — confirms what was bought."""

    id: str | None = None
    name: str | None = None
    slug: str | None = None


@dataclass
class OrderNextSteps:
    """Next-steps block guiding the agent on what to do post-purchase."""

    user_message: str | None = None
    order_status_url: str | None = None
    fulfillment_eta: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderReceipt:
    """Order receipt returned on 200 after a successful settlement."""

    id: str
    """Stable order id — UUID, slug, or platform-native (Commerce7 order id, etc.)."""

    created_at: str
    """ISO-8601 timestamp of order creation."""

    quantity: int | None = None
    product: OrderProductInfo | None = None
    pricing: PricingBlock | None = None
    email: str | None = None

    payment_status: str | None = None
    """Typically ``"completed"``, ``"pending"``, ``"failed"``."""

    fulfillment_status: str | None = None
    """Typically ``"pending"``, ``"shipped"``, ``"delivered"``, ``"cancelled"``."""

    tracking_number: str | None = None
    """Carrier tracking number when fulfillment_status >= shipped."""

    shipping: ShippingAddress | None = None
    """Physical-goods shipping address. Omit for digital goods."""

    gift_note: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    """Vendor-specific extras merged at the top level (loyalty points, warranty, etc.)."""

    next_steps: OrderNextSteps | None = None


__all__ = ["OrderNextSteps", "OrderProductInfo", "OrderReceipt", "ShippingAddress"]
