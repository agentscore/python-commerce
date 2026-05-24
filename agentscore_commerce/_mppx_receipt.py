"""Internal helpers for extracting the ``Payment-Receipt`` header.

Shared by ``Checkout.handle_mppx`` and ``compute_first_checkout``'s MPP
settle path so the rail-label / signer derivation stays one source of truth.

Not part of the public API.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def extract_mppx_receipt_header_from_raw(raw: Any) -> str | None:
    """Pull the ``Payment-Receipt`` header value from an mppx compose result.

    Covers three shapes hand-rolled hooks commonly return:

    * ``raw.receipt_header`` — pympp's current direct-attribute shape.
    * ``raw.to_payment_receipt()`` — pympp's older Receipt return-method shape
      (also reached when ``raw`` is a ``(credential, receipt)`` tuple OR a
      dict/object carrying ``.receipt``).
    * ``raw.with_receipt(response) -> Response`` — a shape that wraps an
      outgoing Response and attaches the header.

    Returns ``None`` when none match or the underlying call raises.
    """
    if raw is None:
        return None
    # Shape 1: direct attribute.
    header = getattr(raw, "receipt_header", None)
    if isinstance(header, str) and header:
        return header
    # Shape 2: pympp's `to_payment_receipt()` callable on raw itself or a
    # carried receipt. Build candidate list; first match wins.
    candidates: list[Any] = [raw]
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        candidates.append(raw[1])
    if isinstance(raw, dict) and "receipt" in raw:
        candidates.append(raw["receipt"])
    inner_receipt = getattr(raw, "receipt", None)
    if inner_receipt is not None:
        candidates.append(inner_receipt)
    for candidate in candidates:
        to_header = getattr(candidate, "to_payment_receipt", None)
        if not callable(to_header):
            continue
        try:
            value = to_header()
        except Exception as exc:
            log.debug("[_mppx_receipt] to_payment_receipt() raised: %s", exc)
            continue
        if isinstance(value, str) and value:
            return value
    # Shape 3: node-style with_receipt(response) decorator.
    with_receipt = getattr(raw, "with_receipt", None)
    if callable(with_receipt):
        try:
            wrapped = with_receipt(None)
            headers = getattr(wrapped, "headers", None)
            if headers is not None and hasattr(headers, "get"):
                val = headers.get("Payment-Receipt")
                if isinstance(val, str) and val:
                    return val
        except Exception:
            return None
    return None


def extract_mppx_receipt_method(header: str) -> str | None:
    """Deserialize the receipt header via mppx and return the ``method`` field.

    The returned method is ``'tempo'`` / ``'solana'`` / ``'stripe'``, or the
    legacy ``'<scheme>/charge'`` form. Returns ``None`` when the header is
    malformed or mppx isn't importable. Uses pympp's
    ``Receipt.from_payment_receipt``.
    """
    try:
        from mpp import Receipt  # type: ignore[import-untyped]
    except Exception:
        return None
    try:
        receipt = Receipt.from_payment_receipt(header)
    except Exception:
        return None
    method = getattr(receipt, "method", None)
    return method if isinstance(method, str) else None


def derive_mppx_receipt_method(raw: Any) -> str | None:
    """Resolve the receipt method from a compose-success raw result in one call.

    Tries the direct ``raw.receipt.method`` path first, then falls back to the
    receipt-header path. Returns ``None`` when neither yields a method.
    """
    receipt = getattr(raw, "receipt", None)
    direct = getattr(receipt, "method", None) if receipt is not None else None
    if isinstance(direct, str) and direct:
        return direct
    header = extract_mppx_receipt_header_from_raw(raw)
    if not header:
        return None
    return extract_mppx_receipt_method(header)
