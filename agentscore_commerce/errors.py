"""Cross-module typed errors.

Lives in its own module so payment / stripe_multichain helpers can throw
``CheckoutValidationError`` without importing ``agentscore_commerce.checkout``
(which itself imports from ``agentscore_commerce.payment`` and would deadlock
at startup).

Re-exported from :mod:`agentscore_commerce.checkout` to preserve the public
import path that consumers use today.
"""

from __future__ import annotations

from typing import Any


class CheckoutValidationError(Exception):
    """Raised to short-circuit a Checkout flow with a 4xx/5xx envelope.

    Caught at request-flow boundaries (e.g. ``Checkout.pre_validate``,
    recipient minting, settlement dispatch); the framework emits the canonical
    ``{error, next_steps}`` body via :func:`build_validation_error` so
    merchants don't construct framework Response objects themselves.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        action: str = "fix_request",
        status: int = 400,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action
        self.status = status
        self.extra = extra
