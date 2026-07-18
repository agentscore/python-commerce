"""Tests for `agentscore_commerce.payment.x402_settle.process_x402_settle`."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentscore_commerce.payment.x402_settle import (
    ProcessX402SettleFailure,
    ProcessX402SettleSuccess,
    classify_orchestration_error,
    classify_x402_settle_result,
    process_x402_settle,
    settle_result_to_json_bytes,
    strip_unsigned_x402_payload_fields,
)


def _make_server() -> MagicMock:
    server = MagicMock()
    server.build_payment_requirements = MagicMock(
        return_value=[
            {"scheme": "exact", "network": "eip155:84532", "payTo": "0xabc"},
        ]
    )
    server.enrich_extensions = MagicMock(return_value=None)
    server.verify_payment = AsyncMock(return_value={"is_valid": True})
    server.settle_payment = AsyncMock(return_value={"success": True, "transaction": "0xdead"})
    return server


@pytest.mark.asyncio
async def test_process_x402_settle_success() -> None:
    result = await process_x402_settle(
        x402_server=_make_server(),
        payload={"x402Version": 2},
        resource_config={"scheme": "exact", "network": "eip155:84532", "payTo": "0xabc"},
        resource_meta={"url": "https://x/y", "description": "t", "mimeType": "application/json"},
    )
    assert isinstance(result, ProcessX402SettleSuccess)


@pytest.mark.asyncio
async def test_process_x402_settle_build_requirements_throws() -> None:
    server = _make_server()
    server.build_payment_requirements = MagicMock(side_effect=RuntimeError("build broken"))
    result = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta={"url": "https://x/y", "description": "t", "mimeType": "application/json"},
    )
    assert isinstance(result, ProcessX402SettleFailure)
    assert result.phase == "facilitator_error"
    assert result.step == "build_requirements"


@pytest.mark.asyncio
async def test_process_x402_settle_no_requirements() -> None:
    server = _make_server()
    server.build_payment_requirements = MagicMock(return_value=[])
    result = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={},
        resource_meta={"url": "https://x/y", "description": "t", "mimeType": "application/json"},
    )
    assert isinstance(result, ProcessX402SettleFailure)
    assert result.phase == "no_requirements"


@pytest.mark.asyncio
async def test_process_x402_settle_verify_returns_invalid() -> None:
    server = _make_server()
    server.verify_payment = AsyncMock(return_value={"is_valid": False, "invalidReason": "bad sig"})
    result = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={"scheme": "exact"},
        resource_meta={"url": "https://x/y", "description": "t", "mimeType": "application/json"},
    )
    assert isinstance(result, ProcessX402SettleFailure)
    assert result.phase == "verify_failed"


@pytest.mark.asyncio
async def test_process_x402_settle_verify_throws() -> None:
    server = _make_server()
    server.verify_payment = AsyncMock(side_effect=RuntimeError("verify broken"))
    result = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={"scheme": "exact"},
        resource_meta={"url": "https://x/y", "description": "t", "mimeType": "application/json"},
    )
    assert isinstance(result, ProcessX402SettleFailure)
    assert result.step == "verify_payment"


@pytest.mark.asyncio
async def test_process_x402_settle_settle_throws() -> None:
    server = _make_server()
    server.settle_payment = AsyncMock(side_effect=RuntimeError("settle broken"))
    result = await process_x402_settle(
        x402_server=server,
        payload={},
        resource_config={"scheme": "exact"},
        resource_meta={"url": "https://x/y", "description": "t", "mimeType": "application/json"},
    )
    assert isinstance(result, ProcessX402SettleFailure)
    assert result.phase == "settle_failed"


def test_classify_x402_settle_result_for_success() -> None:
    failure = ProcessX402SettleFailure(phase="verify_failed", verify_result={"is_valid": False})
    cls = classify_x402_settle_result(failure)
    assert cls is not None


def test_classify_orchestration_error_unknown_returns_none() -> None:
    assert classify_orchestration_error(ValueError("totally unrelated")) is None


def test_settle_result_to_json_bytes() -> None:
    out = settle_result_to_json_bytes({"a": 1, "b": "two"})
    assert isinstance(out, bytes)
    assert b'"a"' in out


_ACCEPTED = {"scheme": "exact", "network": "eip155:8453", "payTo": "0xabc"}
_INNER = {"authorization": {"from": "0xsigner", "to": "0xabc"}, "signature": "0xdeadbeef"}
_WIRE = {
    "x402Version": 2,
    "payload": _INNER,
    "accepted": _ACCEPTED,
    "extensions": {"bazaar": {"schema": {"type": "object", "properties": {"phone": {"type": "string"}}}}},
    "resource": {"url": "https://x/person/base/no-pii", "description": "d" * 600, "tags": ["person"]},
}


def test_strip_drops_extensions_and_resource_keeps_accepted() -> None:
    assert strip_unsigned_x402_payload_fields(_WIRE) == {
        "x402Version": 2,
        "payload": _INNER,
        "accepted": _ACCEPTED,
    }


def test_strip_returns_same_object_when_neither_field_present() -> None:
    lean = {"x402Version": 2, "payload": _INNER, "accepted": _ACCEPTED}
    assert strip_unsigned_x402_payload_fields(lean) is lean


def test_strip_handles_single_field_and_non_dict() -> None:
    assert strip_unsigned_x402_payload_fields({"payload": _INNER, "resource": {"url": "x"}}) == {"payload": _INNER}
    assert strip_unsigned_x402_payload_fields({"payload": _INNER, "extensions": {"bazaar": {}}}) == {"payload": _INNER}
    assert strip_unsigned_x402_payload_fields(None) is None
    assert strip_unsigned_x402_payload_fields("not-a-dict") == "not-a-dict"


@pytest.mark.asyncio
async def test_process_x402_settle_strips_bloat_before_facilitator() -> None:
    # Incomplete `accepted` makes coerce_payment_payload leave the payload a plain dict, so we
    # can assert directly on what reaches verify_payment / settle_payment. The strip must have
    # removed extensions/resource and kept accepted (which CDP's v2 schema requires).
    server = _make_server()
    result = await process_x402_settle(
        x402_server=server,
        payload=_WIRE,
        resource_config={"scheme": "exact", "network": "eip155:8453", "payTo": "0xabc"},
        resource_meta={"url": "https://x/person/base/no-pii", "description": "t", "mimeType": "application/json"},
    )
    assert isinstance(result, ProcessX402SettleSuccess)
    for mock in (server.verify_payment, server.settle_payment):
        forwarded = mock.call_args[0][0]
        assert forwarded == {"x402Version": 2, "payload": _INNER, "accepted": _ACCEPTED}
        assert "accepted" in forwarded
        assert "extensions" not in forwarded
        assert "resource" not in forwarded
