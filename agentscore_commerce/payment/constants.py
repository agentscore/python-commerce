"""Shared payment-layer constants."""

from __future__ import annotations

from decimal import Decimal

STRIPE_MIN_CHARGE_USD: Decimal = Decimal("0.50")
"""Stripe's documented USD minimum charge.

Stripe's fixed ~$0.30 processing fee makes sub-50-cent charges unprofitable
(a $0.11 PI nets -$0.19 after fees); many accounts also reject PI creation
under the floor with ``amount_too_small``. The SDK auto-drops the
``stripe/charge`` rail from BOTH the 402 body's ``accepted_methods`` AND the
per-call mppx compose intents when the priced amount falls below this
threshold, so agents never see a rail they can't profitably use.
"""
