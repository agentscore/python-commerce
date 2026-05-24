"""Per-order Stripe-multichain ``pay_to`` resolver.

Stripe-multichain merchants need ONE function for their ``mint_recipients``
(or per-request payTo) hook that does the right thing on both legs:

- **Discovery leg** (no payment header): mint a fresh PaymentIntent so the 402
  advertises a stable per-order deposit address.
- **Settle leg** (MPP credential attached): reuse the buyer's signed-against
  payTo from the credential (after verifying it's in the local cache OR matches
  a configured ``static_recipients`` entry) — otherwise the verify leg would
  compare against a freshly-rotated address and reject the credential.

Stripe SPT and card methods don't carry an on-chain recipient, so the settle
leg still mints a fresh PaymentIntent for them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentscore_commerce.errors import CheckoutValidationError
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
    static_recipients: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
    order_id: str | None = None,
    preferred_network: str = "tempo",
) -> str:
    """Resolve the on-chain ``pay_to`` for a Stripe-multichain order.

    On the settle leg, when ``authorization_header`` carries an MPP credential
    binding a ``tempo`` or ``solana`` recipient, returns THAT address (after
    verifying it's in ``pi_cache`` OR matches a configured ``static_recipients``
    entry — static addresses are always-accepted because the merchant owns
    them). Otherwise mints a fresh :func:`create_multichain_payment_intent`
    for the rails NOT covered by ``static_recipients``, caches the merged
    address map, and registers static recipients with ``pi_cache.cache_address``
    so future verify-leg lookups pass. Returns the address on the
    ``preferred_network`` (default ``"tempo"``, falling back to ``base`` then
    ``tempo``).

    ``static_recipients`` (optional, keyed by network) lets the merchant pin a
    fixed receive wallet on chains where per-call rotation is expensive — Solana
    in particular, since MPP spec §13.6 charges ~0.002 SOL of ATA rent per
    new recipient into accounts the merchant can't close. Example:
    ``static_recipients={"solana": "FR96wd96urH..."}``. The SDK skips Stripe
    minting for that network so the static address is reused forever; pair
    with a one-time external USDC transfer to pre-create the recipient's USDC
    ATA and every settle pays only the per-tx fee.
    """
    if authorization_header:
        recipient = await _try_resolve_from_credential(
            authorization_header=authorization_header,
            pi_cache=pi_cache,
            static_recipients=static_recipients or {},
        )
        if recipient is not None:
            return recipient

    result_payto, _ = await _mint_and_cache(
        amount_cents=amount_cents,
        stripe=stripe,
        pi_cache=pi_cache,
        networks=networks or ["tempo", "base", "solana"],
        static_recipients=static_recipients or {},
        metadata=metadata,
        order_id=order_id,
        preferred_network=preferred_network,
    )
    return result_payto


@dataclass
class MintMultichainRecipientsResult:
    """Structured result for :func:`mint_multichain_recipients`.

    Exposes the full per-network deposit map plus the PI id, so merchants don't
    have to guess "is the returned string the tempo address or the solana
    static?" when ``static_recipients`` is configured.
    """

    recipients: dict[str, str]
    """Per-network deposit address map (merges Stripe-minted + static_recipients)."""

    payment_intent_id: str | None
    """Stripe PI id, or None if all networks were covered by ``static_recipients``."""

    reused_from_credential: bool
    """True when the settle leg short-circuited to the credential-bound recipient."""


async def mint_multichain_recipients(
    *,
    authorization_header: str | None,
    amount_cents: int,
    stripe: Any,
    pi_cache: PiCache,
    networks: list[str] | None = None,
    static_recipients: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
    order_id: str | None = None,
    preferred_network: str = "tempo",
) -> MintMultichainRecipientsResult:
    """Structured variant of :func:`create_pay_to_address_from_stripe_pi`.

    Returns the full per-rail deposit map. Prefer this when the merchant's
    ``mint_recipients`` hook needs every rail's address (typical multi-rail
    merchant) — avoids the "returned string is ambiguous" trap when
    ``static_recipients`` is configured (settle leg's bound recipient may be
    the solana static rather than the tempo per-PI address).
    """
    static = static_recipients or {}
    if authorization_header:
        from agentscore_commerce.stripe_multichain.pi_cache import PiCache  # noqa: F401

        recipient = await _try_resolve_from_credential(
            authorization_header=authorization_header,
            pi_cache=pi_cache,
            static_recipients=static,
        )
        if recipient is not None:
            pi_id = pi_cache.get_payment_intent_id(recipient)
            network_map: dict[str, str] = {}
            if pi_id:
                for net in ("tempo", "base", "solana"):
                    addr = pi_cache.get_network_deposit_address(pi_id, net)
                    if addr:
                        network_map[net] = addr
            merged = {**network_map, **static}
            return MintMultichainRecipientsResult(
                recipients=merged,
                payment_intent_id=pi_id,
                reused_from_credential=True,
            )

    _, merged = await _mint_and_cache(
        amount_cents=amount_cents,
        stripe=stripe,
        pi_cache=pi_cache,
        networks=networks or ["tempo", "base", "solana"],
        static_recipients=static,
        metadata=metadata,
        order_id=order_id,
        preferred_network=preferred_network,
    )
    pi_id = pi_cache.get_payment_intent_id(next(iter(merged.values()), ""))
    return MintMultichainRecipientsResult(
        recipients=merged,
        payment_intent_id=pi_id,
        reused_from_credential=False,
    )


async def _try_resolve_from_credential(
    *,
    authorization_header: str,
    pi_cache: PiCache,
    static_recipients: dict[str, str],
) -> str | None:
    """Parse the MPP credential and return the bound recipient when valid.

    Returns the address when it's cached OR matches a configured
    ``static_recipients`` entry, or None to fall through to the mint path.
    Raises CheckoutValidationError on malformed credentials.
    """
    if not authorization_header.startswith("Payment "):
        return None
    from mpp import Credential  # type: ignore[import-untyped]

    try:
        credential = Credential.from_authorization(authorization_header)
    except Exception as err:
        raise CheckoutValidationError(
            code="invalid_credential",
            message="The Authorization: Payment header is not a valid MPP credential.",
            action="retry_without_credential",
            status=401,
        ) from err
    method = getattr(credential.challenge, "method", None)
    if method not in ("tempo", "solana"):
        return None
    recipient = getattr(credential.challenge.request, "recipient", None)
    if not isinstance(recipient, str) or not recipient:
        raise CheckoutValidationError(
            code="invalid_credential",
            message="The MPP credential is missing its recipient field.",
            action="retry_without_credential",
            status=401,
        )
    static_for_method = static_recipients.get(method)
    if static_for_method and static_for_method == recipient:
        return recipient
    if not await _maybe_await(pi_cache.has_address(recipient)):
        raise CheckoutValidationError(
            code="invalid_credential",
            message=(
                "The signed-against payTo recipient is not in this merchant's cache "
                "(unknown or expired). Retry without the Authorization: Payment header "
                "to receive a fresh 402 challenge."
            ),
            action="retry_without_credential",
            status=401,
        )
    return recipient


async def _mint_and_cache(
    *,
    amount_cents: int,
    stripe: Any,
    pi_cache: PiCache,
    networks: list[str],
    static_recipients: dict[str, str],
    metadata: dict[str, str] | None,
    order_id: str | None,
    preferred_network: str,
) -> tuple[str, dict[str, str]]:
    """Mint a fresh PI for rails not covered by static_recipients.

    Registers everything in the cache and returns (preferred_address, merged_map).
    """
    stripe_networks = [n for n in networks if n not in static_recipients]
    idempotency_key = f"pi-{order_id}-{amount_cents}" if order_id else None
    result = create_multichain_payment_intent(
        stripe=stripe,
        amount=amount_cents,
        networks=stripe_networks,
        metadata=metadata,
        idempotency_key=idempotency_key,
    )
    for address in result.deposit_addresses.values():
        await _maybe_await(pi_cache.cache_address(address))
        pi_cache.cache_payment_intent(address, result.payment_intent_id)
    for address in static_recipients.values():
        await _maybe_await(pi_cache.cache_address(address))
    merged: dict[str, str] = {**result.deposit_addresses, **static_recipients}
    pi_cache.cache_network_addresses(result.payment_intent_id, merged)

    pay_to = merged.get(preferred_network) or merged.get("base") or merged.get("tempo")
    if not pay_to:
        raise CheckoutValidationError(
            code="payment_provider_unavailable",
            message=(
                "Stripe returned deposit addresses but none matched the requested network (tempo / base / solana). "
                "The account may have only a subset of multichain networks enabled."
            ),
            action="retry_later",
            status=503,
        )
    return pay_to, merged


__all__ = [
    "MintMultichainRecipientsResult",
    "create_pay_to_address_from_stripe_pi",
    "mint_multichain_recipients",
]
