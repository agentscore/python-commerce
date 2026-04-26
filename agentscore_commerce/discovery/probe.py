"""Discovery probe — answers empty-body POSTs from MPP crawlers (mppscan, link-cli) with a sample 402."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from agentscore_commerce.payment.directive import (
    PaymentDirectiveInput,
    PaymentRequestInput,
    build_payment_request_blob,
    payment_directive,
)


@dataclass
class DiscoveryProbeOptions:
    realm: str
    sample_rail: str
    sample_amount_usd: float
    sample_recipient: str
    intent: str = "charge"
    ttl_seconds: int = 300
    docs_url: str | None = None
    message: str | None = None


@dataclass
class DiscoveryProbeResponse:
    status: int
    headers: dict[str, str]
    body: str


def build_discovery_probe_response(opts: DiscoveryProbeOptions) -> DiscoveryProbeResponse:
    """Build a 402 response advertising a sample Payment challenge for crawler indexing."""
    probe_id = f"probe_{int(datetime.now(UTC).timestamp() * 1000)}"
    expires = (datetime.now(UTC) + timedelta(seconds=opts.ttl_seconds)).isoformat().replace("+00:00", "Z")
    request = build_payment_request_blob(
        PaymentRequestInput(rail=opts.sample_rail, amount_usd=opts.sample_amount_usd, recipient=opts.sample_recipient)
    )
    directive = payment_directive(
        PaymentDirectiveInput(
            rail=opts.sample_rail, id=probe_id, realm=opts.realm, intent=opts.intent, expires=expires, request=request
        )
    )
    body_obj: dict[str, Any] = {
        "error": {
            "code": "payment_required",
            "message": opts.message
            or "This endpoint requires payment. Send a valid request body to receive a full challenge.",
        },
        "discovery": True,
    }
    if opts.docs_url:
        body_obj["docs"] = opts.docs_url
    return DiscoveryProbeResponse(
        status=402,
        headers={"content-type": "application/json", "www-authenticate": directive},
        body=json.dumps(body_obj, separators=(",", ":")),
    )


class _RequestLike(Protocol):
    method: str

    def headers_get(self, name: str) -> str | None: ...

    async def body_text(self) -> str: ...


async def is_discovery_probe_request(method: str, authorization: str | None, body_text: str) -> bool:
    """Return True for an empty-body POST without a Payment Authorization header — the MPP crawler probe pattern.

    Framework-agnostic — pass extracted method, Authorization header value, and body text. Vendors wire
    this against their framework's request object.
    """
    if method.upper() != "POST":
        return False
    if authorization and authorization.startswith("Payment "):
        return False
    return not body_text or body_text.strip() == "{}"
