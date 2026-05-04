"""``process_x402_settle``: single-call x402 verify+settle for merchants.

Wraps the four x402-server steps every x402-accepting merchant repeats:

1. ``build_payment_requirements(resource_config)``: builds the requirement entries the
   facilitator validates against
2. ``enrich_extensions(extension, transport_context)``: folds in Bazaar (or other)
   extensions for the verify step
3. ``process_payment_request(payload, resource_config, resource_meta, extensions)``:
   runs verify against the facilitator
4. ``settle_payment(payload, matched_requirement)``: settles on-chain

Returns a tagged result so the caller can map errors to merchant-shaped responses
without owning the orchestration boilerplate. Use :func:`classify_x402_settle_result`
to map the tagged result to a recommended HTTP response.
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
    """Failure outcome from :func:`process_x402_settle`.

    Phases:

    - ``no_requirements``: ``build_payment_requirements`` returned an empty array;
      merchant-side misconfiguration. Log ``reason`` server-side; map to a controlled
      500 to the consumer via :func:`classify_x402_settle_result`.
    - ``verify_failed``: facilitator's verify step ran and returned ``{success: False}``.
      Log ``verify_result`` server-side; map to a controlled 400 with
      ``payment_proof_invalid`` to the consumer.
    - ``settle_failed``: verify succeeded but ``settle_payment`` raised. Log raw
      ``error`` server-side; map to a controlled 503 with
      ``payment_provider_unavailable``.
    - ``facilitator_error``: facilitator raised during one of the verify-stage calls
      (build requirements, extension enrich, or process_payment_request). Most common
      cause: facilitator client rejects the configured network. Log raw ``error``
      server-side; map to a controlled 503 so the agent can pick a different rail.
      ``step`` indicates which verify-stage call raised.
    """

    phase: Literal["no_requirements", "verify_failed", "settle_failed", "facilitator_error"]
    success: Literal[False] = False
    reason: str | None = None
    verify_result: Any = None
    error: Any = None
    matched_requirement: Any = None
    #: Populated only when ``phase == "facilitator_error"``. Indicates which verify-stage
    #: call raised: ``"build_requirements"`` / ``"enrich_extensions"`` / ``"process_payment_request"``.
    step: Literal["build_requirements", "enrich_extensions", "process_payment_request"] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


ProcessX402SettleResult = ProcessX402SettleSuccess | ProcessX402SettleFailure


@dataclass
class ClassifiedX402Error:
    """Merchant-shaped response for a non-success :class:`ProcessX402SettleResult`.

    ``status`` / ``code`` / ``message`` are safe to send back to the consumer.
    ``next_steps`` is the agent-instructions block describing what the agent should do
    next. Raw facilitator errors stay server-side: do NOT serialize the original
    ``error`` / ``verify_result`` / ``reason`` to the consumer; log them yourself.
    """

    status: Literal[400, 500, 503]
    code: Literal["payment_proof_invalid", "payment_provider_unavailable", "payment_internal_error"]
    message: str
    next_steps: dict[str, Any]


def classify_x402_settle_result(result: ProcessX402SettleResult) -> ClassifiedX402Error | None:
    """Map a :class:`ProcessX402SettleResult` to the recommended merchant response.

    Returns ``None`` for success. For each error phase, returns a controlled
    status / code / message / next_steps tuple. Replaces error-message string
    matching with phase-based dispatch so merchants stop coupling to
    facilitator-specific error text.

    Phase mapping:

    - ``verify_failed``: 400 ``payment_proof_invalid`` / ``regenerate_payment_credential``
    - ``facilitator_error``: 503 ``payment_provider_unavailable`` / ``try_different_rail``
    - ``settle_failed``: 503 ``payment_provider_unavailable`` / ``retry_or_swap_method``
    - ``no_requirements``: 500 ``payment_internal_error`` / ``contact_support``

    Always log the raw ``result`` server-side before responding; the returned
    object is intentionally facilitator-agnostic and never carries raw error detail.
    """
    if isinstance(result, ProcessX402SettleSuccess):
        return None
    if result.phase == "no_requirements":
        return ClassifiedX402Error(
            status=500,
            code="payment_internal_error",
            message="Failed to build x402 payment requirements for this configuration",
            next_steps={
                "action": "contact_support",
                "user_message": (
                    "The merchant could not produce a payment challenge for this request. "
                    "Try again later or contact support."
                ),
            },
        )
    if result.phase == "verify_failed":
        return ClassifiedX402Error(
            status=400,
            code="payment_proof_invalid",
            message="Payment credential failed verification; regenerate from a fresh 402 challenge",
            next_steps={
                "action": "regenerate_payment_credential",
                "user_message": (
                    "The payment credential was rejected at verify time. "
                    "Discard it, fetch a fresh 402 challenge, and re-sign."
                ),
            },
        )
    if result.phase == "facilitator_error":
        return ClassifiedX402Error(
            status=503,
            code="payment_provider_unavailable",
            message="Payment provider could not process this network configuration",
            next_steps={
                "action": "try_different_rail",
                "user_message": (
                    "This rail is currently unavailable. Pick a different rail from the 402 challenge and retry."
                ),
            },
        )
    if result.phase == "settle_failed":
        return ClassifiedX402Error(
            status=503,
            code="payment_provider_unavailable",
            message="Payment credential verified but on-chain settlement failed",
            next_steps={
                "action": "retry_or_swap_method",
                "retry_after_seconds": 10,
                "user_message": (
                    "Transient settlement error. Retry in a few seconds, "
                    "or pick a different rail from the 402 challenge."
                ),
            },
        )
    return None


async def process_x402_settle(input: ProcessX402SettleInput) -> ProcessX402SettleResult:
    """Run the x402 verify→settle flow and return a tagged outcome."""
    server = input.x402_server

    try:
        built_requirements = await server.build_payment_requirements(input.resource_config)
    except Exception as err:
        return ProcessX402SettleFailure(phase="facilitator_error", step="build_requirements", error=err)
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

    try:
        enriched_ext = (
            server.enrich_extensions(input.extension, transport_context) if input.extension is not None else None
        )
    except Exception as err:
        return ProcessX402SettleFailure(phase="facilitator_error", step="enrich_extensions", error=err)

    try:
        verify_result = await server.process_payment_request(
            input.payload, input.resource_config, input.resource_meta, enriched_ext
        )
    except Exception as err:
        return ProcessX402SettleFailure(phase="facilitator_error", step="process_payment_request", error=err)

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
