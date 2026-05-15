"""Tests for the helpers lifted into commerce: pi_cache, simulate_deposit_if_test_mode, respond_402, process_x402_settle, validate_x402_network_config, verify_x402_request."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable

import pytest

from agentscore_commerce.challenge import build_402_body, respond_402
from agentscore_commerce.payment import (
    X402_SUPPORTED_BASE_NETWORKS,
    ProcessX402SettleFailure,
    ProcessX402SettleSuccess,
    VerifyX402RequestFailure,
    VerifyX402RequestSuccess,
    classify_x402_settle_result,
    networks,
    process_x402_settle,
    validate_x402_network_config,
    verify_x402_request,
)
from agentscore_commerce.stripe_multichain import (
    STRIPE_TEST_TX_HASH_FAILED,
    STRIPE_TEST_TX_HASH_SUCCESS,
    create_pi_cache,
    simulate_deposit_if_test_mode,
)

# ─────────────────────────────────────────────────────────────────────────────
# pi_cache
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pi_cache_address_round_trip():
    cache = create_pi_cache(ttl_seconds=10)
    assert await cache.has_address("0xdeadbeef") is False
    await cache.cache_address("0xdeadbeef")
    assert await cache.has_address("0xdeadbeef") is True
    cache.stop()


def test_pi_cache_payment_intent_round_trip():
    cache = create_pi_cache(ttl_seconds=10)
    assert cache.get_payment_intent_id("0xaddr") is None
    cache.cache_payment_intent("0xaddr", "pi_test_123")
    assert cache.get_payment_intent_id("0xaddr") == "pi_test_123"
    cache.stop()


def test_pi_cache_network_addresses_round_trip():
    cache = create_pi_cache(ttl_seconds=10)
    cache.cache_network_addresses("pi_test", {"base": "0xbase", "solana": "Gso1ana"})
    assert cache.get_network_deposit_address("pi_test", "base") == "0xbase"
    assert cache.get_network_deposit_address("pi_test", "solana") == "Gso1ana"
    assert cache.get_network_deposit_address("pi_test", "tempo") is None
    assert cache.get_network_deposit_address("pi_unknown", "base") is None
    cache.stop()


def test_pi_cache_ttl_eviction_via_expired_entries():
    cache = create_pi_cache(ttl_seconds=0)
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
        get_payment_intent_id=lambda addr: called.append(addr) or "pi_x",
        deposit_address="0xaddr",
        network="base",
        stripe_secret_key="sk_live_real_one",
    )
    # Should never even look up the PI on a live key
    assert called == []


@pytest.mark.asyncio
async def test_simulate_deposit_no_pi_warns_and_returns():
    await simulate_deposit_if_test_mode(
        get_payment_intent_id=lambda _addr: None,
        deposit_address="0xaddr",
        network="base",
        stripe_secret_key="sk_test_xyz",
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
        mppx_challenge_headers={
            "WWW-Authenticate": 'Payment id="ord_x", method="tempo", request="..."',
            "Content-Type": "application/json",
        },
        body=build_402_body(accepted_methods=[{"method": "tempo/charge"}]),
    )
    assert result.status == 402
    assert "tempo" in result.headers["www-authenticate"]
    assert result.headers["content-type"] == "application/json"
    assert result.body["accepted_methods"] == [{"method": "tempo/charge"}]
    # No PAYMENT-REQUIRED unless x402 was passed
    assert "payment-required" not in result.headers


def test_respond_402_layers_payment_required_when_x402_set():
    result = respond_402(
        mppx_challenge_headers={"www-authenticate": 'Payment id="ord_y"'},
        body=build_402_body(accepted_methods=[]),
        x402={
            "x402_version": 2,
            "accepts": [{"scheme": "exact", "network": "eip155:84532"}],
            "resource": {"url": "https://x.example/y", "mimeType": "application/json"},
        },
    )
    assert "payment-required" in result.headers
    decoded = json.loads(base64.b64decode(result.headers["payment-required"]).decode())
    assert decoded["x402Version"] == 2
    assert decoded["accepts"][0]["network"] == "eip155:84532"


# ─────────────────────────────────────────────────────────────────────────────
# validate_x402_network_config
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_x402_accepts_supported_base():
    validate_x402_network_config(base_network=networks.base.sepolia.caip2)


def test_validate_x402_rejects_unknown_base():
    with pytest.raises(ValueError, match="X402_BASE_NETWORK=eip155:9999"):
        validate_x402_network_config(base_network="eip155:9999")


def test_x402_supported_networks_constants():
    assert networks.base.mainnet.caip2 in X402_SUPPORTED_BASE_NETWORKS
    assert networks.base.sepolia.caip2 in X402_SUPPORTED_BASE_NETWORKS


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
        headers={},
        is_cached_address=_always_true,
        accepted_network=networks.base.sepolia.caip2,
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "missing" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_bad_base64():
    res = await verify_x402_request(
        headers={"X-Payment": "not-base64-json"},
        is_cached_address=_always_true,
        accepted_network=networks.base.sepolia.caip2,
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "valid base64" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_unsupported_network():
    payload = {"accepted": {"network": "eip155:9999", "payTo": "0x" + "a" * 40}}
    res = await verify_x402_request(
        headers={"x-payment": _x_payment(payload)},
        is_cached_address=_always_true,
        accepted_network=networks.base.sepolia.caip2,
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "Unsupported x402 network" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_malformed_evm_pay_to():
    payload = {"accepted": {"network": networks.base.sepolia.caip2, "payTo": "not-an-address"}}
    res = await verify_x402_request(
        headers={"x-payment": _x_payment(payload)},
        is_cached_address=_always_true,
        accepted_network=networks.base.sepolia.caip2,
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "malformed accepted.payTo" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_pay_to_not_in_cache():
    payload = {"accepted": {"network": networks.base.sepolia.caip2, "payTo": "0x" + "f" * 40}}
    res = await verify_x402_request(
        headers={"x-payment": _x_payment(payload)},
        is_cached_address=_always_false,
        accepted_network=networks.base.sepolia.caip2,
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert "not found in cache" in res.body["error"]["message"]


@pytest.mark.asyncio
async def test_verify_x402_failures_carry_regenerate_next_steps():
    """Every failure path emits next_steps with regenerate_payment_credential + user_message + warning."""
    res = await verify_x402_request(
        headers={},
        is_cached_address=_always_true,
        accepted_network=networks.base.sepolia.caip2,
    )
    assert isinstance(res, VerifyX402RequestFailure)
    assert res.body["next_steps"]["action"] == "regenerate_payment_credential"
    assert "user_message" in res.body["next_steps"]
    assert "tempo wallet transfer" in res.body["next_steps"]["warning"]


@pytest.mark.asyncio
async def test_verify_x402_success_evm():
    pay_to = "0x" + "1" * 40
    payload = {"accepted": {"network": networks.base.sepolia.caip2, "payTo": pay_to}}
    res = await verify_x402_request(
        headers={"x-payment": _x_payment(payload)},
        is_cached_address=_always_true,
        accepted_network=networks.base.sepolia.caip2,
    )
    assert isinstance(res, VerifyX402RequestSuccess)
    assert res.signed_pay_to == pay_to
    assert res.signed_network == networks.base.sepolia.caip2


@pytest.mark.asyncio
async def test_verify_x402_rejects_solana_credential():
    """Solana credentials over x402 are not supported (Solana goes through MPP).

    The error message + next_steps point the client at MPP `solana/charge` so an
    agent on a stale x402 SVM client can recover with a single re-sign.
    """
    payload = {"accepted": {"network": networks.solana.mainnet.caip2, "payTo": "11111111111111111111111111111111"}}
    res = await verify_x402_request(
        headers={"x-payment": _x_payment(payload)},
        is_cached_address=_always_true,
        accepted_network=networks.base.sepolia.caip2,
    )
    assert isinstance(res, VerifyX402RequestFailure)
    msg = res.body["error"]["message"]
    assert "Solana" in msg
    assert "`solana/charge`" in msg
    # Recovery guidance points at the rail in the 402 challenge, not at any CLI by name.
    assert "solana/charge" in res.body["next_steps"]["user_message"]


# ─────────────────────────────────────────────────────────────────────────────
# process_x402_settle
# ─────────────────────────────────────────────────────────────────────────────


class _FakeServer:
    """Stubbed x402 server matching the x402 2.9 ``x402ResourceServer`` surface:
    sync ``build_payment_requirements(config, extensions=None)`` + sync
    ``enrich_extensions(declared, transport_context)`` + async ``verify_payment(payload,
    requirements)`` + async ``settle_payment(payload, requirements)``.
    """

    def __init__(
        self,
        requirements: list | Exception,
        verify_result: dict | Exception,
        settle_result: object | Exception | None = None,
        enrich_result: object | Exception = "passthrough",
    ) -> None:
        self.requirements = requirements
        self.verify_result = verify_result
        self.settle_result = settle_result
        self.enrich_result = enrich_result

    def build_payment_requirements(self, _cfg: object, _extensions: object = None) -> list:
        if isinstance(self.requirements, Exception):
            raise self.requirements
        return self.requirements

    def enrich_extensions(self, ext: object, _ctx: object) -> object:
        if isinstance(self.enrich_result, Exception):
            raise self.enrich_result
        return ext if self.enrich_result == "passthrough" else self.enrich_result

    async def verify_payment(self, _payload: object, _req: object) -> dict:
        if isinstance(self.verify_result, Exception):
            raise self.verify_result
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
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleFailure)
    assert res.phase == "no_requirements"


@pytest.mark.asyncio
async def test_process_x402_settle_verify_failed():
    server = _FakeServer(requirements=[{"id": "req1"}], verify_result={"success": False, "error": "bad sig"})
    res = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta=_RESOURCE_META,
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
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta=_RESOURCE_META,
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
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta=_RESOURCE_META,
        extension={"name": "bazaar"},
    )
    assert isinstance(res, ProcessX402SettleSuccess)
    assert res.matched_requirement == {"id": "req1"}
    assert res.settle_result == {"tx_hash": "0xabc", "amount": "110000"}
    assert res.payment_response_header is not None
    decoded = json.loads(base64.b64decode(res.payment_response_header).decode())
    assert decoded["tx_hash"] == "0xabc"


@pytest.mark.asyncio
async def test_process_x402_settle_coerces_dict_resource_config_with_camelcase_keys():
    """Consumers ported from the JS / Hono stack pass dicts with camelCase keys
    (``payTo``, ``maxTimeoutSeconds``). Coerce them into x402's ResourceConfig so
    ``build_payment_requirements(config.network)`` doesn't ``AttributeError`` on a dict.
    """
    captured: dict = {}

    class _CapturingServer:
        def build_payment_requirements(self, cfg: object, _ext: object = None) -> list:
            captured["cfg"] = cfg
            return [{"id": "req1"}]

        async def verify_payment(self, _payload: object, _req: object) -> dict:
            return {"is_valid": True}

        async def settle_payment(self, _payload: object, _req: object) -> dict:
            return {"tx_hash": "0xabc"}

    res = await process_x402_settle(
        x402_server=_CapturingServer(),
        payload={},
        resource_config={
            "scheme": "exact",
            "network": "eip155:8453",
            "price": "$0.10",
            "payTo": "0xa43d4e316ef5f430426cd1b454167e5f85e3f4f1",
            "maxTimeoutSeconds": 300,
        },
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleSuccess)
    # Coerced into x402's ResourceConfig (Pydantic model with snake_case attrs).
    cfg = captured["cfg"]
    assert hasattr(cfg, "network")
    assert cfg.network == "eip155:8453"
    assert cfg.pay_to == "0xa43d4e316ef5f430426cd1b454167e5f85e3f4f1"
    assert cfg.max_timeout_seconds == 300


@pytest.mark.asyncio
async def test_process_x402_settle_passes_typed_resource_config_unchanged():
    """If the caller already provides a typed ResourceConfig, pass it through unchanged."""
    from x402.schemas.config import ResourceConfig

    typed = ResourceConfig(
        scheme="exact",
        network="eip155:8453",
        price="$0.10",
        pay_to="0xa43d4e316ef5f430426cd1b454167e5f85e3f4f1",
        max_timeout_seconds=300,
    )
    captured: dict = {}

    class _CapturingServer:
        def build_payment_requirements(self, cfg: object, _ext: object = None) -> list:
            captured["cfg"] = cfg
            return [{"id": "req1"}]

        async def verify_payment(self, _payload: object, _req: object) -> dict:
            return {"is_valid": True}

        async def settle_payment(self, _payload: object, _req: object) -> dict:
            return {"tx_hash": "0xabc"}

    res = await process_x402_settle(
        x402_server=_CapturingServer(),
        payload={},
        resource_config=typed,
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleSuccess)
    assert captured["cfg"] is typed


@pytest.mark.asyncio
async def test_process_x402_settle_coerces_dict_payload_v2_to_typed_payment_payload():
    """Same fix as resource_config: ``verify_x402_request`` returns ``payload`` as a
    plain dict (the result of ``json.loads(base64.b64decode(X-Payment))``); x402 2.9's
    ``server.verify_payment`` calls ``payload.get_scheme()`` and other typed-model methods
    on it. Coerce dicts → ``PaymentPayload`` (or ``PaymentPayloadV1`` when ``x402Version=1``)
    so the helper doesn't ``AttributeError`` at the verify leg.
    """
    captured: dict = {}

    class _CapturingServer:
        def build_payment_requirements(self, _cfg: object, _ext: object = None) -> list:
            return [{"id": "req1"}]

        async def verify_payment(self, payload: object, _req: object) -> dict:
            captured["verify_payload"] = payload
            return {"is_valid": True}

        async def settle_payment(self, payload: object, _req: object) -> dict:
            captured["settle_payload"] = payload
            return {"tx_hash": "0xabc"}

    payload_dict = {
        "x402Version": 2,
        "accepted": {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": "100000",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "payTo": "0x000000000000000000000000000000000000dEaD",
            "maxTimeoutSeconds": 300,
        },
        "payload": {
            "authorization": {
                "from": "0xeb2Ca790F72787c7e61bC6c861353a1e4ACDFCa5",
                "to": "0x000000000000000000000000000000000000dEaD",
                "value": "100000",
                "validAfter": 0,
                "validBefore": 9999999999,
                "nonce": "0x" + "aa" * 32,
            },
            "signature": "0x" + "cc" * 65,
        },
    }
    res = await process_x402_settle(
        x402_server=_CapturingServer(),
        payload=payload_dict,
        resource_config={
            "scheme": "exact",
            "network": "eip155:8453",
            "price": "$0.10",
            "payTo": "0x" + "00" * 19 + "dE" + "aD",
            "maxTimeoutSeconds": 300,
        },
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleSuccess)
    # Both verify and settle legs received the typed Pydantic model, not the raw dict.
    verify_payload = captured["verify_payload"]
    settle_payload = captured["settle_payload"]
    assert type(verify_payload).__name__ == "PaymentPayload"
    assert type(settle_payload).__name__ == "PaymentPayload"
    # The typed model exposes the get_scheme() method that x402's facilitator calls.
    assert hasattr(verify_payload, "get_scheme")
    assert verify_payload.get_scheme() == "exact"


@pytest.mark.asyncio
async def test_process_x402_settle_serializes_pydantic_settle_result_to_payment_response_header():
    """x402 2.9's ``settle_payment`` returns a Pydantic ``SettleResponse`` model;
    plain ``json.dumps`` rejects it with ``TypeError``. The helper must call
    ``model_dump_json(by_alias=True)`` so the X-Payment-Response header stays
    base64'd JSON with the right wire keys (``errorReason`` not ``error_reason``).
    """
    from x402.schemas import SettleResponse

    pydantic_settle = SettleResponse(
        success=True,
        transaction="0xabc",
        network="eip155:8453",
        payer="0x000000000000000000000000000000000000dEaD",
        amount=None,
    )

    class _PydanticSettleServer:
        def build_payment_requirements(self, _cfg: object, _ext: object = None) -> list:
            return [{"id": "req1"}]

        async def verify_payment(self, _payload: object, _req: object) -> dict:
            return {"is_valid": True}

        async def settle_payment(self, _payload: object, _req: object) -> SettleResponse:
            return pydantic_settle

    res = await process_x402_settle(
        x402_server=_PydanticSettleServer(),
        payload={},
        resource_config={
            "scheme": "exact",
            "network": "eip155:8453",
            "price": "$0.10",
            "payTo": "0x" + "00" * 19 + "dE" + "aD",
            "maxTimeoutSeconds": 300,
        },
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleSuccess)
    assert res.payment_response_header is not None
    decoded = json.loads(base64.b64decode(res.payment_response_header).decode())
    assert decoded["success"] is True
    assert decoded["transaction"] == "0xabc"
    assert decoded["network"] == "eip155:8453"
    # by_alias=True: wire shape uses errorReason / errorMessage (camelCase) — not snake_case.
    assert "errorReason" in decoded


@pytest.mark.asyncio
async def test_process_x402_settle_serializes_dict_settle_result():
    """Plain-dict settle results (test stubs) still serialize."""

    class _DictSettleServer:
        def build_payment_requirements(self, _cfg: object, _ext: object = None) -> list:
            return [{"id": "req1"}]

        async def verify_payment(self, _payload: object, _req: object) -> dict:
            return {"is_valid": True}

        async def settle_payment(self, _payload: object, _req: object) -> dict:
            return {"success": True, "transaction": "0xdef"}

    res = await process_x402_settle(
        x402_server=_DictSettleServer(),
        payload={},
        resource_config={
            "scheme": "exact",
            "network": "eip155:8453",
            "price": "$0.10",
            "payTo": "0x" + "00" * 19 + "dE" + "aD",
            "maxTimeoutSeconds": 300,
        },
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleSuccess)
    assert res.payment_response_header is not None
    decoded = json.loads(base64.b64decode(res.payment_response_header).decode())
    assert decoded == {"success": True, "transaction": "0xdef"}


@pytest.mark.asyncio
async def test_process_x402_settle_passes_typed_payment_payload_unchanged():
    """If the caller already provides a typed PaymentPayload, pass it through unchanged."""
    from x402.schemas import PaymentPayload

    typed = PaymentPayload.model_validate(
        {
            "x402Version": 2,
            "accepted": {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": "100000",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "payTo": "0x000000000000000000000000000000000000dEaD",
                "maxTimeoutSeconds": 300,
            },
            "payload": {
                "authorization": {
                    "from": "0xeb2Ca790F72787c7e61bC6c861353a1e4ACDFCa5",
                    "to": "0x000000000000000000000000000000000000dEaD",
                    "value": "100000",
                    "validAfter": 0,
                    "validBefore": 9999999999,
                    "nonce": "0x" + "aa" * 32,
                },
                "signature": "0x" + "cc" * 65,
            },
        }
    )
    captured: dict = {}

    class _CapturingServer:
        def build_payment_requirements(self, _cfg: object, _ext: object = None) -> list:
            return [{"id": "req1"}]

        async def verify_payment(self, payload: object, _req: object) -> dict:
            captured["payload"] = payload
            return {"is_valid": True}

        async def settle_payment(self, _payload: object, _req: object) -> dict:
            return {"tx_hash": "0xabc"}

    res = await process_x402_settle(
        x402_server=_CapturingServer(),
        payload=typed,
        resource_config={
            "scheme": "exact",
            "network": "eip155:8453",
            "price": "$0.10",
            "payTo": "0x" + "00" * 19 + "dE" + "aD",
            "maxTimeoutSeconds": 300,
        },
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleSuccess)
    assert captured["payload"] is typed


# ─────────────────────────────────────────────────────────────────────────────
# process_x402_settle: facilitator_error wrap
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_x402_settle_wraps_build_requirements_throws_as_facilitator_error():
    server = _FakeServer(
        requirements=RuntimeError("facilitator: network not supported"),
        verify_result={"success": True},
    )
    res = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleFailure)
    assert res.phase == "facilitator_error"
    assert res.step == "build_requirements"
    assert isinstance(res.error, RuntimeError)


@pytest.mark.asyncio
async def test_process_x402_settle_wraps_enrich_extensions_throws_as_facilitator_error():
    server = _FakeServer(
        requirements=[{"id": "req1"}],
        verify_result={"success": True},
        enrich_result=RuntimeError("extension barfed"),
    )
    res = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta=_RESOURCE_META,
        extension={"name": "bazaar"},
    )
    assert isinstance(res, ProcessX402SettleFailure)
    assert res.phase == "facilitator_error"
    assert res.step == "enrich_extensions"


@pytest.mark.asyncio
async def test_process_x402_settle_wraps_verify_payment_throws_as_facilitator_error():
    server = _FakeServer(
        requirements=[{"id": "req1"}],
        verify_result=RuntimeError("CDP facilitator: solana:devnet not supported"),
    )
    res = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleFailure)
    assert res.phase == "facilitator_error"
    assert res.step == "verify_payment"
    assert isinstance(res.error, RuntimeError)


@pytest.mark.asyncio
async def test_process_x402_settle_does_not_swallow_settle_failed_as_facilitator_error():
    server = _FakeServer(
        requirements=[{"id": "req1"}],
        verify_result={"success": True},
        settle_result=RuntimeError("on-chain rejection"),
    )
    res = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta=_RESOURCE_META,
    )
    assert isinstance(res, ProcessX402SettleFailure)
    assert res.phase == "settle_failed"
    assert res.step is None


# ─────────────────────────────────────────────────────────────────────────────
# classify_x402_settle_result
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_returns_none_on_success():
    res = ProcessX402SettleSuccess(
        matched_requirement={"id": "req1"},
        settle_result={"tx": "0xabc"},
        payment_response_header="abc",
        verify_result={"success": True},
    )
    assert classify_x402_settle_result(res) is None


def test_classify_no_requirements_to_500_payment_internal_error():
    classified = classify_x402_settle_result(ProcessX402SettleFailure(phase="no_requirements", reason="empty"))
    assert classified is not None
    assert classified.status == 500
    assert classified.code == "payment_internal_error"
    assert classified.next_steps["action"] == "contact_support"


def test_classify_verify_failed_to_400_payment_proof_invalid():
    classified = classify_x402_settle_result(
        ProcessX402SettleFailure(phase="verify_failed", verify_result={"success": False, "reason": "expired"})
    )
    assert classified is not None
    assert classified.status == 400
    assert classified.code == "payment_proof_invalid"
    assert classified.next_steps["action"] == "regenerate_payment_credential"


def test_classify_facilitator_error_to_503_payment_provider_unavailable():
    classified = classify_x402_settle_result(
        ProcessX402SettleFailure(
            phase="facilitator_error", step="process_payment_request", error=RuntimeError("CDP rejects solana:devnet")
        )
    )
    assert classified is not None
    assert classified.status == 503
    assert classified.code == "payment_provider_unavailable"
    assert classified.next_steps["action"] == "try_different_rail"


def test_classify_settle_failed_to_503_with_retry_after():
    classified = classify_x402_settle_result(
        ProcessX402SettleFailure(
            phase="settle_failed", error=RuntimeError("on-chain rejection"), matched_requirement={}
        )
    )
    assert classified is not None
    assert classified.status == 503
    assert classified.code == "payment_provider_unavailable"
    assert classified.next_steps["action"] == "retry_or_swap_method"
    assert classified.next_steps["retry_after_seconds"] == 10


def test_classify_does_not_leak_raw_error_detail():
    sensitive = RuntimeError("CDP-INTERNAL-TRACE-ID-12345 secret-key-in-stack")
    classified = classify_x402_settle_result(
        ProcessX402SettleFailure(phase="facilitator_error", step="process_payment_request", error=sensitive)
    )
    assert classified is not None
    assert "CDP-INTERNAL-TRACE-ID-12345" not in classified.message
    assert "secret-key-in-stack" not in classified.message
    assert "CDP-INTERNAL-TRACE-ID-12345" not in classified.next_steps["user_message"]


# Required for asyncio fixtures
@pytest.fixture
def _ensure_loop() -> None:
    asyncio.get_event_loop()
    return


_ = (Awaitable,)  # reference imported names so noqa lines aren't needed
