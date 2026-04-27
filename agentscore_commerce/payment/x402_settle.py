"""``process_x402_settle`` — single-call x402 verify+settle for merchants.

Wraps the four x402-server steps every x402-accepting merchant repeats:

1. ``build_payment_requirements(resource_config)`` — builds the requirement entries the
   facilitator validates against
2. ``enrich_extensions(extension, transport_context)`` — folds in Bazaar (or other)
   extensions for the verify step
3. ``process_payment_request(payload, resource_config, resource_meta, extensions)`` —
   runs verify against the facilitator
4. ``settle_payment(payload, matched_requirement)`` — settles on-chain

Returns a tagged result so the caller can map errors to merchant-shaped responses
without owning the orchestration boilerplate.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse


@dataclass
class ProcessX402SettleInput:
    """Input for :func:`process_x402_settle`."""

    #: The x402 server instance from ``create_x402_server``.
    x402_server: Any
    #: The verified x402 payload extracted from the X-Payment header.
    payload: Any
    #: Resource configuration the facilitator validates against (network, price, payTo,
    #: asset, max_timeout_seconds, etc.). Shape is x402-server-specific.
    resource_config: Any
    #: Resource metadata exposed to the facilitator.
    resource_meta: dict[str, str]
    #: Optional extension to enrich during verify (e.g. Bazaar).
    extension: Any = None
    #: Transport context for the extension enrich step. Defaults to ``{"method": "POST",
    #: "adapter": {"getPath": <pathname>}, "routePattern": <pathname>}`` derived from
    #: ``resource_meta["url"]``.
    transport_context: Any = None


@dataclass
class ProcessX402SettleSuccess:
    """Success outcome from :func:`process_x402_settle`."""

    matched_requirement: Any
    settle_result: Any
    payment_response_header: str | None
    verify_result: Any
    success: Literal[True] = True


@dataclass
class ProcessX402SettleFailure:
    """Failure outcome from :func:`process_x402_settle`."""

    phase: Literal["no_requirements", "verify_failed", "settle_failed"]
    success: Literal[False] = False
    reason: str | None = None
    verify_result: Any = None
    error: Any = None
    matched_requirement: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


ProcessX402SettleResult = ProcessX402SettleSuccess | ProcessX402SettleFailure


async def process_x402_settle(input: ProcessX402SettleInput) -> ProcessX402SettleResult:
    """Run the x402 verify→settle flow and return a tagged outcome."""
    server = input.x402_server

    built_requirements = await server.build_payment_requirements(input.resource_config)
    if not built_requirements:
        return ProcessX402SettleFailure(
            phase="no_requirements",
            reason="x402_server.build_payment_requirements returned empty",
        )
    matched_requirement = built_requirements[0]

    transport_context = input.transport_context
    if transport_context is None:
        path = urlparse(input.resource_meta["url"]).path
        transport_context = {
            "method": "POST",
            "adapter": {"getPath": lambda: path},
            "routePattern": path,
        }

    enriched_ext = server.enrich_extensions(input.extension, transport_context) if input.extension is not None else None

    verify_result = await server.process_payment_request(
        input.payload, input.resource_config, input.resource_meta, enriched_ext
    )

    if not getattr(
        verify_result, "success", verify_result.get("success") if isinstance(verify_result, dict) else False
    ):
        return ProcessX402SettleFailure(phase="verify_failed", verify_result=verify_result)

    try:
        settle_result = await server.settle_payment(input.payload, matched_requirement)
        payment_response_header: str | None = None
        if settle_result is not None:
            payload_bytes = json.dumps(settle_result, separators=(",", ":")).encode()
            payment_response_header = base64.b64encode(payload_bytes).decode()
        return ProcessX402SettleSuccess(
            matched_requirement=matched_requirement,
            settle_result=settle_result,
            payment_response_header=payment_response_header,
            verify_result=verify_result,
        )
    except Exception as err:
        return ProcessX402SettleFailure(phase="settle_failed", error=err, matched_requirement=matched_requirement)
