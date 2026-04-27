"""Tests for the helpers lifted into commerce: pi_cache, simulate_deposit_if_test_mode, respond_402, process_x402_settle, validate_x402_network_config, verify_x402_request."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable

import pytest

from agentscore_commerce.challenge import (
    Build402BodyInput,
    Respond402Input,
    respond_402,
)
from agentscore_commerce.payment import (
    X402_SUPPORTED_BASE_NETWORKS,
    X402_SUPPORTED_SVM_NETWORKS,
    PaymentRequiredHeaderInput,
    ProcessX402SettleFailure,
    ProcessX402SettleInput,
    ProcessX402SettleSuccess,
    ValidateX402NetworkConfigInput,
    VerifyX402RequestFailure,
    VerifyX402RequestInput,
    VerifyX402RequestSuccess,
    networks,
    process_x402_settle,
    validate_x402_network_config,
    verify_x402_request,
)
from agentscore_commerce.stripe_multichain import (
    STRIPE_TEST_TX_HASH_FAILED,
    STRIPE_TEST_TX_HASH_SUCCESS,
    PiCacheOptions,
    SimulateDepositIfTestModeInput,
    create_pi_cache,
    simulate_deposit_if_test_mode,
)

# ─────────────────────────────────────────────────────────────────────────────
# pi_cache
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pi_cache_address_round_trip():
    cache = create_pi_cache(PiCacheOptions(ttl_seconds=10))
    assert await cache.has_address("0xdeadbeef") is False
    await cache.cache_address("0xdeadbeef")
    assert await cache.has_address("0xdeadbeef") is True
    cache.stop()


def test_pi_cache_payment_intent_round_trip():
    cache = create_pi_cache(PiCacheOptions(ttl_seconds=10))
    assert cache.get_payment_intent_id("0xaddr") is None
    cache.cache_payment_intent("0xaddr", "pi_test_123")
    assert cache.get_payment_intent_id("0xaddr") == "pi_test_123"
    cache.stop()


def test_pi_cache_network_addresses_round_trip():
    cache = create_pi_cache(PiCacheOptions(ttl_seconds=10))
    cache.cache_network_addresses("pi_test", {"base": "0xbase", "solana": "Gso1ana"})
    assert cache.get_network_deposit_address("pi_test", "base") == "0xbase"
    assert cache.get_network_deposit_address("pi_test", "solana") == "Gso1ana"
    assert cache.get_network_deposit_address("pi_test", "tempo") is None
    assert cache.get_network_deposit_address("pi_unknown", "base") is None
    cache.stop()


def test_pi_cache_ttl_eviction_via_expired_entries():
    cache = create_pi_cache(PiCacheOptions(ttl_seconds=0))
    cache.cache_payment_intent("0xaddr", "pi_short")
    # ttl=0 means expires_at == now; subsequent get returns None
    time.sleep(0.01)
    assert cache.get_payment_intent_id("0xaddr") is None

    cache.cache_network_addresses("pi_short", {"base": "0xbase"})
    time.sleep(0.01)
    assert cache.get_network_deposit_address("pi_short", "base") is None
    cache.stop()


@pytest.mark.asyncio
async def test_pi_cache_no_redis_url_falls_back_to_memory_only():
    cache = create_pi_cache()
    await cache.cache_address("0xnoredis")
    assert await cache.has_address("0xnoredis") is True
    cache.stop()


# ─────────────────────────────────────────────────────────────────────────────
# simulate_deposit_if_test_mode
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_simulate_deposit_skips_on_live_key():
    called: list[str] = []
    await simulate_deposit_if_test_mode(
        SimulateDepositIfTestModeInput(
            get_payment_intent_id=lambda addr: called.append(addr) or "pi_x",
            deposit_address="0xaddr",
            network="base",
            stripe_secret_key="sk_live_real_one",
        )
    )
    # Should never even look up the PI on a live key
    assert called == []


@pytest.mark.asyncio
async def test_simulate_deposit_no_pi_warns_and_returns():
    await simulate_deposit_if_test_mode(
        SimulateDepositIfTestModeInput(
            get_payment_intent_id=lambda _addr: None,
            deposit_address="0xaddr",
            network="base",
            stripe_secret_key="sk_test_xyz",
        )
    )
    # No exception; warning logged (not asserted here, would need caplog)


def test_stripe_test_tx_hashes_documented():
    assert STRIPE_TEST_TX_HASH_SUCCESS.endswith("testsuccess")
    assert STRIPE_TEST_TX_HASH_FAILED.endswith("testfailed")
    assert STRIPE_TEST_TX_HASH_SUCCESS.startswith("0x")
    assert STRIPE_TEST_TX_HASH_FAILED.startswith("0x")


# ─────────────────────────────────────────────────────────────────────────────
# respond_402
# ─────────────────────────────────────────────────────────────────────────────


def test_respond_402_preserves_mppx_www_authenticate():
    result = respond_402(
        Respond402Input(
            mppx_challenge_headers={
                "WWW-Authenticate": 'Payment id="ord_x", method="tempo", request="..."',
                "Content-Type": "application/json",
            },
            body=Build402BodyInput(accepted_methods=[{"method": "tempo/charge"}]),
        )
    )
    assert result.status == 402
    assert "tempo" in result.headers["www-authenticate"]
    assert result.headers["content-type"] == "application/json"
    assert result.body["accepted_methods"] == [{"method": "tempo/charge"}]
    # No PAYMENT-REQUIRED unless x402 was passed
    assert "payment-required" not in result.headers


def test_respond_402_layers_payment_required_when_x402_set():
    result = respond_402(
        Respond402Input(
            mppx_challenge_headers={"www-authenticate": 'Payment id="ord_y"'},
            body=Build402BodyInput(accepted_methods=[]),
            x402=PaymentRequiredHeaderInput(
                x402_version=2,
                accepts=[{"scheme": "exact", "network": "eip155:84532"}],
                resource={"url": "https://x.example/y", "mimeType": "application/json"},
            ),
        )
    )
    assert "payment-required" in result.headers
    decoded = json.loads(base64.b64decode(result.headers["payment-required"]).decode())
    assert decoded["x402Version"] == 2
    assert decoded["accepts"][0]["network"] == "eip155:84532"


# ─────────────────────────────────────────────────────────────────────────────
# validate_x402_network_config
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_x402_accepts_supported_combo():
    validate_x402_network_config(
        ValidateX402NetworkConfigInput(
            base_network=networks.base.sepolia.caip2,
            svm_network=networks.solana.devnet.caip2,
        )
    )


def test_validate_x402_rejects_unknown_base():
    with pytest.raises(ValueError, match="X402_BASE_NETWORK=eip155:9999"):
        validate_x402_network_config(
            ValidateX402NetworkConfigInput(base_network="eip155:9999", svm_network=networks.solana.devnet.caip2)
        )


def test_validate_x402_rejects_unknown_svm():
    with pytest.raises(ValueError, match="X402_SVM_NETWORK=solana:bogus"):
        validate_x402_network_config(
            ValidateX402NetworkConfigInput(base_network=networks.base.sepolia.caip2, svm_network="solana:bogus")
        )


def test_x402_supported_networks_constants():
    assert networks.base.mainnet.caip2 in X402_SUPPORTED_BASE_NETWORKS
    assert networks.base.sepolia.caip2 in X402_SUPPORTED_BASE_NETWORKS
    assert networks.solana.mainnet.caip2 in X402_SUPPORTED_SVM_NETWORKS
    assert networks.solana.devnet.caip2 in X402_SUPPORTED_SVM_NETWORKS


# ─────────────────────────────────────────────────────────────────────────────
# verify_x402_request
# ─────────────────────────────────────────────────────────────────────────────


async def _always_true(_addr: str) -> bool:
    return True


async def _always_false(_addr: str) -> bool:
    return False


def _x_payment(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.mark.asyncio
async def test_verify_x402_missing_header():
    res = await verify_x402_request(
        VerifyX402RequestInput(
            headers={},
            is_cached_address=_always_true,
            accepted_base_network=networks.base.sepolia.caip2,
            accepted_svm_network=networks.solana.devnet.caip2,
        )
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "missing" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_bad_base64():
    res = await verify_x402_request(
        VerifyX402RequestInput(
            headers={"X-Payment": "not-base64-json"},
            is_cached_address=_always_true,
            accepted_base_network=networks.base.sepolia.caip2,
            accepted_svm_network=networks.solana.devnet.caip2,
        )
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "valid base64" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_unsupported_network():
    payload = {"accepted": {"network": "eip155:9999", "payTo": "0x" + "a" * 40}}
    res = await verify_x402_request(
        VerifyX402RequestInput(
            headers={"x-payment": _x_payment(payload)},
            is_cached_address=_always_true,
            accepted_base_network=networks.base.sepolia.caip2,
            accepted_svm_network=networks.solana.devnet.caip2,
        )
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "Unsupported x402 network" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_malformed_evm_pay_to():
    payload = {"accepted": {"network": networks.base.sepolia.caip2, "payTo": "not-an-address"}}
    res = await verify_x402_request(
        VerifyX402RequestInput(
            headers={"x-payment": _x_payment(payload)},
            is_cached_address=_always_true,
            accepted_base_network=networks.base.sepolia.caip2,
            accepted_svm_network=networks.solana.devnet.caip2,
        )
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "malformed accepted.payTo" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_pay_to_not_in_cache():
    payload = {"accepted": {"network": networks.base.sepolia.caip2, "payTo": "0x" + "f" * 40}}
    res = await verify_x402_request(
        VerifyX402RequestInput(
            headers={"x-payment": _x_payment(payload)},
            is_cached_address=_always_false,
            accepted_base_network=networks.base.sepolia.caip2,
            accepted_svm_network=networks.solana.devnet.caip2,
        )
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "not found in cache" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_success_evm():
    pay_to = "0x" + "1" * 40
    payload = {"accepted": {"network": networks.base.sepolia.caip2, "payTo": pay_to}}
    res = await verify_x402_request(
        VerifyX402RequestInput(
            headers={"x-payment": _x_payment(payload)},
            is_cached_address=_always_true,
            accepted_base_network=networks.base.sepolia.caip2,
            accepted_svm_network=networks.solana.devnet.caip2,
        )
    )
    assert isinstance(res, VerifyX402RequestSuccess)
    assert res.signed_pay_to == pay_to
    assert res.signed_network == networks.base.sepolia.caip2
    assert res.is_solana is False


@pytest.mark.asyncio
async def test_verify_x402_success_solana():
    # Real-shape Solana base58 (System Program address)
    pay_to = "11111111111111111111111111111111"
    payload = {"accepted": {"network": networks.solana.devnet.caip2, "payTo": pay_to}}
    res = await verify_x402_request(
        VerifyX402RequestInput(
            headers={"x-payment": _x_payment(payload)},
            is_cached_address=_always_true,
            accepted_base_network=networks.base.sepolia.caip2,
            accepted_svm_network=networks.solana.devnet.caip2,
        )
    )
    assert isinstance(res, VerifyX402RequestSuccess)
    assert res.signed_pay_to == pay_to
    assert res.is_solana is True


# ─────────────────────────────────────────────────────────────────────────────
# process_x402_settle
# ─────────────────────────────────────────────────────────────────────────────


class _FakeServer:
    def __init__(
        self,
        requirements: list,
        verify_result: dict,
        settle_result: object | Exception | None = None,
    ) -> None:
        self.requirements = requirements
        self.verify_result = verify_result
        self.settle_result = settle_result

    async def build_payment_requirements(self, _cfg: object) -> list:
        return self.requirements

    def enrich_extensions(self, ext: object, _ctx: object) -> object:
        return ext

    async def process_payment_request(self, _payload: object, _cfg: object, _meta: object, _ext: object) -> dict:
        return self.verify_result

    async def settle_payment(self, _payload: object, _req: object) -> object:
        if isinstance(self.settle_result, Exception):
            raise self.settle_result
        return self.settle_result


_RESOURCE_META = {"url": "http://localhost/x", "description": "d", "mimeType": "application/json"}


@pytest.mark.asyncio
async def test_process_x402_settle_no_requirements():
    server = _FakeServer(requirements=[], verify_result={"success": True})
    res = await process_x402_settle(
        ProcessX402SettleInput(
            x402_server=server,
            payload={},
            resource_config={},
            resource_meta=_RESOURCE_META,
        )
    )
    assert isinstance(res, ProcessX402SettleFailure)
    assert res.phase == "no_requirements"


@pytest.mark.asyncio
async def test_process_x402_settle_verify_failed():
    server = _FakeServer(requirements=[{"id": "req1"}], verify_result={"success": False, "error": "bad sig"})
    res = await process_x402_settle(
        ProcessX402SettleInput(
            x402_server=server,
            payload={},
            resource_config={},
            resource_meta=_RESOURCE_META,
        )
    )
    assert isinstance(res, ProcessX402SettleFailure)
    assert res.phase == "verify_failed"


@pytest.mark.asyncio
async def test_process_x402_settle_settle_failed():
    server = _FakeServer(
        requirements=[{"id": "req1"}],
        verify_result={"success": True},
        settle_result=RuntimeError("facilitator timeout"),
    )
    res = await process_x402_settle(
        ProcessX402SettleInput(
            x402_server=server,
            payload={},
            resource_config={},
            resource_meta=_RESOURCE_META,
        )
    )
    assert isinstance(res, ProcessX402SettleFailure)
    assert res.phase == "settle_failed"
    assert isinstance(res.error, RuntimeError)


@pytest.mark.asyncio
async def test_process_x402_settle_success_returns_payment_response_header():
    server = _FakeServer(
        requirements=[{"id": "req1"}],
        verify_result={"success": True},
        settle_result={"tx_hash": "0xabc", "amount": "110000"},
    )
    res = await process_x402_settle(
        ProcessX402SettleInput(
            x402_server=server,
            payload={},
            resource_config={},
            resource_meta=_RESOURCE_META,
            extension={"name": "bazaar"},
        )
    )
    assert isinstance(res, ProcessX402SettleSuccess)
    assert res.matched_requirement == {"id": "req1"}
    assert res.settle_result == {"tx_hash": "0xabc", "amount": "110000"}
    assert res.payment_response_header is not None
    decoded = json.loads(base64.b64decode(res.payment_response_header).decode())
    assert decoded["tx_hash"] == "0xabc"


# Required for asyncio fixtures
@pytest.fixture
def _ensure_loop() -> None:
    asyncio.get_event_loop()
    return


_ = (Awaitable,)  # reference imported names so noqa lines aren't needed
