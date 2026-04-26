"""Settlement dispatch by CAIP-2 network family (eip155→evm, solana→svm)."""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

T = TypeVar("T")
Handler = Callable[[Any], T | Awaitable[T]]


async def dispatch_settlement_by_network(
    payload: Any,
    *,
    evm: Handler[T] | None = None,
    svm: Handler[T] | None = None,
) -> T:
    """Dispatch a settlement payload to evm or svm handler based on payload.accepted.network.

    Raises:
        ValueError: if the network is unrecognized or no matching handler is registered.
    """
    network = payload.accepted["network"] if isinstance(payload.accepted, dict) else payload.accepted.network
    if network.startswith("eip155:"):
        if evm is None:
            raise ValueError(f"No EVM settlement handler registered (network: {network})")
        result = evm(payload)
    elif network.startswith("solana:"):
        if svm is None:
            raise ValueError(f"No Solana settlement handler registered (network: {network})")
        result = svm(payload)
    else:
        raise ValueError(f"Unrecognized network in settlement payload: {network}")
    if inspect.isawaitable(result):
        return await cast("Awaitable[T]", result)
    return cast("T", result)
