"""Stripe multichain PaymentIntent helper.

Creates a PaymentIntent with `payment_method_options.crypto.deposit_options.networks` set to multiple
chains, returning the PI id + deposit addresses per network. Distinct from the Stripe SPT flow.
"""

from dataclasses import dataclass
from typing import Any, Protocol


class StripePaymentIntentsAPI(Protocol):
    def create(self, params: dict[str, Any], idempotency_key: str | None = None) -> Any: ...


class StripeClientLike(Protocol):
    payment_intents: StripePaymentIntentsAPI


@dataclass
class MultichainPaymentIntentResult:
    payment_intent_id: str
    deposit_addresses: dict[str, str]


_DEFAULT_NETWORKS: tuple[str, ...] = ("tempo", "base", "solana")


def create_multichain_payment_intent(
    *,
    stripe: Any,  # StripeClientLike, kept loose so vendors can pass their actual `stripe.StripeClient`
    amount: int,  # in cents (Stripe convention)
    currency: str = "usd",
    networks: list[str] | None = None,
    metadata: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> MultichainPaymentIntentResult:
    """Create a Stripe PaymentIntent with multichain crypto deposit_options.

    Returns the PI id + per-network deposit addresses. Raises if Stripe doesn't return any addresses.
    """
    resolved_networks = list(networks) if networks else list(_DEFAULT_NETWORKS)
    params: dict[str, Any] = {
        "amount": amount,
        "currency": currency,
        "payment_method_types": ["crypto"],
        "payment_method_data": {"type": "crypto"},
        "payment_method_options": {
            "crypto": {"mode": "deposit", "deposit_options": {"networks": resolved_networks}},
        },
        "confirm": True,
    }
    if metadata:
        params["metadata"] = metadata

    pi = (
        stripe.payment_intents.create(params, idempotency_key=idempotency_key)
        if idempotency_key
        else stripe.payment_intents.create(params)
    )
    deposit_addresses: dict[str, str] = {}
    next_action = getattr(pi, "next_action", None) or (pi.get("next_action") if isinstance(pi, dict) else None)
    if next_action:
        crypto_details = getattr(next_action, "crypto_display_details", None) or (
            next_action.get("crypto_display_details") if isinstance(next_action, dict) else None
        )
        if crypto_details:
            addrs = (
                getattr(crypto_details, "deposit_addresses", None)
                or (crypto_details.get("deposit_addresses") if isinstance(crypto_details, dict) else None)
                or {}
            )
            for network, info in addrs.items():
                addr = (
                    getattr(info, "address", None)
                    if info is not None and not isinstance(info, dict)
                    else (info.get("address") if isinstance(info, dict) else None)
                )
                if addr:
                    deposit_addresses[network] = addr

    if not deposit_addresses:
        raise RuntimeError("No deposit addresses returned from Stripe PaymentIntent")

    pi_id = getattr(pi, "id", None) or (pi.get("id") if isinstance(pi, dict) else None)
    if not isinstance(pi_id, str):
        raise RuntimeError("Stripe PaymentIntent missing id field")
    return MultichainPaymentIntentResult(payment_intent_id=pi_id, deposit_addresses=deposit_addresses)
