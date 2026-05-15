"""Lazy-init helpers for x402 + mppx servers.

Every merchant accepting these rails writes the same singleton + asyncio.Lock
pattern around ``create_x402_server`` / ``create_mppx_server``. These helpers
collapse the boilerplate to a single call; the returned getter is safe to call
from any number of concurrent handlers; only one server instance is ever
constructed per merchant.

The x402 helper also derives the facilitator choice (``coinbase`` vs ``http``)
from optional CDP credentials so merchants don't repeat the boot-time conditional.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agentscore_commerce.payment.mppx_server import create_mppx_server
from agentscore_commerce.payment.x402_server import create_x402_server

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentscore_commerce.payment.mppx_server import MppxRailSpec
    from agentscore_commerce.payment.rail_spec import X402BaseRailSpec
    from agentscore_commerce.payment.x402_server import X402SymbolicRail


def _x402_rail_name(spec: X402BaseRailSpec) -> X402SymbolicRail:
    """Map ``X402BaseRailSpec.network`` to the symbolic rail string.

    The underlying x402 scheme registry indexes by symbolic name; we keep the
    CAIP-2 ↔ symbolic mapping in one place so merchants pass RailSpecs everywhere.
    """
    if spec.network in ("eip155:8453",):
        return "x402-base-mainnet"
    if spec.network in ("eip155:84532",):
        return "x402-base-sepolia"
    msg = f"lazy_x402_server: unsupported X402BaseRailSpec.network={spec.network!r}"
    raise ValueError(msg)


def lazy_x402_server(
    *,
    spec: X402BaseRailSpec,
    cdp_api_key_id: str | None = None,
    cdp_api_key_secret: str | None = None,
) -> Callable[[], Awaitable[Any]]:
    """Build a memoized async getter for an x402 server.

    First call constructs the server; subsequent calls return the cached
    instance. Concurrent first-callers serialize on an asyncio.Lock so we
    never construct two and discard one.

    When both CDP creds are passed, the server uses Coinbase's facilitator;
    otherwise it falls back to the public HTTP facilitator. Merchants who
    only have one of the two creds get the HTTP fallback (with a server-side
    warning logged by ``create_x402_server``).
    """
    cache: list[Any] = [None]
    lock = asyncio.Lock()
    rail_name = _x402_rail_name(spec)
    use_cdp = bool(cdp_api_key_id and cdp_api_key_secret)
    facilitator = "coinbase" if use_cdp else "http"

    async def getter() -> Any:
        if cache[0] is not None:
            return cache[0]
        async with lock:
            if cache[0] is not None:
                return cache[0]
            cache[0] = await create_x402_server(facilitator=facilitator, rails=[rail_name])
            return cache[0]

    return getter


def lazy_mppx_server(
    *,
    rails: dict[str, MppxRailSpec],
    secret_key: str,
    realm: str | None = None,
) -> Callable[[], Awaitable[Any]]:
    """Build a memoized async getter for a pympp server.

    Same singleton + lock semantics as :func:`lazy_x402_server`. Forwards
    ``rails`` / ``secret_key`` / ``realm`` unchanged to
    :func:`create_mppx_server`.
    """
    cache: list[Any] = [None]
    lock = asyncio.Lock()

    async def getter() -> Any:
        if cache[0] is not None:
            return cache[0]
        async with lock:
            if cache[0] is not None:
                return cache[0]
            cache[0] = await create_mppx_server(
                secret_key=secret_key,
                rails=rails,
                realm=realm,
            )
            return cache[0]

    return getter


__all__ = ["lazy_mppx_server", "lazy_x402_server"]
