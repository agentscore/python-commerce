from dataclasses import dataclass

import pytest

from agentscore_commerce.payment import dispatch_settlement_by_network


@dataclass
class _Payload:
    accepted: dict


async def test_dispatches_to_evm_for_eip155():
    p = _Payload(accepted={"network": "eip155:8453"})
    out = await dispatch_settlement_by_network(p, evm=lambda _: "evm-result")
    assert out == "evm-result"


async def test_dispatches_to_svm_for_solana():
    p = _Payload(accepted={"network": "solana:abc"})
    out = await dispatch_settlement_by_network(p, svm=lambda _: "svm-result")
    assert out == "svm-result"


async def test_raises_when_no_handler_registered():
    from agentscore_commerce.errors import CheckoutValidationError

    p = _Payload(accepted={"network": "eip155:8453"})
    with pytest.raises(CheckoutValidationError) as exc:
        await dispatch_settlement_by_network(p, svm=lambda _: "x")
    assert exc.value.code == "payment_provider_unavailable"
    assert exc.value.status == 503
    assert "No EVM" in exc.value.message


async def test_raises_for_unrecognized_network():
    from agentscore_commerce.errors import CheckoutValidationError

    p = _Payload(accepted={"network": "cosmos:foo"})
    with pytest.raises(CheckoutValidationError) as exc:
        await dispatch_settlement_by_network(p, evm=lambda _: "x", svm=lambda _: "x")
    assert exc.value.code == "payment_provider_unavailable"
    assert exc.value.status == 503
    assert "Unrecognized" in exc.value.message


async def test_awaits_async_handler():
    p = _Payload(accepted={"network": "eip155:8453"})

    async def handler(_):
        return "async-evm"

    out = await dispatch_settlement_by_network(p, evm=handler)
    assert out == "async-evm"
