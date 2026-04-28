"""Discovery probe — answers empty-body POSTs from MPP crawlers (mppscan, link-cli) with a sample 402."""

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from agentscore_commerce.payment.directive import (
    PaymentDirectiveInput,
    PaymentRequestInput,
    build_payment_request_blob,
    payment_directive,
)
from agentscore_commerce.payment.wwwauthenticate import (
    PaymentRequiredHeaderInput,
    payment_required_header,
)


@dataclass
class X402SampleProbe:
    """Sample x402 accepts to embed in the discovery probe's PAYMENT-REQUIRED header.

    Crawlers (e.g. ``awal x402 details``) can find this endpoint's x402 support
    without a real business-shaped request. Each entry is run through
    ``alias_amount_fields`` so v1-only parsers find ``maxAmountRequired`` and
    v2-strict parsers find ``amount``.
    """

    accepts: list[Any]
    version: Literal[1, 2] = 2
    resource_url: str | None = None


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
    x402_sample: X402SampleProbe | None = field(default=None)


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
    headers: dict[str, str] = {"content-type": "application/json", "www-authenticate": directive}

    if opts.x402_sample is not None:
        x402v = opts.x402_sample.version
        # payment_required_header internally runs alias_amount_fields, so v1+v2
        # parsers both find their expected field name on the header decode.
        header_kwargs: dict[str, Any] = {"x402_version": x402v, "accepts": opts.x402_sample.accepts}
        if opts.x402_sample.resource_url:
            header_kwargs["resource"] = {
                "url": opts.x402_sample.resource_url,
                "mimeType": "application/json",
            }
        encoded = payment_required_header(PaymentRequiredHeaderInput(**header_kwargs))
        headers["payment-required"] = encoded
        # Mirror the aliased accepts in the body so clients that fall back from
        # header → body (e.g. awal's discover) can still extract requirements.
        decoded = json.loads(base64.b64decode(encoded).decode())
        body_obj["x402Version"] = x402v
        body_obj["accepts"] = decoded["accepts"]

    return DiscoveryProbeResponse(
        status=402,
        headers=headers,
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
