"""Payment dispatch helpers.

* :func:`detect_rail_from_headers` — detect which payment-protocol family
  (x402 vs MPP) the inbound request carries, based on header presence.
* :func:`dispatch_settlement_by_network` — route a settlement payload to
  evm vs svm handler based on the CAIP-2 network family in
  ``payload.accepted.network``.
"""

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypeVar, cast

from agentscore_commerce.payment.network_kind import is_evm_network, is_solana_network

T = TypeVar("T")
Handler = Callable[[Any], T | Awaitable[T]]


def detect_rail_from_headers(headers: Mapping[str, str]) -> Literal["x402", "mpp"] | None:
    """Detect which payment-protocol family the inbound request carries.

    Returns ``"mpp"`` when an ``Authorization`` header starts with the ``Payment``
    scheme (case-insensitive per RFC 7235). Returns ``"x402"`` when a non-empty
    ``payment-signature`` or ``x-payment`` header is present. Returns ``None``
    otherwise.

    In practice a client constructs a request with exactly one protocol's headers;
    both arriving together is a client bug or misconfigured proxy. The helper
    checks MPP first so the rare degenerate case resolves to MPP. Empty header
    values are treated as absent. Header-name lookups are case-insensitive
    (RFC 7230 §3.2). The narrower rail naming (``"tempo"`` vs ``"solana"`` inside
    MPP) is merchant-side, derived from the credential body, not this helper.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    auth = lower.get("authorization") or ""
    if auth.lower().startswith("payment "):
        return "mpp"
    if lower.get("payment-signature") or lower.get("x-payment"):
        return "x402"
    return None


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
    if is_evm_network(network):
        if evm is None:
            raise ValueError(f"No EVM settlement handler registered (network: {network})")
        result = evm(payload)
    elif is_solana_network(network):
        if svm is None:
            raise ValueError(f"No Solana settlement handler registered (network: {network})")
        result = svm(payload)
    else:
        raise ValueError(f"Unrecognized network in settlement payload: {network}")
    if inspect.isawaitable(result):
        return await cast("Awaitable[T]", result)
    return cast("T", result)
