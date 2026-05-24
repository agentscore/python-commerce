"""Settle-outcome → Stripe testnet simulator dispatch.

Replaces the 3-branch rail/rail_key switch + thin
``simulate_deposit_if_testnet(addr, network)`` wrapper a merchant would
otherwise hand-roll in their own payment helpers. Call it directly from
``on_settled``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from agentscore_commerce.stripe_multichain.simulate_deposit import simulate_deposit_if_test_mode

if TYPE_CHECKING:
    from collections.abc import Callable

SimulateNetwork = Literal["tempo", "base", "solana"]


def _field(outcome: Any, name: str) -> str | None:
    """Read a field from an outcome that may be a dataclass, pydantic model, or dict."""
    if outcome is None:
        return None
    val = outcome.get(name) if isinstance(outcome, dict) else getattr(outcome, name, None)
    return val if isinstance(val, str) else None


def network_for_outcome(outcome: Any) -> SimulateNetwork | None:
    """Map a settle outcome to the simulator's ``network`` arg.

    Reads ``Checkout.on_settled`` / ``compute_first_checkout.on_settled``
    outcomes and returns ``None`` for Stripe SPT (no on-chain deposit) or
    unknown rails.

    Accepts both Checkout-shaped outcomes (``rail`` + ``rail_key``) and
    compute-first-shaped outcomes (``rail`` + ``mpp_method``). The two
    diverged historically; this helper canonicalizes them.
    """
    rail = _field(outcome, "rail")
    if rail == "x402":
        return "base"
    # mppx's Receipt.method can be either the bare scheme name (``'tempo'``)
    # or the full directive (``'tempo/charge'``) depending on the version.
    method = _field(outcome, "mpp_method") or _field(outcome, "mppMethod")
    scheme = method.split("/", 1)[0] if method else None
    if scheme == "tempo":
        return "tempo"
    if scheme == "solana":
        return "solana"
    if scheme == "stripe":
        return None
    rail_key = _field(outcome, "rail_key") or _field(outcome, "railKey")
    if rail_key in ("tempo", "tempo_mpp"):
        return "tempo"
    if rail_key == "solana_mpp":
        return "solana"
    if rail_key == "x402_base":
        return "base"
    if rail_key == "stripe":
        return None
    return None


async def simulate_deposit_for_outcome(
    *,
    outcome: Any,
    deposit_address: str,
    get_payment_intent_id: Callable[[str], str | None],
    stripe_secret_key: str,
    stripe_version: str | None = None,
    buyer_wallet: str | None = None,
) -> None:
    """Dispatch :func:`simulate_deposit_if_test_mode` based on the outcome's rail.

    Calls through to the SDK simulator; no-op for Stripe SPT or unknown rails.

    Use this in ``on_settled`` to replace the hand-rolled rail switch +
    ``simulate_deposit_if_testnet`` wrapper pattern.
    """
    network = network_for_outcome(outcome)
    if network is None:
        return
    kwargs: dict[str, Any] = {
        "get_payment_intent_id": get_payment_intent_id,
        "deposit_address": deposit_address,
        "network": network,
        "stripe_secret_key": stripe_secret_key,
    }
    if buyer_wallet is not None:
        kwargs["buyer_wallet"] = buyer_wallet
    if stripe_version is not None:
        kwargs["stripe_version"] = stripe_version
    await simulate_deposit_if_test_mode(**kwargs)
