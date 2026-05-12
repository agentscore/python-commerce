"""Idempotency-key composition.

Stable per-payment keys that retries of the same logical payment can reuse, so AgentScore's
``/v1/credentials/wallets`` capture endpoint dedupes correctly and the operator's
``transaction_count`` doesn't inflate.

Convention:
    1. Prefer the upstream payment-rail's stable identifier (Stripe PaymentIntent id, x402
       tx hash) when one exists — those are already idempotent on their side.
    2. Fall back to a synthesized ``pi-{order_id}-{amount_cents}`` key when no upstream id
       is available.
    3. Server caps idempotency keys at 200 chars; this helper warns when that boundary is
       crossed so a future caller doesn't silently get truncation collisions.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_SERVER_IDEMPOTENCY_KEY_MAX = 200


def build_idempotency_key(
    payment_intent_id: str | None = None,
    order_id: str | None = None,
    amount_cents: int | None = None,
    prefix: str | None = None,
) -> str | None:
    """Compose a stable idempotency key for AgentScore wallet capture and other retry-safe POSTs.

    Returns ``None`` when no inputs are present (caller should treat as "no idempotency
    key — first attempt only", same shape as omitting the field entirely).

    Examples::

        build_idempotency_key(payment_intent_id="pi_abc")           # → "pi_abc"
        build_idempotency_key(order_id="ord_x", amount_cents=25000) # → "pi-ord_x-25000"
        build_idempotency_key(order_id="ord_x")                     # → "pi-ord_x"
        build_idempotency_key(payment_intent_id="pi_abc", prefix="refund")  # → "refund-pi_abc"
        build_idempotency_key()                                     # → None
    """
    prefix_str = f"{prefix}-" if prefix else ""

    if payment_intent_id:
        return _clamp_key(f"{prefix_str}{payment_intent_id}")

    if order_id:
        amount_suffix = f"-{amount_cents}" if amount_cents is not None else ""
        return _clamp_key(f"{prefix_str}pi-{order_id}{amount_suffix}")

    return None


def _clamp_key(key: str) -> str:
    if len(key) <= _SERVER_IDEMPOTENCY_KEY_MAX:
        return key
    # Server truncates anyway; surfacing the warning here gives callers a chance to design
    # shorter inputs. We still return the original key (server-side truncation is the
    # source of truth) — clamping client-side would change semantics for any caller already
    # depending on the full string for their own dedup.
    _log.warning(
        "[agentscore-commerce] idempotency key longer than %d chars — server will truncate, "
        "may cause silent collisions if multiple keys share the first %d chars.",
        _SERVER_IDEMPOTENCY_KEY_MAX,
        _SERVER_IDEMPOTENCY_KEY_MAX,
    )
    return key


__all__ = ["build_idempotency_key"]
