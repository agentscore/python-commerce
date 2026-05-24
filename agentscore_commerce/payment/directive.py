"""paymentauth.org Payment directive builders.

`build_payment_request_blob` produces the base64url-encoded request blob; `payment_directive`
formats the WWW-Authenticate directive string. `build_payment_directive` does both in one call.
"""

import base64
import json
from datetime import UTC, datetime, timedelta

from agentscore_commerce.payment.amounts import usd_to_atomic
from agentscore_commerce.payment.rails import lookup_rail


def build_payment_request_blob(
    *,
    amount_usd: float | str,
    rail: str | None = None,
    currency: str | None = None,
    decimals: int | None = None,
    recipient: str | None = None,
    chain_id: int | None = None,
    # Stripe profile_id; camelCase per link-cli mpp decode validator.
    network_id: str | None = None,
) -> str:
    """Build the base64url-encoded `request` blob for an MPP Payment directive."""
    rail_def = lookup_rail(rail) if rail else None
    resolved_decimals = decimals if decimals is not None else (rail_def.decimals if rail_def else 6)
    resolved_currency = currency or (rail_def.currency if rail_def else "usd")
    resolved_chain_id = chain_id if chain_id is not None else (rail_def.chain_id if rail_def else None)

    # Shared ROUND_HALF_UP converter so half-base-unit amounts round deterministically.
    amount_raw = str(usd_to_atomic(amount_usd, decimals=resolved_decimals))
    blob: dict[str, object] = {"amount": amount_raw, "currency": resolved_currency, "decimals": resolved_decimals}
    if recipient:
        blob["recipient"] = recipient
    method_details: dict[str, object] = {}
    if resolved_chain_id is not None:
        method_details["chainId"] = resolved_chain_id
    if network_id:
        method_details["networkId"] = network_id
    if method_details:
        blob["methodDetails"] = method_details

    raw = json.dumps(blob, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def payment_directive(
    *,
    id: str,
    realm: str,
    request: str,
    rail: str | None = None,
    method: str | None = None,
    intent: str = "charge",
    expires: str | None = None,
) -> str:
    """Format an MPP Payment directive string for the WWW-Authenticate header."""
    rail_def = lookup_rail(rail) if rail else None
    resolved_method = method or (rail_def.method if rail_def else "unknown")
    resolved_expires = expires or (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    return (
        f'Payment id="{id}", realm="{realm}", method="{resolved_method}", '
        f'intent="{intent}", expires="{resolved_expires}", request="{request}"'
    )


def build_payment_directive(
    *,
    rail: str,
    id: str,
    realm: str,
    amount_usd: float | str,
    currency: str | None = None,
    decimals: int | None = None,
    recipient: str | None = None,
    chain_id: int | None = None,
    network_id: str | None = None,
    method: str | None = None,
    intent: str = "charge",
    expires: str | None = None,
) -> str:
    """Convenience: build the request blob + directive in one call."""
    request = build_payment_request_blob(
        rail=rail,
        amount_usd=amount_usd,
        currency=currency,
        decimals=decimals,
        recipient=recipient,
        chain_id=chain_id,
        network_id=network_id,
    )
    return payment_directive(
        rail=rail,
        id=id,
        realm=realm,
        method=method,
        intent=intent,
        expires=expires,
        request=request,
    )
