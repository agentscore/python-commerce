"""One-call MPP server setup wrapping the official `pympp` Python package.

Wires Tempo charge, Tempo session (channel-based for variable-cost /
streaming), and Stripe SPT methods from rail specs — replaces the boilerplate
of constructing each method by hand.

Usage::

    from agentscore_commerce.payment import (
        create_mppx_server,
        TempoRailSpec,
        StripeRailSpec,
    )

    mpp = await create_mppx_server(
        secret_key=os.environ["MPP_SECRET_KEY"],
        rails={
            "tempo": TempoRailSpec(recipient=os.environ["TEMPO_RECIPIENT"]),
            "stripe": StripeRailSpec(
                profile_id=os.environ["STRIPE_PROFILE_ID"],
                secret_key=os.environ["STRIPE_SECRET_KEY"],
            ),
        },
    )

Keys are rail names (``"tempo"``, ``"tempo_session"``, ``"stripe"``); values are
the canonical ``*RailSpec`` instances every other helper also consumes.

`pympp` is an OPTIONAL peer dependency — install only if you accept MPP rails::

    pip install 'pympp[server,tempo,stripe]>=0.6,<1'
"""

from __future__ import annotations

import importlib
from typing import Any

from agentscore_commerce.payment.rail_spec import (
    RecipientLike,
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    TempoSessionRailSpec,
    resolve_recipient,
)
from agentscore_commerce.payment.usdc import USDC

MppxRailSpec = TempoRailSpec | TempoSessionRailSpec | StripeRailSpec | SolanaMppRailSpec


def _import_optional(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


async def _resolve_recipient_for_method(recipient: RecipientLike) -> str:
    return await resolve_recipient(recipient)


async def _tempo_method(spec: TempoRailSpec) -> Any:
    tempo_module = _import_optional("mpp.methods.tempo")
    tempo_factory = getattr(tempo_module, "tempo", None) if tempo_module else None
    if not callable(tempo_factory):
        msg = "pympp[tempo] not installed — run `pip install 'pympp[tempo]'` for Tempo MPP rails."
        raise ImportError(msg)
    charge_intent_cls = getattr(tempo_module, "ChargeIntent", None) if tempo_module else None
    if charge_intent_cls is None:
        msg = "pympp[tempo] missing ChargeIntent — upgrade pympp to 0.6+."
        raise ImportError(msg)
    default_currency = USDC.tempo.testnet.address if spec.testnet else USDC.tempo.mainnet.address
    chain_id = 42431 if spec.testnet else (spec.chain_id or 4217)
    return tempo_factory(
        intents={"charge": charge_intent_cls()},
        currency=spec.token or default_currency,
        recipient=await _resolve_recipient_for_method(spec.recipient),
        chain_id=chain_id,
    )


async def _stripe_method(spec: StripeRailSpec) -> Any:
    from agentscore_commerce.stripe_multichain.mppx_stripe import create_mppx_stripe

    if not spec.profile_id or not spec.secret_key:
        msg = "StripeRailSpec for create_mppx_server requires both profile_id and secret_key."
        raise ValueError(msg)
    return await create_mppx_stripe(
        profile_id=spec.profile_id,
        secret_key=spec.secret_key,
        payment_method_types=spec.payment_method_types,
    )


async def create_mppx_server(
    *,
    secret_key: str,
    rails: dict[str, MppxRailSpec] | None = None,
    method: Any = None,
    realm: str | None = None,
) -> Any:
    """One-call pympp server setup.

    Returns a configured ``Mpp`` server instance from ``pympp>=0.6``. Raises
    ``ImportError`` with a guiding install command when ``pympp`` or a per-rail
    extra is missing.

    ``rails`` keys are rail names (``"tempo"``, ``"tempo_session"``, ``"stripe"``);
    values are the canonical ``*RailSpec`` instances every other helper also
    consumes. Tempo session is reserved for future pympp ``SessionIntent``
    support — passing it today raises ``ImportError``.

    pympp 0.6 takes a single ``method`` per ``Mpp`` instance. When ``rails`` is
    provided, the first resolvable rail in dict-insertion order wins; merchants
    supporting multiple distinct methods construct a separate ``Mpp`` per method
    and route by name at the request layer.
    """
    pympp = _import_optional("mpp.server")
    if pympp is None or not hasattr(pympp, "Mpp"):
        msg = "pympp not installed — run `pip install 'pympp[server,tempo,stripe]>=0.6,<1'` to use create_mppx_server."
        raise ImportError(msg)

    resolved_method: Any = method
    rails_map: dict[str, MppxRailSpec] = rails or {}

    if resolved_method is None:
        for name, spec in rails_map.items():
            if isinstance(spec, TempoRailSpec):
                resolved_method = await _tempo_method(spec)
                break
            if isinstance(spec, TempoSessionRailSpec):
                msg = (
                    "pympp[tempo] session support not available — pympp 0.6 has not "
                    "shipped a SessionIntent factory yet. Upgrade pympp when it does "
                    "or pass `method=` directly with a hand-built TempoMethod."
                )
                raise ImportError(msg)
            if isinstance(spec, StripeRailSpec):
                resolved_method = await _stripe_method(spec)
                break
            msg = f"create_mppx_server: unsupported rail spec for key {name!r}: {type(spec).__name__}"
            raise TypeError(msg)

    if resolved_method is None:
        msg = (
            "create_mppx_server called with no method or rails — pass `method=` or a "
            "non-empty `rails={...}` map keyed by rail name (`tempo`, `tempo_session`, `stripe`)."
        )
        raise ValueError(msg)

    kwargs: dict[str, Any] = {"method": resolved_method, "secret_key": secret_key}
    if realm is not None:
        kwargs["realm"] = realm
    return pympp.Mpp.create(**kwargs)


__all__ = [
    "MppxRailSpec",
    "create_mppx_server",
]
