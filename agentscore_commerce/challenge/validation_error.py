"""Structured 4xx validation-error body builder.

Pairs cleanly with the existing 402 / 403 builders. Every commerce merchant
returning helpful ``bad_request`` / ``not_found`` / ``out_of_stock`` errors
converges on the same shape: ``{error: {code, message}, ...optional_hints,
next_steps?}``. This builder doesn't choose the HTTP status — vendors wrap the
returned body in their framework's response (``JSONResponse(body, 400)`` in
FastAPI, etc.). Status stays the merchant's call because the same shape works
for 400/404/409/422.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BuildValidationErrorInput:
    """Inputs for ``build_validation_error``.

    Attributes:
        code: Machine-readable error code (e.g. ``'bad_request'``, ``'not_found'``,
            ``'out_of_stock'``).
        message: Human-readable message — surfaced directly to the user via the agent.
        required_fields: Optional schema description of required body fields, keyed by
            field name. Surfaced so agents can self-correct without fetching docs.
        example_body: Optional concrete example body. Pairs with ``required_fields``
            for max self-serve. Use the ``_HAS_NO_EXAMPLE`` sentinel to omit; pass
            ``None`` to emit a literal ``"example_body": null``.
        next_steps: Optional next-step hint block (``{action, user_message?,
            ...vendor_extras}``).
        extra: Vendor-specific top-level fields merged into the body (e.g. ``available``,
            ``blocked_states``, ``max_length``).
    """

    code: str
    message: str
    required_fields: dict[str, str] | None = None
    # Sentinel: distinguish "no example provided" from "explicit null in body".
    example_body: Any = None
    has_example_body: bool = False
    next_steps: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_validation_error(input: BuildValidationErrorInput) -> dict[str, Any]:
    """Compose a 4xx body that vendors return via their framework's response helper.

    Combine with the merchant's chosen HTTP status (400 for body shape errors,
    404 for missing entities, 409 for stock conflicts, 403 for policy denials, etc.).

    Example::

        body = build_validation_error(BuildValidationErrorInput(
            code='bad_request',
            message='product_id, email, and shipping are required',
            required_fields={'product_id': 'uuid', 'email': 'string', 'shipping': 'object'},
            next_steps={'action': 'retry_with_complete_body'},
        ))
        return JSONResponse(body, status_code=400)
    """
    body: dict[str, Any] = {
        "error": {"code": input.code, "message": input.message},
    }
    if input.required_fields is not None:
        body["required_fields"] = input.required_fields
    if input.has_example_body:
        body["example_body"] = input.example_body
    if input.next_steps is not None:
        body["next_steps"] = input.next_steps
    body.update(input.extra)
    return body
