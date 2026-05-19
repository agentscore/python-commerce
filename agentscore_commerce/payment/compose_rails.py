"""Builder for the ``compose(*intents)`` array passed to mppx.

Replaces the hand-rolled ``compose_rails`` assembly that recurs verbatim
across multi-rail merchants' ``compose_mppx`` hooks.

The intent shape is mppx-protocol-shaped; this helper spares callers from
re-typing the same atomic-conversion + per-rail dict literal.

Mirrors node-commerce ``src/payment/compose_rails.ts``.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from agentscore_commerce.payment.amounts import usd_to_atomic
from agentscore_commerce.payment.usdc import USDC

_SOLANA_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
_STRIPE_MIN_CHARGE_USD = Decimal("0.50")
_warned_stripe_below_minimum = False
_logger = logging.getLogger(__name__)


def build_mppx_compose_rails(
    *,
    amount_usd: str,
    tempo_recipient: str | None = None,
    tempo_token_address: str | None = None,
    solana_recipient: str | None = None,
    solana_token_mint: str | None = None,
    solana_network: str | None = None,
    include_stripe: bool = True,
) -> list[tuple[str, dict[str, Any]]]:
    """Build the ``compose(*intents)`` argument list.

    Order matches mppx's preferred ordering: tempo first (cheapest), then
    solana, then stripe. The returned list contains ``(directive, payload)``
    tuples ready to splat into ``mppx.compose(*build_mppx_compose_rails(...))``.

    Args:
        amount_usd: USD price string (``"1.50"``). Tempo + Stripe consume it
            verbatim; Solana converts to atomic units (``int``) via
            :func:`usd_to_atomic`.
        tempo_recipient: Tempo address. When ``None``, the ``tempo/charge``
            intent is omitted.
        tempo_token_address: Tempo USDC contract address. Defaults to
            ``USDC.tempo.mainnet.address``.
        solana_recipient: Solana address. When ``None``, the ``solana/charge``
            intent is omitted.
        solana_token_mint: Solana USDC mint. Defaults to
            ``USDC.solana.mainnet.mint``.
        solana_network: Solana CAIP-2 network. Defaults to mainnet-beta.
        include_stripe: Include the ``stripe/charge`` intent (Stripe SPT
            rail). Default ``True``.

            Stripe's documented USD minimum is $0.50 because the fixed
            processing fee (~$0.30) exceeds revenue below that — sub-50-cent
            charges that DO go through still cost the merchant money (a
            $0.11 PI nets -$0.19 after fees). Some Stripe accounts also
            reject PI creation under the floor with ``amount_too_small``.
            The helper auto-drops the rail (with a one-time
            ``logging.warning``) when ``amount_usd < 0.50`` so sub-50-cent
            APIs don't ship an unprofitable rail. Pass
            ``include_stripe=False`` explicitly to suppress the warning.

    Raises:
        ValueError: when Solana is requested but ``amount_usd`` can't convert
        to atomic — merchants should catch and return a 402 to drop the rail
        rather than crash the request.
    """
    rails: list[tuple[str, dict[str, Any]]] = []
    if tempo_recipient:
        rails.append(
            (
                "tempo/charge",
                {
                    "amount": amount_usd,
                    "currency": tempo_token_address or USDC.tempo.mainnet.address,
                    "decimals": 6,
                    "recipient": tempo_recipient,
                },
            )
        )
    if solana_recipient:
        atomic = usd_to_atomic(amount_usd, decimals=6)
        rails.append(
            (
                "solana/charge",
                {
                    "amount": str(atomic),
                    "currency": solana_token_mint or USDC.solana.mainnet.mint,
                    "decimals": 6,
                    "recipient": solana_recipient,
                    "network": solana_network or _SOLANA_MAINNET_CAIP2,
                },
            )
        )
    if include_stripe:
        try:
            amount_decimal = Decimal(amount_usd)
        except (InvalidOperation, TypeError, ValueError):
            amount_decimal = None
        if amount_decimal is not None and amount_decimal < _STRIPE_MIN_CHARGE_USD:
            global _warned_stripe_below_minimum
            if not _warned_stripe_below_minimum:
                _warned_stripe_below_minimum = True
                _logger.warning(
                    "[build_mppx_compose_rails] Dropping stripe/charge rail: amount_usd=%s is below "
                    "Stripe's $%s USD minimum. Stripe's fixed ~$0.30 fee makes sub-50-cent charges "
                    "unprofitable (and many accounts reject PI creation with amount_too_small below "
                    "this floor). Pass include_stripe=False to suppress this warning.",
                    amount_usd,
                    _STRIPE_MIN_CHARGE_USD,
                )
        else:
            rails.append(("stripe/charge", {"amount": amount_usd, "currency": "usd", "decimals": 2}))
    return rails
