"""Tests for ``default_read_only_on_denied`` — read-only resource gate denial."""

from agentscore_commerce.identity.default_denied import default_read_only_on_denied
from agentscore_commerce.identity.types import DenialReason


def test_returns_401_with_missing_identity_message() -> None:
    r = default_read_only_on_denied(DenialReason(code="missing_identity"))
    assert r.status == 401
    assert r.body["error"] == {
        "code": "unauthorized",
        "message": "X-Wallet-Address or X-Operator-Token header required",
    }
    assert r.headers == {"Cache-Control": "no-store"}


def test_returns_401_with_invalid_identity_message_on_other_codes() -> None:
    r = default_read_only_on_denied(DenialReason(code="token_expired"))
    assert r.status == 401
    assert r.body["error"] == {"code": "unauthorized", "message": "Invalid identity"}
    assert r.headers == {"Cache-Control": "no-store"}


def test_spreads_denial_reason_to_body_so_agent_instructions_ride_through() -> None:
    r = default_read_only_on_denied(
        DenialReason(code="wallet_not_trusted", reasons=["sanctions_flagged"]),
    )
    assert r.status == 401
    # Body carries through additional denial-derived fields beyond just `error`.
    assert len(r.body) > 1


def test_collapses_api_error_to_401_no_5xx_leakage() -> None:
    r = default_read_only_on_denied(DenialReason(code="api_error"))
    assert r.status == 401
