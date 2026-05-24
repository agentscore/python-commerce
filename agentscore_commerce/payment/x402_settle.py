"""``process_x402_settle``: single-call x402 verify+settle for merchants.

Wraps the four x402-server steps every x402-accepting merchant repeats against
``x402.x402ResourceServer`` (sync ``build_payment_requirements`` + sync
``enrich_extensions`` + async ``verify_payment`` + async ``settle_payment``):

1. ``build_payment_requirements(resource_config)``: builds the requirement entries the
   facilitator validates against
2. ``enrich_extensions(declared, transport_context)``: folds in Bazaar (or other)
   extensions for the verify step (only when ``input.extension`` is supplied)
3. ``verify_payment(payload, matched_requirement)``: runs verify against the facilitator
4. ``settle_payment(payload, matched_requirement)``: settles on-chain

Accepts ``resource_config`` as either a ``dict`` (JS-style with ``payTo`` /
``maxTimeoutSeconds`` camelCase keys) or an ``x402.schemas.config.ResourceConfig``;
dicts are coerced before the build step so callers don't have to import the x402 type.

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
      (build requirements, extension enrich, or verify_payment). Most common cause:
      facilitator client rejects the configured network. Log raw ``error`` server-side;
      map to a controlled 503 so the agent can pick a different rail. ``step`` indicates
      which verify-stage call raised.
    """

    phase: Literal["no_requirements", "verify_failed", "settle_failed", "facilitator_error"]
    success: Literal[False] = False
    reason: str | None = None
    verify_result: Any = None
    error: Any = None
    matched_requirement: Any = None
    #: Populated only when ``phase == "facilitator_error"``. Indicates which verify-stage
    #: call raised: ``"build_requirements"`` / ``"enrich_extensions"`` / ``"verify_payment"``.
    step: Literal["build_requirements", "enrich_extensions", "verify_payment"] | None = None
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


def classify_orchestration_error(err: BaseException | str) -> ClassifiedX402Error | None:
    """Classify a thrown error during the 402 orchestration.

    Catches errors that escape ``process_x402_settle`` (e.g. raised by ``mppx.compose``,
    a Stripe SDK call, or any other payment-side library code wrapped in a single
    ``try/except`` around the full settle flow). Returns a :class:`ClassifiedX402Error`
    when the error message matches a known pattern; ``None`` otherwise.

    Callers should rethrow on ``None`` — this helper never swallows unknown errors.
    The typical pattern::

        try:
            ...
        except Exception as exc:
            classified = classify_orchestration_error(exc)
            if classified is not None:
                return JSONResponse(
                    {"error": {"code": classified.code, "message": classified.message},
                     "next_steps": classified.next_steps},
                    status_code=classified.status,
                )
            log.error("unclassified payment error: %s", exc)
            raise

    Pattern matching is case-insensitive substring on the error message:

    * ``"x402version"`` / ``"invalid payment"`` / ``"unsupported x402"`` →
      400 ``payment_proof_invalid`` / ``regenerate_payment_credential``
    * ``"stripe"`` / ``"facilitator"`` / ``"cdp"`` →
      503 ``payment_provider_unavailable`` / ``retry_or_swap_method``
    * Anything else → ``None`` (caller rethrows)

    Substring matching is intentionally narrow. New error families should land here
    explicitly rather than have the helper grow opaque heuristics. For tagged failure
    results that already classify themselves, use :func:`classify_x402_settle_result`.
    """
    msg = str(err) if isinstance(err, BaseException) else err
    if not isinstance(msg, str):
        return None
    msg_lower = msg.lower()

    if any(needle in msg_lower for needle in ("x402version", "invalid payment", "unsupported x402")):
        return ClassifiedX402Error(
            status=400,
            code="payment_proof_invalid",
            message="Payment credential is malformed or uses an unsupported version",
            next_steps={
                "action": "regenerate_payment_credential",
                "user_message": (
                    "The payment credential is malformed or uses an unsupported version. "
                    "Regenerate from a fresh 402 challenge and re-sign."
                ),
            },
        )

    if any(needle in msg_lower for needle in ("stripe", "facilitator", "cdp")):
        return ClassifiedX402Error(
            status=503,
            code="payment_provider_unavailable",
            message="Payment provider returned an error",
            next_steps={
                "action": "retry_or_swap_method",
                "retry_after_seconds": 10,
                "user_message": (
                    "Transient payment-provider error. Retry in a few seconds, "
                    "or pick a different rail from the 402 challenge."
                ),
            },
        )

    return None


def coerce_resource_config(config: Any) -> Any:
    """Best-effort dict → x402 ``ResourceConfig`` coercion.

    Consumers ported from a JS / Hono stack often pass a plain dict with JS-style
    ``payTo`` / ``maxTimeoutSeconds`` camelCase keys. x402's Python ``ResourceConfig``
    is a Pydantic model with
    ``pay_to`` / ``max_timeout_seconds`` snake_case fields, and ``build_payment_requirements``
    does ``config.network`` attribute access — so a raw dict raises
    ``AttributeError("'dict' object has no attribute 'network'")``. Coerce here so callers
    can pass either shape.

    Falls back to the original input on any failure (missing peer dep, validation error)
    so caller-side typed instances still pass through unchanged.
    """
    if not isinstance(config, dict):
        return config
    try:
        from x402.schemas.config import ResourceConfig
    except ImportError:
        return config
    coerced = dict(config)
    if "payTo" in coerced and "pay_to" not in coerced:
        coerced["pay_to"] = coerced.pop("payTo")
    if "maxTimeoutSeconds" in coerced and "max_timeout_seconds" not in coerced:
        coerced["max_timeout_seconds"] = coerced.pop("maxTimeoutSeconds")
    try:
        return ResourceConfig(**coerced)
    except Exception:
        return config


def coerce_payment_payload(payload: Any) -> Any:
    """Best-effort dict → x402 ``PaymentPayload`` (v1 or v2) coercion.

    ``verify_x402_request`` returns ``payload`` as a plain dict (the result of
    ``json.loads(base64.b64decode(X-Payment))``), but x402 2.9's
    ``server.verify_payment`` / ``server.settle_payment`` call ``payload.get_scheme()``
    and other typed-model methods on it. Without coercion, the dict raises
    ``AttributeError("'dict' object has no attribute 'get_scheme'")`` on the verify leg.

    Routes by the ``x402Version`` field: ``1`` → ``PaymentPayloadV1`` (flat shape with
    top-level ``scheme`` / ``network``); anything else → ``PaymentPayload`` (v2 shape
    nested under ``accepted``). Falls back to the original dict on any failure so callers
    that already pass typed instances or unusual shapes still flow through unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    try:
        from x402.schemas import PaymentPayload
        from x402.schemas.v1 import PaymentPayloadV1
    except ImportError:
        return payload
    version = payload.get("x402Version")
    model = PaymentPayloadV1 if version == 1 else PaymentPayload
    try:
        return model.model_validate(payload)
    except Exception:
        return payload


async def process_x402_settle(
    *,
    x402_server: Any,
    payload: Any,
    resource_config: Any,
    resource_meta: dict[str, str],
    extension: Any = None,
    transport_context: Any = None,
) -> ProcessX402SettleResult:
    """Run the x402 verify→settle flow and return a tagged outcome.

    ``resource_config`` accepts either a ``dict`` (JS-style with ``payTo`` /
    ``maxTimeoutSeconds`` camelCase keys) or an x402 ``ResourceConfig`` instance —
    dicts are coerced before the build step.

    Set ``extension`` to fold a Bazaar (or other) extension into the verify step;
    ``transport_context`` defaults to a POST context derived from
    ``resource_meta["url"]`` when an extension is supplied.
    """
    server = x402_server
    coerced_config = coerce_resource_config(resource_config)
    coerced_payload = coerce_payment_payload(payload)

    try:
        built_requirements = server.build_payment_requirements(coerced_config)
    except Exception as err:
        return ProcessX402SettleFailure(phase="facilitator_error", step="build_requirements", error=err)
    if not built_requirements:
        return ProcessX402SettleFailure(
            phase="no_requirements",
            reason="x402_server.build_payment_requirements returned empty",
        )
    matched_requirement = built_requirements[0]

    # Per-request extension enrichment runs only when a caller explicitly attaches one
    # (e.g. the Bazaar discovery extension). x402 2.9 takes the enriched dict as the
    # second argument to ``build_payment_requirements`` rather than as a verify-step
    # input, but the fold happens at build time — so we replay the build with the
    # enriched extensions and use those requirements going forward.
    if extension is not None:
        resolved_transport_context = transport_context
        if resolved_transport_context is None:
            path = urlparse(resource_meta["url"]).path
            resolved_transport_context = {
                "method": "POST",
                "adapter": {"getPath": lambda: path},
                "routePattern": path,
            }
        try:
            enriched_ext = server.enrich_extensions(extension, resolved_transport_context)
        except Exception as err:
            return ProcessX402SettleFailure(phase="facilitator_error", step="enrich_extensions", error=err)
        try:
            built_requirements = server.build_payment_requirements(
                coerced_config, list(enriched_ext.keys()) if isinstance(enriched_ext, dict) else None
            )
            if built_requirements:
                matched_requirement = built_requirements[0]
        except Exception as err:
            return ProcessX402SettleFailure(phase="facilitator_error", step="build_requirements", error=err)

    # x402 2.9's ``x402ResourceServer`` exposes ``verify_payment(payload, requirements)``
    # — not ``process_payment_request`` (a fictional method that earlier versions of this
    # helper called and only ever worked against test stubs).
    try:
        verify_result = await server.verify_payment(coerced_payload, matched_requirement)
    except Exception as err:
        return ProcessX402SettleFailure(phase="facilitator_error", step="verify_payment", error=err)

    # x402's VerifyResponse exposes ``is_valid``; some stubs / older facilitators expose
    # ``success``. Accept either.
    is_valid = (
        getattr(verify_result, "is_valid", None)
        if hasattr(verify_result, "is_valid")
        else (verify_result.get("is_valid") if isinstance(verify_result, dict) else None)
    )
    if is_valid is None:
        is_valid = (
            getattr(verify_result, "success", None)
            if hasattr(verify_result, "success")
            else (verify_result.get("success") if isinstance(verify_result, dict) else False)
        )
    if not is_valid:
        return ProcessX402SettleFailure(phase="verify_failed", verify_result=verify_result)

    try:
        settle_result = await server.settle_payment(coerced_payload, matched_requirement)
        payment_response_header: str | None = None
        if settle_result is not None:
            payment_response_header = base64.b64encode(settle_result_to_json_bytes(settle_result)).decode()
        return ProcessX402SettleSuccess(
            matched_requirement=matched_requirement,
            settle_result=settle_result,
            payment_response_header=payment_response_header,
            verify_result=verify_result,
        )
    except Exception as err:
        return ProcessX402SettleFailure(phase="settle_failed", error=err, matched_requirement=matched_requirement)


def settle_result_to_json_bytes(settle_result: Any) -> bytes:
    """Serialize the settle result to a base64-friendly JSON byte string.

    Pydantic ``SettleResponse`` (x402's wire shape) goes through ``model_dump_json
    (by_alias=True)`` so emitted keys match the wire shape (``errorReason`` /
    ``errorMessage`` rather than the snake_case attrs). Plain dicts fall through
    to ``json.dumps``.
    """
    model_dump_json = getattr(settle_result, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(by_alias=True).encode()
    return json.dumps(settle_result, separators=(",", ":")).encode()
