"""Per-order Stripe-multichain ``pay_to`` resolver.

Stripe-multichain merchants need ONE function for their ``mint_recipients``
(or per-request payTo) hook that does the right thing on both legs:

- **Discovery leg** (no payment header): mint a fresh PaymentIntent so the 402
  advertises a stable per-order deposit address.
- **Settle leg** (MPP credential attached): reuse the buyer's signed-against
  payTo from the credential (after verifying it's in the local cache) —
  otherwise the verify leg would compare against a freshly-rotated address
  and reject the credential.

Stripe SPT and card methods don't carry an on-chain recipient, so the settle
leg still mints a fresh PaymentIntent for them.

Mirrors node-commerce ``src/stripe-multichain/pay_to_address.ts``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agentscore_commerce.stripe_multichain.payment_intent import (
    create_multichain_payment_intent,
)

if TYPE_CHECKING:
    from agentscore_commerce.stripe_multichain.pi_cache import PiCache


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


async def create_pay_to_address_from_stripe_pi(
    *,
    authorization_header: str | None,
    amount_cents: int,
    stripe: Any,
    pi_cache: PiCache,
    networks: list[str] | None = None,
    metadata: dict[str, str] | None = None,
    order_id: str | None = None,
    preferred_network: str = "tempo",
) -> str:
    """Resolve the on-chain ``pay_to`` for a Stripe-multichain order.

    On the settle leg, when ``authorization_header`` carries an MPP credential
    binding a ``tempo`` or ``solana`` recipient, returns THAT address (after
    verifying it's still in ``pi_cache``). Otherwise mints a fresh
    :func:`create_multichain_payment_intent` and caches the addresses + PI
    mapping. Returns the address on the ``preferred_network`` (default
    ``"tempo"``, falling back to ``base`` then ``tempo``).
    """
    if authorization_header:
        from mpp import Credential  # type: ignore[import-untyped]

        if authorization_header.startswith("Payment "):
            credential = Credential.from_authorization(authorization_header)
            method = getattr(credential.challenge, "method", None)
            if method in ("tempo", "solana"):
                recipient = getattr(credential.challenge.request, "recipient", None)
                if not isinstance(recipient, str) or not recipient:
                    msg = "MPP credential challenge missing recipient field"
                    raise ValueError(msg)
                if not await _maybe_await(pi_cache.has_address(recipient)):
                    msg = "Invalid payTo address: not found in cache or expired"
                    raise ValueError(msg)
                return recipient

    idempotency_key = f"pi-{order_id}-{amount_cents}" if order_id else None
    result = create_multichain_payment_intent(
        stripe=stripe,
        amount=amount_cents,
        networks=networks or ["tempo", "base", "solana"],
        metadata=metadata,
        idempotency_key=idempotency_key,
    )

    for address in result.deposit_addresses.values():
        await _maybe_await(pi_cache.cache_address(address))
        pi_cache.cache_payment_intent(address, result.payment_intent_id)
    pi_cache.cache_network_addresses(result.payment_intent_id, result.deposit_addresses)

    pay_to = (
        result.deposit_addresses.get(preferred_network)
        or result.deposit_addresses.get("base")
        or result.deposit_addresses.get("tempo")
    )
    if not pay_to:
        msg = "Failed to resolve pay_to address from Stripe PaymentIntent"
        raise RuntimeError(msg)
    return pay_to


__all__ = ["create_pay_to_address_from_stripe_pi"]
