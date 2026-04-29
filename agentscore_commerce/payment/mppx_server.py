"""One-call MPP server setup wrapping the official `pympp` Python package.

Wires Tempo charge, Tempo session (channel-based for variable-cost /
streaming), and Stripe SPT methods from symbolic rail config — replaces
the boilerplate of constructing each method by hand.

Usage::

    from agentscore_commerce.payment import create_mppx_server, MppxRails, TempoChargeRail

    mpp = await create_mppx_server(
        rails=MppxRails(
            tempo=TempoChargeRail(recipient=os.environ["TEMPO_RECIPIENT"]),
            stripe=StripeRail(
                profile_id=os.environ["STRIPE_PROFILE_ID"],
                secret_key=os.environ["STRIPE_SECRET_KEY"],
            ),
        ),
        secret_key=os.environ["MPP_SECRET_KEY"],
    )

`pympp` is an OPTIONAL peer dependency — install only if you accept MPP rails::

    pip install 'pympp[server,tempo,stripe]>=0.6,<1'
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from agentscore_commerce.payment.usdc import USDC


@dataclass
class TempoChargeRail:
    """One-shot Tempo USDC charge (intent: ``charge``)."""

    recipient: str
    """Tempo wallet address that receives settled funds."""

    currency: str | None = None
    """Token contract address. Defaults to USDC on Tempo (selected by ``testnet`` flag)."""

    testnet: bool = False
    """Use Tempo testnet (Moderato) instead of mainnet."""


@dataclass
class TempoSessionRail:
    """Tempo session (intent: ``session``) — pay-as-you-go channel.

    Used for repeated calls or SSE-streamed responses. Vendor brings their own
    ``ChannelStore`` and ``escrow_contract`` address.
    """

    recipient: str
    escrow_contract: str
    """On-chain escrow contract address that holds channel deposits and pays out
    cumulative vouchers on settlement. Vendor-deployed."""

    store: Any
    """ChannelStore implementation tracking open channels + cumulative voucher state.
    Pass an instance of pympp's ``ChannelStore`` interface (in-memory default for
    dev or a Postgres/Redis-backed store for production)."""

    currency: str | None = None
    testnet: bool = False
    chains: Any | None = None
    """Optional supported chains; defaults to pympp defaults if omitted."""


@dataclass
class StripeRail:
    """Stripe SPT (Shared Payment Token) rail config.

    See :mod:`agentscore_commerce.stripe_multichain` for the multichain
    PaymentIntent helpers used alongside this rail.
    """

    profile_id: str
    secret_key: str
    payment_method_types: list[str] | None = None


@dataclass
class MppxRails:
    """Symbolic rail config for :func:`create_mppx_server`.

    Commerce wires the boilerplate (``tempo.charge()``, ``mpp_stripe.charge()``,
    etc.) so vendors only declare the rails they accept.
    """

    tempo: TempoChargeRail | None = None
    tempo_session: TempoSessionRail | None = None
    stripe: StripeRail | None = None


def _import_optional(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


async def create_mppx_server(
    secret_key: str,
    rails: MppxRails | None = None,
    method: Any = None,
    realm: str | None = None,
) -> Any:
    """One-call pympp server setup.

    Returns a configured ``Mpp`` server instance from ``pympp>=0.6``. Raises
    ``ImportError`` with a guiding install command when ``pympp`` or a per-rail
    extra is missing.

    Async because Stripe SPT method construction may require an HTTP setup call
    to the Stripe API.

    Note: pympp 0.6 takes a single ``method`` per ``Mpp`` instance (the prior
    multi-method ``Mppx`` API was removed). If multiple rails are configured on
    ``rails``, the first non-None one wins; merchants supporting multiple
    distinct methods (e.g. tempo charge + tempo session, or tempo + Stripe SPT)
    construct a separate ``Mpp`` instance per method and route by the method
    name they detect on the request. Mirrors how pympp 0.6 separates methods.
    """
    # The pympp distribution publishes its modules under the top-level `mpp`
    # package (the dist name is `pympp` but `import pympp` doesn't resolve —
    # only `import mpp`).
    pympp = _import_optional("mpp.server")
    if pympp is None or not hasattr(pympp, "Mpp"):
        msg = "pympp not installed — run `pip install 'pympp[server,tempo,stripe]>=0.6,<1'` to use create_mppx_server."
        raise ImportError(msg)

    rails_cfg = rails or MppxRails()
    resolved_method: Any = method

    if resolved_method is None and rails_cfg.tempo is not None:
        tempo_module = _import_optional("mpp.methods.tempo")
        tempo_factory = getattr(tempo_module, "tempo", None) if tempo_module else None
        if not callable(tempo_factory):
            msg = "pympp[tempo] not installed — run `pip install 'pympp[tempo]'` for Tempo MPP rails."
            raise ImportError(msg)
        charge_intent_cls = getattr(tempo_module, "ChargeIntent", None) if tempo_module else None
        if charge_intent_cls is None:
            msg = "pympp[tempo] missing ChargeIntent — upgrade pympp to 0.6+."
            raise ImportError(msg)
        t = rails_cfg.tempo
        default_currency = USDC.tempo.testnet.address if t.testnet else USDC.tempo.mainnet.address
        chain_id = 42431 if t.testnet else 4217
        resolved_method = tempo_factory(
            intents={"charge": charge_intent_cls()},
            currency=t.currency or default_currency,
            recipient=t.recipient,
            chain_id=chain_id,
        )

    if resolved_method is None and rails_cfg.tempo_session is not None:
        # pympp 0.6 has not shipped a session intent factory under the same
        # naming. Keep the surface (TempoSessionRail), but vendors must wait
        # for pympp to expose ``SessionIntent`` before this branch resolves.
        msg = (
            "pympp[tempo] session support not available — pympp 0.6 has not "
            "shipped a SessionIntent factory yet. Upgrade pympp when it does "
            "or pass `method=` directly with a hand-built TempoMethod."
        )
        raise ImportError(msg)

    if resolved_method is None and rails_cfg.stripe is not None:
        from agentscore_commerce.stripe_multichain.mppx_stripe import create_mppx_stripe

        resolved_method = await create_mppx_stripe(
            profile_id=rails_cfg.stripe.profile_id,
            secret_key=rails_cfg.stripe.secret_key,
            payment_method_types=rails_cfg.stripe.payment_method_types,
        )

    if resolved_method is None:
        msg = (
            "create_mppx_server called with no method or rails — pass at least one of "
            "`method=`, `rails.tempo`, `rails.tempo_session`, or `rails.stripe`."
        )
        raise ValueError(msg)

    kwargs: dict[str, Any] = {"method": resolved_method, "secret_key": secret_key}
    if realm is not None:
        kwargs["realm"] = realm
    return pympp.Mpp.create(**kwargs)


__all__ = [
    "MppxRails",
    "StripeRail",
    "TempoChargeRail",
    "TempoSessionRail",
    "create_mppx_server",
]
