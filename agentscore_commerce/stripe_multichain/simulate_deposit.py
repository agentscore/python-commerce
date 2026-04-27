"""Stripe test_helpers/simulate_crypto_deposit caller — testnet helper for end-to-end exercises."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

logger = logging.getLogger("agentscore_commerce.stripe_multichain")

DEFAULT_BUYER_WALLET: dict[str, str] = {
    "base": "0x0000000000000000000000000000000000000001",
    "tempo": "0x0000000000000000000000000000000000000001",
    "solana": "11111111111111111111111111111111",
}

# Stripe's documented magic test_helpers transaction hash that resolves the
# PaymentIntent to ``succeeded`` within 15 seconds. Same value across all networks —
# Stripe normalizes the format internally. Anything else (including network-shaped
# placeholder bytes) is rejected with "not a valid testmode transaction hash".
#
# See: https://docs.stripe.com/payments/deposit-mode-stablecoin-payments
STRIPE_TEST_TX_HASH_SUCCESS = "0x00000000000000000000000000000000000000000000000000000testsuccess"

# Stripe's documented magic test_helpers transaction hash that fails the charge
# (PaymentIntent returns to ``requires_payment_method`` within 15 seconds).
STRIPE_TEST_TX_HASH_FAILED = "0x000000000000000000000000000000000000000000000000000000testfailed"


@dataclass
class SimulateCryptoDepositInput:
    payment_intent_id: str
    network: Literal["tempo", "base", "solana"]
    stripe_secret_key: str
    buyer_wallet: str | None = None
    token_currency: str | None = None
    transaction_hash: str | None = None
    stripe_version: str | None = None
    stripe_api_base: str = "https://api.stripe.com"
    extra: dict[str, str] = field(default_factory=dict)


async def simulate_crypto_deposit(input: SimulateCryptoDepositInput) -> None:
    """Call Stripe's `test_helpers/payment_intents/{id}/simulate_crypto_deposit` endpoint."""
    url = f"{input.stripe_api_base}/v1/test_helpers/payment_intents/{input.payment_intent_id}/simulate_crypto_deposit"
    params: dict[str, str] = {
        "network": input.network,
        "buyer_wallet": input.buyer_wallet or DEFAULT_BUYER_WALLET.get(input.network, ""),
    }
    if input.token_currency:
        params["token_currency"] = input.token_currency
    if input.transaction_hash:
        params["transaction_hash"] = input.transaction_hash
    params.update(input.extra)
    headers: dict[str, str] = {
        "Authorization": f"Bearer {input.stripe_secret_key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if input.stripe_version:
        headers["Stripe-Version"] = input.stripe_version
    async with httpx.AsyncClient() as client:
        res = await client.post(url, headers=headers, content="&".join(f"{k}={v}" for k, v in params.items()))
        if res.status_code >= 300:
            raise RuntimeError(f"Stripe simulate_crypto_deposit failed: {res.status_code} {res.text}")


@dataclass
class SimulateDepositIfTestModeInput:
    """Input for :func:`simulate_deposit_if_test_mode`."""

    get_payment_intent_id: Callable[[str], str | None]
    deposit_address: str
    network: Literal["tempo", "base", "solana"]
    stripe_secret_key: str
    buyer_wallet: str | None = None
    token_currency: str = "usdc"
    stripe_version: str | None = None


async def simulate_deposit_if_test_mode(input: SimulateDepositIfTestModeInput) -> None:
    """Higher-level wrapper around :func:`simulate_crypto_deposit` for the testnet/dev path.

    Bundles the three steps every Stripe-multichain merchant repeats:

    1. Gate on ``sk_test_`` key prefix — production keys reject the test_helpers endpoint
       with 400; live deposits reach Stripe's real crypto-deposit watcher instead.
    2. Resolve the PaymentIntent id from the deposit address (cache lookup).
    3. Call ``simulate_crypto_deposit`` with Stripe's documented success magic hash.

    Logs ``[stripe] ✓ Simulated <network> deposit for PI <id>`` on success and
    ``[stripe] ✗ Failed to simulate <network> deposit for PI <id>: <err>`` on failure.
    Errors are caught and logged (never raised) so a sim hiccup doesn't fail the order.

    Use case is exclusively dev/testnet end-to-end — production servers (sk_live_) no-op.
    """
    if not input.stripe_secret_key.startswith("sk_test_"):
        return
    pi_id = input.get_payment_intent_id(input.deposit_address)
    if not pi_id:
        logger.warning(
            "[stripe] Skipping deposit simulation — no PI cached for deposit address %s… (network=%s). "
            "The PI cache TTL may have expired between 402 emission and settlement.",
            input.deposit_address[:10],
            input.network,
        )
        return
    try:
        await simulate_crypto_deposit(
            SimulateCryptoDepositInput(
                payment_intent_id=pi_id,
                network=input.network,
                stripe_secret_key=input.stripe_secret_key,
                buyer_wallet=input.buyer_wallet,
                token_currency=input.token_currency,
                transaction_hash=STRIPE_TEST_TX_HASH_SUCCESS,
                stripe_version=input.stripe_version,
            )
        )
        logger.warning("[stripe] ✓ Simulated %s deposit for PI %s", input.network, pi_id)
    except Exception as err:
        logger.error(
            "[stripe] ✗ Failed to simulate %s deposit for PI %s: %s",
            input.network,
            pi_id,
            err,
        )
