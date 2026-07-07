"""Tests for ``create_pay_to_address_from_stripe_pi``."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from agentscore_commerce.stripe_multichain.pay_to_address import (
    MintMultichainRecipientsResult,
    create_pay_to_address_from_stripe_pi,
    mint_multichain_recipients,
)


def _mpp_installed() -> bool:
    try:
        return importlib.util.find_spec("mpp") is not None
    except ModuleNotFoundError:
        return False


# These tests patch mpp.Credential, so they need the optional pympp (mppx
# extra) peer dep importable; skip in minimal envs (CI installs --all-extras).
pytestmark = pytest.mark.skipif(not _mpp_installed(), reason="pympp (mpp) extra not installed")


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
    from agentscore_commerce.errors import CheckoutValidationError

    cache = FakePiCache(has_address_result=False)
    with patch("mpp.Credential", FakeCredential), pytest.raises(CheckoutValidationError) as exc:
        await create_pay_to_address_from_stripe_pi(
            authorization_header="Payment tempo:0xUNKNOWN",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
        )
    assert exc.value.code == "invalid_credential"
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_raises_when_authorization_header_is_malformed() -> None:
    from agentscore_commerce.errors import CheckoutValidationError

    class _ThrowingCredential:
        @staticmethod
        def from_authorization(_: str) -> object:
            msg = "Invalid base64url or JSON."
            raise ValueError(msg)

    cache = FakePiCache(has_address_result=True)
    with patch("mpp.Credential", _ThrowingCredential), pytest.raises(CheckoutValidationError) as exc:
        await create_pay_to_address_from_stripe_pi(
            authorization_header="Payment fake.jwt.bogus",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
        )
    assert exc.value.code == "invalid_credential"
    assert exc.value.status == 401


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


# ---------------------------------------------------------------------------
# static_recipients + mint_multichain_recipients
# ---------------------------------------------------------------------------


class _StripeRecordingNetworks:
    """Stripe stub that records which networks were requested on PI creation.

    Lets us assert that ``static_recipients``-covered networks are excluded
    from the underlying ``create_multichain_payment_intent`` call.
    """

    def __init__(self, addresses: dict[str, str]) -> None:
        self.payment_intents = self
        self._addresses = addresses
        self.last_networks: list[str] | None = None
        self.last_idempotency_key: str | None = None

    def create(self, params: dict[str, Any], idempotency_key: str | None = None) -> Any:
        self.last_networks = list(params["payment_method_options"]["crypto"]["deposit_options"]["networks"])
        self.last_idempotency_key = idempotency_key
        deposits = {n: {"address": a} for n, a in self._addresses.items()}

        class _PI:
            id = "pi_test_456"
            next_action = {"crypto_display_details": {"deposit_addresses": deposits}}

        return _PI()


@pytest.mark.asyncio
async def test_static_recipients_excluded_from_stripe_pi_networks() -> None:
    cache = FakePiCache()
    stripe = _StripeRecordingNetworks({"tempo": "0xTEMPO", "base": "0xBASE"})
    await create_pay_to_address_from_stripe_pi(
        authorization_header=None,
        amount_cents=100,
        stripe=stripe,  # type: ignore[arg-type]
        pi_cache=cache,  # type: ignore[arg-type]
        static_recipients={"solana": "STATIC123"},
    )
    assert stripe.last_networks == ["tempo", "base"]


@pytest.mark.asyncio
async def test_static_recipients_registered_in_cache_and_merged_map() -> None:
    cache = FakePiCache()
    stripe = _fake_stripe({"tempo": "0xTEMPO", "base": "0xBASE"})
    await create_pay_to_address_from_stripe_pi(
        authorization_header=None,
        amount_cents=100,
        stripe=stripe,
        pi_cache=cache,  # type: ignore[arg-type]
        static_recipients={"solana": "STATIC123"},
    )
    assert "STATIC123" in cache.cached_addresses
    assert cache.cached_network_addresses == [
        ("pi_test_123", {"tempo": "0xTEMPO", "base": "0xBASE", "solana": "STATIC123"}),
    ]


@pytest.mark.asyncio
async def test_settle_leg_accepts_static_recipient_without_cache_check() -> None:
    """A bound recipient that matches static_recipients is always accepted,
    even if the local PI cache wouldn't know about it (e.g. cold start).
    """
    cache = FakePiCache(has_address_result=False)
    with patch("mpp.Credential", FakeCredential):
        result = await create_pay_to_address_from_stripe_pi(
            authorization_header="Payment solana:STATIC123",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
            static_recipients={"solana": "STATIC123"},
        )
    assert result == "STATIC123"
    assert cache.cached_addresses == []


@pytest.mark.asyncio
async def test_settle_leg_rejects_attacker_recipient_when_static_configured() -> None:
    """Attacker forges a credential bound to a different solana address;
    static_recipients match check fails, then the cache check fails, then
    the SDK raises invalid_credential.
    """
    from agentscore_commerce.errors import CheckoutValidationError

    cache = FakePiCache(has_address_result=False)
    with (
        patch("mpp.Credential", FakeCredential),
        pytest.raises(CheckoutValidationError) as exc,
    ):
        await create_pay_to_address_from_stripe_pi(
            authorization_header="Payment solana:ATTACKER456",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
            static_recipients={"solana": "STATIC123"},
        )
    assert exc.value.code == "invalid_credential"


@pytest.mark.asyncio
async def test_mint_multichain_recipients_returns_full_merged_map() -> None:
    cache = FakePiCache()
    stripe = _fake_stripe({"tempo": "0xTEMPO", "base": "0xBASE"})
    out = await mint_multichain_recipients(
        authorization_header=None,
        amount_cents=100,
        stripe=stripe,
        pi_cache=cache,  # type: ignore[arg-type]
        static_recipients={"solana": "STATIC123"},
    )
    assert isinstance(out, MintMultichainRecipientsResult)
    assert out.recipients == {"tempo": "0xTEMPO", "base": "0xBASE", "solana": "STATIC123"}
    assert out.reused_from_credential is False


@dataclass
class FakePiCacheWithLookups(FakePiCache):
    """PiCache stand-in that resolves a PI id + per-network addresses for an
    already-known recipient, exercising the settle-leg reuse path of
    `mint_multichain_recipients`."""

    pi_id_for_addr: str = "pi_known_789"
    network_map: dict[str, str] = field(default_factory=dict)

    def get_payment_intent_id(self, addr: str) -> str | None:
        return self.pi_id_for_addr if addr else None

    def get_network_deposit_address(self, _pi: str, network: str) -> str | None:
        return self.network_map.get(network)


@pytest.mark.asyncio
async def test_mint_multichain_recipients_mints_when_credential_not_reusable() -> None:
    """A stripe-method credential isn't reusable, so mint_multichain_recipients
    falls through to the mint path (142->157)."""
    cache = FakePiCache()
    stripe = _fake_stripe({"tempo": "0xTEMPO", "base": "0xBASE"})
    with patch("mpp.Credential", FakeCredential):
        out = await mint_multichain_recipients(
            authorization_header="Payment stripe:does-not-matter",
            amount_cents=100,
            stripe=stripe,
            pi_cache=cache,  # type: ignore[arg-type]
        )
    assert out.reused_from_credential is False
    assert out.recipients == {"tempo": "0xTEMPO", "base": "0xBASE"}


@pytest.mark.asyncio
async def test_mint_multichain_recipients_reuses_credential_recipient() -> None:
    """Settle leg: the credential-bound recipient is cached, so the structured
    helper rebuilds the full per-network map from the cache + static map and
    flags `reused_from_credential=True`."""
    cache = FakePiCacheWithLookups(
        has_address_result=True,
        network_map={"tempo": "0xCACHED", "base": "0xBASE"},
    )
    with patch("mpp.Credential", FakeCredential):
        out = await mint_multichain_recipients(
            authorization_header="Payment tempo:0xCACHED",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
            static_recipients={"solana": "STATIC123"},
        )
    assert out.reused_from_credential is True
    assert out.payment_intent_id == "pi_known_789"
    assert out.recipients == {"tempo": "0xCACHED", "base": "0xBASE", "solana": "STATIC123"}


@pytest.mark.asyncio
async def test_mint_multichain_recipients_reuse_with_no_pi_id() -> None:
    """Credential reuse where the cache can't resolve a PI id: the network_map
    stays empty and only the static recipients survive in the merge."""
    cache = FakePiCacheWithLookups(has_address_result=True, pi_id_for_addr="")
    with patch("mpp.Credential", FakeCredential):
        out = await mint_multichain_recipients(
            authorization_header="Payment solana:STATIC123",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
            static_recipients={"solana": "STATIC123"},
        )
    assert out.reused_from_credential is True
    assert out.payment_intent_id == ""
    assert out.recipients == {"solana": "STATIC123"}


@pytest.mark.asyncio
async def test_credential_without_payment_prefix_falls_through_to_mint() -> None:
    """An Authorization header that isn't a `Payment ...` credential falls
    through to the mint path rather than being parsed (line 188)."""
    cache = FakePiCache()
    stripe = _fake_stripe({"tempo": "0xFRESH"})
    result = await create_pay_to_address_from_stripe_pi(
        authorization_header="Bearer not-a-payment-credential",
        amount_cents=100,
        stripe=stripe,
        pi_cache=cache,  # type: ignore[arg-type]
    )
    assert result == "0xFRESH"


@pytest.mark.asyncio
async def test_credential_missing_recipient_field_raises() -> None:
    """A credential whose challenge.request.recipient is empty raises invalid_credential."""
    from agentscore_commerce.errors import CheckoutValidationError

    cache = FakePiCache(has_address_result=True)
    with patch("mpp.Credential", FakeCredential), pytest.raises(CheckoutValidationError) as exc:
        # FakeCredential.from_authorization splits on ':' — empty recipient after method.
        await create_pay_to_address_from_stripe_pi(
            authorization_header="Payment tempo:",
            amount_cents=100,
            stripe=_fake_stripe({}),
            pi_cache=cache,  # type: ignore[arg-type]
        )
    assert exc.value.code == "invalid_credential"
    assert "missing its recipient" in exc.value.message


@pytest.mark.asyncio
async def test_mint_raises_503_when_no_matching_deposit_network() -> None:
    """Stripe returns deposit addresses but none on tempo/base/solana → 503."""
    from agentscore_commerce.errors import CheckoutValidationError

    cache = FakePiCache()
    # Only an unrelated network is returned, so the preferred/base/tempo fallback all miss.
    stripe = _fake_stripe({"polygon": "0xPOLY"})
    with pytest.raises(CheckoutValidationError) as exc:
        await create_pay_to_address_from_stripe_pi(
            authorization_header=None,
            amount_cents=100,
            stripe=stripe,
            pi_cache=cache,  # type: ignore[arg-type]
            networks=["polygon"],
        )
    assert exc.value.status == 503
    assert exc.value.code == "payment_provider_unavailable"
