"""Tests for ``create_pay_to_address_from_stripe_pi``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from agentscore_commerce.stripe_multichain.pay_to_address import (
    create_pay_to_address_from_stripe_pi,
)


@dataclass
class FakeRequest:
    """In-place stand-in for a pympp credential.request shape."""

    recipient: str


@dataclass
class FakeChallenge:
    method: str
    request: FakeRequest


@dataclass
class FakeCredential:
    challenge: FakeChallenge

    @classmethod
    def from_authorization(cls, auth: str) -> FakeCredential:
        payload = auth.replace("Payment ", "", 1)
        method, recipient = payload.split(":", 1)
        return cls(challenge=FakeChallenge(method=method, request=FakeRequest(recipient=recipient)))


@dataclass
class FakePiCache:
    """Minimal stand-in for the SDK's PiCache shape."""

    has_address_result: bool = False
    cached_addresses: list[str] = field(default_factory=list)
    cached_pis: list[tuple[str, str]] = field(default_factory=list)
    cached_network_addresses: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    async def cache_address(self, address: str) -> None:
        self.cached_addresses.append(address)

    async def has_address(self, _address: str) -> bool:
        return self.has_address_result

    def cache_payment_intent(self, deposit_address: str, pi_id: str) -> None:
        self.cached_pis.append((deposit_address, pi_id))

    def get_payment_intent_id(self, _addr: str) -> str | None:
        return None

    def cache_network_addresses(self, pi_id: str, addresses: dict[str, str]) -> None:
        self.cached_network_addresses.append((pi_id, addresses))

    def get_network_deposit_address(self, _pi: str, _network: str) -> str | None:
        return None

    def stop(self) -> None:
        pass


def _fake_stripe(addresses: dict[str, str]) -> Any:
    """Build a stripe-like object that returns a PI with the given deposit addresses."""

    class _PI:
        id = "pi_test_123"
        next_action = {
            "crypto_display_details": {
                "deposit_addresses": {n: {"address": a} for n, a in addresses.items()},
            },
        }

    class _PaymentIntentsAPI:
        def __init__(self) -> None:
            self.last_idempotency_key: str | None = None

        def create(self, _params: dict[str, Any], idempotency_key: str | None = None) -> Any:
            self.last_idempotency_key = idempotency_key
            return _PI()

    class _Stripe:
        def __init__(self) -> None:
            self.payment_intents = _PaymentIntentsAPI()

    return _Stripe()


@pytest.mark.asyncio
async def test_reuses_credential_recipient_when_cached() -> None:
    cache = FakePiCache(has_address_result=True)
    with patch("mpp.Credential", FakeCredential):
        result = await create_pay_to_address_from_stripe_pi(
            authorization_header="Payment tempo:0xCACHED",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
        )
    assert result == "0xCACHED"
    # No mint happened — no addresses cached.
    assert cache.cached_addresses == []


@pytest.mark.asyncio
async def test_raises_when_credential_recipient_not_in_cache() -> None:
    cache = FakePiCache(has_address_result=False)
    with patch("mpp.Credential", FakeCredential), pytest.raises(ValueError, match="not found in cache"):
        await create_pay_to_address_from_stripe_pi(
            authorization_header="Payment tempo:0xUNKNOWN",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_mints_fresh_pi_when_no_authorization_header() -> None:
    cache = FakePiCache()
    stripe = _fake_stripe({"tempo": "0xTEMPO", "base": "0xBASE", "solana": "SOLABC"})
    result = await create_pay_to_address_from_stripe_pi(
        authorization_header=None,
        amount_cents=250,
        stripe=stripe,
        pi_cache=cache,  # type: ignore[arg-type]
        order_id="order-1",
    )
    assert result == "0xTEMPO"
    assert set(cache.cached_addresses) == {"0xTEMPO", "0xBASE", "SOLABC"}
    assert cache.cached_pis == [
        ("0xTEMPO", "pi_test_123"),
        ("0xBASE", "pi_test_123"),
        ("SOLABC", "pi_test_123"),
    ]
    assert cache.cached_network_addresses == [
        ("pi_test_123", {"tempo": "0xTEMPO", "base": "0xBASE", "solana": "SOLABC"}),
    ]
    assert stripe.payment_intents.last_idempotency_key == "pi-order-1-250"


@pytest.mark.asyncio
async def test_falls_back_to_base_when_tempo_missing() -> None:
    cache = FakePiCache()
    stripe = _fake_stripe({"base": "0xBASE"})
    result = await create_pay_to_address_from_stripe_pi(
        authorization_header=None,
        amount_cents=100,
        stripe=stripe,
        pi_cache=cache,  # type: ignore[arg-type]
    )
    assert result == "0xBASE"


@pytest.mark.asyncio
async def test_mints_fresh_when_credential_method_is_stripe() -> None:
    cache = FakePiCache()
    stripe = _fake_stripe({"tempo": "0xFRESH"})
    with patch("mpp.Credential", FakeCredential):
        result = await create_pay_to_address_from_stripe_pi(
            authorization_header="Payment stripe:does-not-matter",
            amount_cents=100,
            stripe=stripe,
            pi_cache=cache,  # type: ignore[arg-type]
        )
    assert result == "0xFRESH"
