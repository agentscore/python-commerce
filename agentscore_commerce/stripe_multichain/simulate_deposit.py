"""Stripe test_helpers/simulate_crypto_deposit caller — testnet helper for end-to-end exercises."""

from dataclasses import dataclass, field
from typing import Literal

import httpx

DEFAULT_BUYER_WALLET: dict[str, str] = {
    "base": "0x0000000000000000000000000000000000000001",
    "tempo": "0x0000000000000000000000000000000000000001",
    "solana": "11111111111111111111111111111111",
}


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
