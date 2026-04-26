"""Multi-rail payment header bundle.

One call composes both ``WWW-Authenticate`` (the ``paymentauth.org`` Payment directives)
and the standard x402 ``PAYMENT-REQUIRED`` header from a single rails declaration.
Reduces ~10 lines of merchant boilerplate per 402 response.

Layered on top of :func:`payment_directive` / :func:`www_authenticate_header` /
:func:`payment_required_header` — those primitives stay exposed for vendors who want
full control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from agentscore_commerce.payment.directive import (
    BuildPaymentDirectiveInput,
    build_payment_directive,
)
from agentscore_commerce.payment.wwwauthenticate import (
    PaymentRequiredHeaderInput,
    payment_required_header,
    www_authenticate_header,
)


@dataclass
class PaymentHeadersRail:
    """One rail entry for :func:`build_payment_headers`."""

    rail: str
    """Symbolic rail name — ``tempo-mainnet``, ``x402-base-mainnet``, ``stripe``, etc."""

    amount_usd: str | float
    """Amount in USD as a number or string."""

    recipient: str | None = None
    """Recipient address (on-chain) — required for crypto rails."""

    network_id: str | None = None
    """Stripe profile_id / network_id — required for ``stripe`` rail."""

    chain_id: int | None = None
    """EVM chain id override — usually inferred from rail."""

    currency: str | None = None
    """Token contract / currency override — usually inferred from rail."""

    decimals: int | None = None
    """Decimal precision override — usually inferred from rail (USDC=6, etc.)."""

    method: str | None = None
    """MPP method override — usually inferred from rail."""

    intent: str | None = None
    """MPP intent. Default ``charge``."""

    expires: str | None = None
    """ISO-8601 expiry. Default now + 5 min."""


@dataclass
class X402AcceptsBlock:
    """x402 PAYMENT-REQUIRED header inputs."""

    accepts: list[Any]
    version: Literal[1, 2] = 1
    resource: dict[str, str] | None = None


@dataclass
class BuildPaymentHeadersInput:
    """Input shape for :func:`build_payment_headers`."""

    rails: list[PaymentHeadersRail] = field(default_factory=list)
    order_id: str = ""
    """Order id used as the directive challenge id (per-rail it becomes ``{order_id}-{rail}``)."""

    realm: str = ""
    """Realm — the host of the merchant URL (e.g. ``agents.merchant.example``)."""

    x402: X402AcceptsBlock | None = None
    """Optional x402 ``accepts`` array — included as the standard PAYMENT-REQUIRED header
    so x402 clients (``x402[fastapi]``, ``agentscore-pay``) can parse the binary-friendly
    format. Pass ``None`` (or omit) to skip the PAYMENT-REQUIRED header."""


class PaymentHeadersResult(TypedDict, total=False):
    """Header dict returned by :func:`build_payment_headers`."""

    www_authenticate: str
    """Multi-directive ``WWW-Authenticate`` header value (caller sets the actual
    header name; HTTP libraries typically lowercase it)."""

    payment_required: str
    """Base64-encoded x402 PAYMENT-REQUIRED header value. Only present when x402 inputs
    were provided."""


def build_payment_headers(input: BuildPaymentHeadersInput) -> PaymentHeadersResult:
    """Compose WWW-Authenticate + PAYMENT-REQUIRED headers from a single rails declaration.

    Returns a dict with snake_case keys — callers map to actual HTTP header names::

        headers = build_payment_headers(BuildPaymentHeadersInput(...))
        response.headers["www-authenticate"] = headers["www_authenticate"]
        if "payment_required" in headers:
            response.headers["PAYMENT-REQUIRED"] = headers["payment_required"]

    Example::

        headers = build_payment_headers(BuildPaymentHeadersInput(
            order_id="ord_123",
            realm="agents.merchant.example",
            rails=[
                PaymentHeadersRail(rail="tempo-mainnet", amount_usd=25, recipient=TEMPO_ADDR),
                PaymentHeadersRail(rail="x402-base-mainnet", amount_usd=25, recipient=BASE_ADDR),
                PaymentHeadersRail(rail="stripe", amount_usd=25, network_id=STRIPE_PROFILE_ID),
            ],
            x402=X402AcceptsBlock(accepts=x402_accepts, version=1),
        ))
    """
    directives = []
    for rail in input.rails:
        directive_input = BuildPaymentDirectiveInput(
            id=f"{input.order_id}-{rail.rail}",
            realm=input.realm,
            rail=rail.rail,
            amount_usd=rail.amount_usd,
            recipient=rail.recipient,
            network_id=rail.network_id,
            chain_id=rail.chain_id,
            currency=rail.currency,
            decimals=rail.decimals,
            method=rail.method,
            intent=rail.intent,
            expires=rail.expires,
        )
        directives.append(build_payment_directive(directive_input))

    result: PaymentHeadersResult = {"www_authenticate": www_authenticate_header(directives)}

    if input.x402 is not None:
        result["payment_required"] = payment_required_header(
            PaymentRequiredHeaderInput(
                x402_version=input.x402.version,
                accepts=input.x402.accepts,
                resource=input.x402.resource,
            ),
        )

    return result


__all__ = [
    "BuildPaymentHeadersInput",
    "PaymentHeadersRail",
    "PaymentHeadersResult",
    "X402AcceptsBlock",
    "build_payment_headers",
]
