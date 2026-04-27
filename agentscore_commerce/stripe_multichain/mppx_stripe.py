"""Stripe SPT method wrapper for ``pympp`` server merchants.

Wraps the ``pympp.stripe.charge(...)`` boilerplate so vendors only declare config,
not method instantiation. Returns the value vendors pass into ``Mppx.create(methods=[...])``.

Example::

    from pympp.server import Mppx
    from pympp.methods.tempo import charge as tempo_charge
    from agentscore_commerce.stripe_multichain import create_mppx_stripe

    stripe_method = await create_mppx_stripe(
        profile_id=os.environ["STRIPE_PROFILE_ID"],
        secret_key=os.environ["STRIPE_SECRET_KEY"],
    )

    mpp = Mppx.create(
        methods=[tempo_charge(currency=USDC_TEMPO, recipient=...), stripe_method],
        secret_key=os.environ["MPP_SECRET_KEY"],
    )

``pympp`` is an OPTIONAL peer dependency — vendors who don't use Stripe SPT don't need
to install it. Throws ``ImportError`` if pympp (or its stripe support) is missing.
"""

from __future__ import annotations

import importlib
from typing import Any

DEFAULT_PAYMENT_METHOD_TYPES = ("card", "link")


async def create_mppx_stripe(
    profile_id: str,
    secret_key: str,
    payment_method_types: list[str] | None = None,
) -> Any:
    """Build the Stripe SPT method instance for ``Mppx.create(methods=[...])``.

    Args:
        profile_id: Stripe profile_id / network_id advertised in your ``stripe/charge``
            ``accepted_methods`` entry.
        secret_key: Stripe secret key — pympp uses it to validate inbound SharedPaymentTokens.
        payment_method_types: Payment method types this stripe rail accepts. Defaults to
            ``["card", "link"]``.
    """
    try:
        stripe_module = importlib.import_module("pympp.methods.stripe")
    except ImportError as exc:
        msg = "pympp[stripe] not installed — run `pip install 'pympp[stripe]'` to use create_mppx_stripe."
        raise ImportError(msg) from exc

    charge_factory = getattr(stripe_module, "charge", None)
    if not callable(charge_factory):
        msg = (
            "pympp.methods.stripe.charge not found — your pympp version may not ship "
            "Stripe SPT support. Upgrade with `pip install -U pympp`."
        )
        raise ImportError(msg)

    return charge_factory(
        network_id=profile_id,
        payment_method_types=list(payment_method_types or DEFAULT_PAYMENT_METHOD_TYPES),
        secret_key=secret_key,
    )


__all__ = ["DEFAULT_PAYMENT_METHOD_TYPES", "create_mppx_stripe"]
