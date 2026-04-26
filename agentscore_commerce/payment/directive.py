"""paymentauth.org Payment directive builders.

`build_payment_request_blob` produces the base64url-encoded request blob; `payment_directive`
formats the WWW-Authenticate directive string. `build_payment_directive` does both in one call.
"""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agentscore_commerce.payment.rails import lookup_rail


@dataclass
class PaymentRequestInput:
    amount_usd: float | str
    rail: str | None = None
    currency: str | None = None
    decimals: int | None = None
    recipient: str | None = None
    chain_id: int | None = None
    network_id: str | None = None  # Stripe profile_id; camelCase per link-cli mpp decode validator


def build_payment_request_blob(input: PaymentRequestInput) -> str:
    """Build the base64url-encoded `request` blob for an MPP Payment directive."""
    rail_def = lookup_rail(input.rail) if input.rail else None
    decimals = input.decimals if input.decimals is not None else (rail_def.decimals if rail_def else 6)
    currency = input.currency or (rail_def.currency if rail_def else "usd")
    chain_id = input.chain_id if input.chain_id is not None else (rail_def.chain_id if rail_def else None)

    amount_num = float(input.amount_usd) if isinstance(input.amount_usd, str) else input.amount_usd
    amount_raw = str(round(amount_num * 10**decimals))
    blob: dict[str, object] = {"amount": amount_raw, "currency": currency}
    if input.recipient:
        blob["recipient"] = input.recipient
    method_details: dict[str, object] = {}
    if chain_id is not None:
        method_details["chainId"] = chain_id
    if input.network_id:
        method_details["networkId"] = input.network_id
    if method_details:
        blob["methodDetails"] = method_details

    raw = json.dumps(blob, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass
class PaymentDirectiveInput:
    id: str
    realm: str
    request: str
    rail: str | None = None
    method: str | None = None
    intent: str = "charge"
    expires: str | None = None


def payment_directive(input: PaymentDirectiveInput) -> str:
    """Format an MPP Payment directive string for the WWW-Authenticate header."""
    rail_def = lookup_rail(input.rail) if input.rail else None
    method = input.method or (rail_def.method if rail_def else "unknown")
    expires = input.expires or (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    return (
        f'Payment id="{input.id}", realm="{input.realm}", method="{method}", '
        f'intent="{input.intent}", expires="{expires}", request="{input.request}"'
    )


@dataclass
class BuildPaymentDirectiveInput:
    rail: str
    id: str
    realm: str
    amount_usd: float | str
    currency: str | None = None
    decimals: int | None = None
    recipient: str | None = None
    chain_id: int | None = None
    network_id: str | None = None
    method: str | None = None
    intent: str = "charge"
    expires: str | None = None


def build_payment_directive(input: BuildPaymentDirectiveInput) -> str:
    """Convenience: build the request blob + directive in one call."""
    request = build_payment_request_blob(
        PaymentRequestInput(
            rail=input.rail,
            amount_usd=input.amount_usd,
            currency=input.currency,
            decimals=input.decimals,
            recipient=input.recipient,
            chain_id=input.chain_id,
            network_id=input.network_id,
        )
    )
    return payment_directive(
        PaymentDirectiveInput(
            rail=input.rail,
            id=input.id,
            realm=input.realm,
            method=input.method,
            intent=input.intent,
            expires=input.expires,
            request=request,
        )
    )
