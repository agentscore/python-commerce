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
    """Add the v1↔v2 amount-field alias to each accepts entry. Idempotent.

    Used by both ``payment_required_header`` (header emit) and ``build_402_body``
    (body emit) so every x402 entry on the wire carries BOTH ``amount`` (v2 spec)
    AND ``maxAmountRequired`` (v1 spec). Strict v1-only parsers (e.g. Coinbase
    awal at ``payments-mcp.coinbase.com``, hardcoded to read ``maxAmountRequired``)
    work alongside strict v2 parsers, which ignore the alias.
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

    Each accepts entry is post-processed via :func:`alias_amount_fields` so v1-only
    clients (e.g. awal) and v2-strict clients can both read it.
    """
    body: dict[str, Any] = {"x402Version": x402_version, "accepts": alias_amount_fields(accepts)}
    if resource is not None:
        body["resource"] = resource
    raw = json.dumps(body, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()
