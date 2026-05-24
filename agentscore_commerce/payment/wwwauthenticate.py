"""WWW-Authenticate + PAYMENT-REQUIRED header builders."""

import base64
import json
from typing import Any, Literal


def www_authenticate_header(directives: list[str]) -> str:
    """Join multiple Payment directives into a single WWW-Authenticate header value.

    Per RFC 7235, multiple challenges are comma-separated.
    """
    return ", ".join(directives)


def alias_amount_fields(accepts: list[Any]) -> list[Any]:
    """Add the v1<->v2 amount-field alias to each accepts entry. Idempotent.

    Opt-in helper: the 402 emitters (``payment_required_header`` / ``build_402_body``)
    do NOT call this. Strict x402 v2 settlement matches the agent's echoed requirement
    against the server's rebuilt one by exact comparison, so an extra ``maxAmountRequired``
    the rebuild lacks silently fails settle — keep emitted ``accepts`` as
    ``build_payment_requirements`` produced them. Call this only for a client hardcoded
    to read ``maxAmountRequired`` regardless of ``x402Version``.
    """
    out: list[Any] = []
    for entry in accepts:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        has_amount = "amount" in entry
        has_max_amount = "maxAmountRequired" in entry
        if has_amount and not has_max_amount:
            out.append({**entry, "maxAmountRequired": entry["amount"]})
        elif has_max_amount and not has_amount:
            out.append({**entry, "amount": entry["maxAmountRequired"]})
        else:
            out.append(entry)
    return out


def payment_required_header(
    *,
    x402_version: Literal[1, 2],
    accepts: list[Any],
    resource: dict[str, str] | None = None,
) -> str:
    """Encode the standard x402 PAYMENT-REQUIRED header (base64-encoded JSON).

    Do NOT add a v1<->v2 amount-field alias here. Strict x402 v2 settlement matches the
    agent's echoed ``accepted`` requirement against the server's freshly rebuilt
    requirement by exact comparison, so an extra ``maxAmountRequired`` on the wire that
    the rebuild does not carry makes the match silently fail at settle. Keep ``accepts``
    identical to what ``build_payment_requirements`` produces. ``alias_amount_fields``
    stays exported as an explicit opt-in for callers whose client is hardcoded to read
    ``maxAmountRequired`` regardless of ``x402Version``.
    """
    body: dict[str, Any] = {"x402Version": x402_version, "accepts": accepts}
    if resource is not None:
        body["resource"] = resource
    raw = json.dumps(body, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()
