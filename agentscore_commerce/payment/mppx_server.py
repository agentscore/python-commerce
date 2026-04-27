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
    methods: list[Any] | None = None,
) -> Any:
    """One-call pympp server setup.

    Returns a configured ``Mppx`` server instance. Raises ``ImportError`` with a
    guiding install command when ``pympp`` (or a per-rail extra) is missing.

    Async because Stripe SPT method construction may require an HTTP setup call
    to the Stripe API.
    """
    pympp = _import_optional("pympp.server")
    if pympp is None or not hasattr(pympp, "Mppx"):
        msg = "pympp not installed — run `pip install 'pympp[server,tempo,stripe]>=0.6,<1'` to use create_mppx_server."
        raise ImportError(msg)

    method_list: list[Any] = list(methods or [])
    rails_cfg = rails or MppxRails()

    if rails_cfg.tempo is not None:
        tempo_module = _import_optional("pympp.methods.tempo")
        charge_factory = getattr(tempo_module, "charge", None) if tempo_module else None
        if not callable(charge_factory):
            msg = "pympp[tempo] not installed — run `pip install 'pympp[tempo]'` for Tempo MPP rails."
            raise ImportError(msg)
        t = rails_cfg.tempo
        default_currency = USDC.tempo.testnet.address if t.testnet else USDC.tempo.mainnet.address
        method_list.append(
            charge_factory(
                currency=t.currency or default_currency,
                recipient=t.recipient,
                testnet=t.testnet,
            ),
        )

    if rails_cfg.tempo_session is not None:
        tempo_module = _import_optional("pympp.methods.tempo")
        session_factory = getattr(tempo_module, "session", None) if tempo_module else None
        if not callable(session_factory):
            msg = (
                "pympp[tempo] session support not available — your pympp version may "
                "not ship sessions yet. Upgrade with `pip install -U pympp`."
            )
            raise ImportError(msg)
        s = rails_cfg.tempo_session
        default_currency = USDC.tempo.testnet.address if s.testnet else USDC.tempo.mainnet.address
        kwargs: dict[str, Any] = {
            "currency": s.currency or default_currency,
            "recipient": s.recipient,
            "escrow_contract": s.escrow_contract,
            "store": s.store,
            "testnet": s.testnet,
        }
        if s.chains is not None:
            kwargs["chains"] = s.chains
        method_list.append(session_factory(**kwargs))

    if rails_cfg.stripe is not None:
        from agentscore_commerce.stripe_multichain.mppx_stripe import create_mppx_stripe

        stripe_method = await create_mppx_stripe(
            profile_id=rails_cfg.stripe.profile_id,
            secret_key=rails_cfg.stripe.secret_key,
            payment_method_types=rails_cfg.stripe.payment_method_types,
        )
        method_list.append(stripe_method)

    return pympp.Mppx.create(methods=method_list, secret_key=secret_key)


__all__ = [
    "MppxRails",
    "StripeRail",
    "TempoChargeRail",
    "TempoSessionRail",
    "create_mppx_server",
]
