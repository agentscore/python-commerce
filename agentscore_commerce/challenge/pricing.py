"""Pricing block builder + canonical type.

Composes cents-denominated price components into the dollar-string shape that 402
challenge bodies advertise. Standardizes the pricing block so every merchant —
current and future commerce-platform plugins (Commerce7, WooCommerce, Shopify) —
surfaces the same shape to agents.

Shipping is included by default because most physical-goods merchants carry it; pass
``shipping_cents=0`` (or omit) for digital goods / services. Tax is optional for
merchants outside taxable jurisdictions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PricingBlock:
    """Pricing breakdown advertised on 402 challenges + receipts.

    All money fields are dollar-strings (e.g. ``"250.00"``). Use :func:`build_pricing_block`
    to compose from cents and avoid floating-point drift.
    """

    subtotal: str
    """Pre-tax, pre-shipping subtotal."""

    tax: str
    """Tax amount. Always present even if ``"0.00"``."""

    total: str
    """Final total = subtotal + tax + shipping."""

    shipping: str | None = None
    """Shipping cost. Omit for digital goods / services."""

    tax_rate: float | None = None
    """Tax rate as a decimal fraction (e.g. ``0.0775`` for 7.75%). Omit for tax-free merchants."""

    tax_state: str | None = None
    """ISO-3166-2 state code or jurisdiction name used for tax calc."""

    currency: str | None = None
    """ISO-4217 currency code. Default ``"USD"`` (omitted means default)."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the dict shape advertised on the 402 body."""
        out: dict[str, Any] = {
            "subtotal": self.subtotal,
            "tax": self.tax,
            "total": self.total,
        }
        if self.shipping is not None:
            out["shipping"] = self.shipping
        if self.tax_rate is not None:
            out["tax_rate"] = self.tax_rate
        if self.tax_state is not None:
            out["tax_state"] = self.tax_state
        if self.currency is not None:
            out["currency"] = self.currency
        return out


def build_pricing_block(
    subtotal_cents: int,
    tax_cents: int = 0,
    shipping_cents: int | None = None,
    total_cents: int | None = None,
    tax_rate: float | None = None,
    tax_state: str | None = None,
    currency: str | None = None,
) -> PricingBlock:
    """Compose a :class:`PricingBlock` from cents-denominated inputs.

    Handles the cents → dollar-string conversion (always 2 decimals) and computes the total
    when not explicitly provided.

    Example::

        pricing = build_pricing_block(
            subtotal_cents=25000,
            tax_cents=1875,
            shipping_cents=999,
            tax_rate=0.075,
            tax_state="CA",
        )
        # → PricingBlock(subtotal="250.00", tax="18.75", shipping="9.99", total="278.74", ...)

    Pass ``shipping_cents=0`` for digital goods if you want the field present (it's then
    ``"0.00"``); pass ``None`` (or omit) if you don't want shipping in the response shape
    at all.
    """
    shipping = shipping_cents if shipping_cents is not None else 0
    total = total_cents if total_cents is not None else subtotal_cents + tax_cents + shipping

    return PricingBlock(
        subtotal=_format_cents(subtotal_cents),
        tax=_format_cents(tax_cents),
        total=_format_cents(total),
        shipping=_format_cents(shipping) if shipping_cents is not None else None,
        tax_rate=tax_rate,
        tax_state=tax_state,
        currency=currency,
    )


def _format_cents(cents: int) -> str:
    return f"{cents / 100:.2f}"


__all__ = ["PricingBlock", "build_pricing_block"]
