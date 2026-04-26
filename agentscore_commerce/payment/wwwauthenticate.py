"""WWW-Authenticate + PAYMENT-REQUIRED header builders."""

import base64
import json
from dataclasses import dataclass
from typing import Any, Literal


def www_authenticate_header(directives: list[str]) -> str:
    """Join multiple Payment directives into a single WWW-Authenticate header value.

    Per RFC 7235, multiple challenges are comma-separated.
    """
    return ", ".join(directives)


@dataclass
class PaymentRequiredHeaderInput:
    x402_version: Literal[1, 2]
    accepts: list[Any]
    resource: dict[str, str] | None = None


def payment_required_header(input: PaymentRequiredHeaderInput) -> str:
    """Encode the standard x402 PAYMENT-REQUIRED header (base64-encoded JSON)."""
    body: dict[str, Any] = {"x402Version": input.x402_version, "accepts": input.accepts}
    if input.resource is not None:
        body["resource"] = input.resource
    raw = json.dumps(body, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()
