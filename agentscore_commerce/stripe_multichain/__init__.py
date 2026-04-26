"""Stripe multichain helpers — PaymentIntent with deposit_options + testnet simulator."""

from agentscore_commerce.stripe_multichain.payment_intent import (
    CreateMultichainPaymentIntentInput,
    MultichainPaymentIntentResult,
    StripeClientLike,
    create_multichain_payment_intent,
    get_deposit_address,
)
from agentscore_commerce.stripe_multichain.simulate_deposit import (
    DEFAULT_BUYER_WALLET,
    SimulateCryptoDepositInput,
    simulate_crypto_deposit,
)

__all__ = [
    "DEFAULT_BUYER_WALLET",
    "CreateMultichainPaymentIntentInput",
    "MultichainPaymentIntentResult",
    "SimulateCryptoDepositInput",
    "StripeClientLike",
    "create_multichain_payment_intent",
    "get_deposit_address",
    "simulate_crypto_deposit",
]
