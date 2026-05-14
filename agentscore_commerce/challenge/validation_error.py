"""Structured 4xx validation-error body builder.

Pairs cleanly with the existing 402 / 403 builders. Every commerce merchant
returning helpful ``bad_request`` / ``not_found`` / ``out_of_stock`` errors
converges on the same shape: ``{error: {code, message}, ...optional_hints,
next_steps?}``. This builder doesn't choose the HTTP status — vendors wrap the
returned body in their framework's response (``JSONResponse(body, 400)`` in
FastAPI, etc.). Status stays the merchant's call because the same shape works
for 400/404/409/422.
"""

from typing import Any

_NO_EXAMPLE: Any = object()


def build_validation_error(
    *,
    code: str,
    message: str,
    required_fields: dict[str, str] | None = None,
    # Sentinel: distinguish "no example provided" from "explicit null in body".
    # Pass any value (including None) to emit it; omit to suppress the field entirely.
    example_body: Any = _NO_EXAMPLE,
    next_steps: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose a 4xx body that vendors return via their framework's response helper.

    Combine with the merchant's chosen HTTP status (400 for body shape errors,
    404 for missing entities, 409 for stock conflicts, 403 for policy denials, etc.).

    Example::

        body = build_validation_error(
            code='bad_request',
            message='product_id, email, and shipping are required',
            required_fields={'product_id': 'uuid', 'email': 'string', 'shipping': 'object'},
            next_steps={'action': 'retry_with_complete_body'},
        )
        return JSONResponse(body, status_code=400)
    """
    body: dict[str, Any] = {
        "error": {"code": code, "message": message},
    }
    if required_fields is not None:
        body["required_fields"] = required_fields
    if example_body is not _NO_EXAMPLE:
        body["example_body"] = example_body
    if next_steps is not None:
        body["next_steps"] = next_steps
    if extra:
        body.update(extra)
    return body
