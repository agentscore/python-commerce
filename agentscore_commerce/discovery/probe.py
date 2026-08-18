"""Discovery probe — answers empty-body POSTs from MPP crawlers (mppscan, link-cli) with a sample 402."""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from agentscore_commerce.payment.directive import (
    build_payment_request_blob,
    payment_directive,
)
from agentscore_commerce.payment.networks import networks
from agentscore_commerce.payment.usdc import USDC
from agentscore_commerce.payment.wwwauthenticate import payment_required_header

# Placeholder payTo for x402 sample accepts in the discovery probe — the probe
# exists for crawlers to find that we support x402, not for actual payment.
_ZERO_EVM_PAYTO = "0x0000000000000000000000000000000000000000"
_ZERO_SOLANA_PAYTO = "11111111111111111111111111111111"


def sample_x402_accept_for_network(caip2: str, amount_atomic: str = "1000000") -> dict[str, Any] | None:
    """Build a sample x402 accepts entry for a known CAIP-2 network using the USDC registry.

    Returns None for networks not in the registry — vendors with custom networks
    should construct accepts entries by hand and pass them via ``x402_sample.accepts``.
    """
    if caip2 == networks.base.mainnet.caip2:
        return {
            "scheme": "exact",
            "network": caip2,
            "amount": amount_atomic,
            "asset": USDC.base.mainnet.address,
            "payTo": _ZERO_EVM_PAYTO,
            "maxTimeoutSeconds": 300,
            # ``extra.name`` mirrors the on-chain USDC contract's ``name()`` return value
            # because EIP-712 domain hashes include this string. Wrong name → every
            # signed payload fails facilitator verify with ``invalid_exact_evm_payload_signature``.
            # Base mainnet USDC returns "USD Coin"; base sepolia USDC returns "USDC".
            "extra": {"name": "USD Coin", "version": "2"},
        }
    if caip2 == networks.base.sepolia.caip2:
        return {
            "scheme": "exact",
            "network": caip2,
            "amount": amount_atomic,
            "asset": USDC.base.sepolia.address,
            "payTo": _ZERO_EVM_PAYTO,
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USDC", "version": "2"},
        }
    if caip2 == networks.solana.mainnet.caip2:
        return {
            "scheme": "exact",
            "network": caip2,
            "amount": amount_atomic,
            "asset": USDC.solana.mainnet.mint,
            "payTo": _ZERO_SOLANA_PAYTO,
            "maxTimeoutSeconds": 300,
        }
    if caip2 == networks.solana.devnet.caip2:
        return {
            "scheme": "exact",
            "network": caip2,
            "amount": amount_atomic,
            "asset": USDC.solana.devnet.mint,
            "payTo": _ZERO_SOLANA_PAYTO,
            "maxTimeoutSeconds": 300,
        }
    return None


@dataclass
class X402SampleProbe:
    """Sample x402 accepts to embed in the discovery probe's PAYMENT-REQUIRED header.

    Crawlers (e.g. ``awal x402 details``) can find this endpoint's x402 support
    without a real business-shaped request. Entries are emitted as-is in their
    declared ``x402Version`` shape (v2 ``amount``); clients version-route on
    ``x402Version``.

    Pass ``networks`` (shorthand) for the common case — each CAIP-2 network is
    mapped to a sample USDC accepts entry via ``sample_x402_accept_for_network``.
    Or pass ``accepts`` directly for full control over the sample shape.
    """

    networks: list[str] | None = None
    accepts: list[Any] | None = None
    amount_atomic: str = "1000000"
    version: Literal[1, 2] = 2
    resource_url: str | None = None
    resource: dict[str, Any] | None = None
    """Full x402 v2 ResourceInfo for the sample envelope; overrides ``resource_url``.
    When neither is set a minimal resource is synthesized from the realm: v2
    envelope validators (mppx, x402scan's shared engine) hard-require ``resource``,
    so a resource-less sample header reads as "no valid x402 response" however
    correct the accepts are."""
    extensions: dict[str, Any] | None = None
    """x402 v2 ``extensions`` for the sample envelope (header AND body), e.g. the
    Bazaar block with input/output schemas. Discovery validators read the example
    input from here to build VALID bodies for their follow-up probes. ``Checkout``
    fills this from its own ``discovery_extensions`` automatically."""


@dataclass
class DiscoveryProbeResponse:
    status: int
    headers: dict[str, str]
    body: str


def build_discovery_probe_response(
    *,
    realm: str,
    sample_rail: str,
    sample_amount_usd: float,
    sample_recipient: str,
    intent: str = "charge",
    ttl_seconds: int = 300,
    docs_url: str | None = None,
    message: str | None = None,
    x402_sample: X402SampleProbe | None = None,
) -> DiscoveryProbeResponse:
    """Build a 402 response advertising a sample Payment challenge for crawler indexing."""
    probe_id = f"probe_{int(datetime.now(UTC).timestamp() * 1000)}"
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    request = build_payment_request_blob(rail=sample_rail, amount_usd=sample_amount_usd, recipient=sample_recipient)
    directive = payment_directive(
        rail=sample_rail, id=probe_id, realm=realm, intent=intent, expires=expires, request=request
    )
    body_obj: dict[str, Any] = {
        "error": {
            "code": "payment_required",
            "message": message
            or "This endpoint requires payment. Send a valid request body to receive a full challenge.",
        },
        "discovery": True,
    }
    if docs_url:
        body_obj["docs"] = docs_url
    headers: dict[str, str] = {"content-type": "application/json", "www-authenticate": directive}

    if x402_sample is not None:
        x402v = x402_sample.version
        if x402_sample.accepts is not None:
            sample_accepts: list[Any] = x402_sample.accepts
        else:
            sample_accepts = [
                e
                for n in (x402_sample.networks or [])
                for e in [sample_x402_accept_for_network(n, x402_sample.amount_atomic)]
                if e is not None
            ]
        # Emit the sample accepts as-is (no v1<->v2 amount alias) so the probe
        # sample matches what the real 402 emits; clients version-route on x402Version.
        # The v2 envelope REQUIRES ``resource``: validators (mppx, x402scan's shared
        # engine) refuse a resource-less PAYMENT-REQUIRED header outright, so when
        # the caller supplied neither form, synthesize a minimal one from the realm.
        if x402_sample.resource is not None:
            resource = x402_sample.resource
        elif x402_sample.resource_url:
            resource = {"url": x402_sample.resource_url, "mimeType": "application/json"}
        else:
            realm_url = realm if realm.startswith("http") else f"https://{realm}"
            resource = {"url": realm_url, "mimeType": "application/json"}
        header_kwargs: dict[str, Any] = {
            "x402_version": x402v,
            "accepts": sample_accepts,
            "resource": resource,
        }
        if x402_sample.extensions:
            header_kwargs["extensions"] = x402_sample.extensions
        encoded = payment_required_header(**header_kwargs)
        headers["payment-required"] = encoded
        # Mirror the header's accepts in the body so clients that fall back from
        # header → body (e.g. awal's discover) can still extract requirements.
        decoded = json.loads(base64.b64decode(encoded).decode())
        body_obj["x402Version"] = x402v
        body_obj["accepts"] = decoded["accepts"]
        body_obj["resource"] = decoded["resource"]
        if "extensions" in decoded:
            body_obj["extensions"] = decoded["extensions"]

    return DiscoveryProbeResponse(
        status=402,
        headers=headers,
        body=json.dumps(body_obj, separators=(",", ":")),
    )


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
