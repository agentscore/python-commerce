"""Tests for build_idempotency_key."""

import logging

from agentscore_commerce.payment import build_idempotency_key


def test_returns_payment_intent_id_verbatim():
    assert build_idempotency_key(payment_intent_id="pi_abc123") == "pi_abc123"


def test_synthesizes_from_order_id_plus_amount():
    assert build_idempotency_key(order_id="ord_xyz", amount_cents=25000) == "pi-ord_xyz-25000"


def test_uses_order_id_only_when_amount_missing():
    assert build_idempotency_key(order_id="ord_xyz") == "pi-ord_xyz"


def test_returns_none_when_no_inputs():
    assert build_idempotency_key() is None


def test_payment_intent_id_wins_over_order_id():
    result = build_idempotency_key(
        payment_intent_id="pi_abc",
        order_id="ord_xyz",
        amount_cents=100,
    )
    assert result == "pi_abc"


def test_prefix_applied_to_payment_intent_path():
    assert build_idempotency_key(payment_intent_id="pi_abc", prefix="refund") == "refund-pi_abc"


def test_prefix_applied_to_order_id_fallback_path():
    assert build_idempotency_key(order_id="ord_x", prefix="void") == "void-pi-ord_x"


def test_does_not_warn_for_keys_under_200_chars():
    key = "a" * 200
    result = build_idempotency_key(payment_intent_id=key)
    # No warning expected when at the boundary; just verify the key passes through unchanged.
    assert result == key


def test_warns_when_key_exceeds_200_chars(caplog):
    key = "a" * 201
    with caplog.at_level(logging.WARNING, logger="agentscore_commerce.payment.idempotency"):
        result = build_idempotency_key(payment_intent_id=key)
    # Original key returned unchanged — server is the source of truth for truncation.
    assert result == key
    assert any("idempotency key longer than 200 chars" in rec.message for rec in caplog.records)
