"""Boilerplate-reducer for the ``rails`` config passed to ``Checkout``.

Merchants supplying a chain set always rebuild the same constants (empty
``recipient`` sentinel, network/chain_id/token defaults); this helper folds
those defaults in so the merchant config only specifies the merchant-specific
overrides.

Per-order recipient minting (Stripe-multichain) is wired via Checkout's
``mint_recipients`` hook, so the ``recipient=""`` sentinel here is the
expected shape — ``mint_recipients`` overrides it at request time.
"""

from __future__ import annotations

from typing import Any

from agentscore_commerce.payment.rail_spec import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
)


def build_default_checkout_rails(
    *,
    tempo: dict[str, Any] | None = None,
    x402_base: dict[str, Any] | None = None,
    solana_mpp: dict[str, Any] | None = None,
    stripe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical four-rail ``rails`` dict for ``Checkout``.

    Keys match the convention used across consumer codebases: ``tempo``,
    ``x402_base``, ``solana_mpp``, ``stripe``. Empty-string ``recipient`` is a
    placeholder — ``Checkout.mint_recipients`` must populate real values at
    request time.

    Each kwarg accepts a partial dict of rail-spec fields (matching the
    dataclass field names from :mod:`agentscore_commerce.payment.rail_spec`).
    Omit a kwarg to skip that rail entirely.

    Example::

        rails = build_default_checkout_rails(
            tempo={"testnet": True},
            x402_base={"network": "eip155:84532"},
            solana_mpp={},
            stripe={"profile_id": "p_test", "payment_method_types": ["card", "link"]},
        )
    """
    out: dict[str, Any] = {}
    if tempo is not None:
        spec_args = dict(tempo)
        spec_args.setdefault("recipient", "")
        out["tempo"] = TempoRailSpec(**spec_args)
    if x402_base is not None:
        spec_args = dict(x402_base)
        spec_args.setdefault("recipient", "")
        out["x402_base"] = X402BaseRailSpec(**spec_args)
    if solana_mpp is not None:
        spec_args = dict(solana_mpp)
        spec_args.setdefault("recipient", "")
        out["solana_mpp"] = SolanaMppRailSpec(**spec_args)
    if stripe is not None:
        out["stripe"] = StripeRailSpec(**stripe)
    return out
