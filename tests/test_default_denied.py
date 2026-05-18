"""Tests for ``agentscore_commerce.identity.default_denied``."""

from agentscore_commerce.identity.default_denied import create_default_on_denied
from agentscore_commerce.identity.types import DenialReason


def test_wallet_signer_mismatch_403_with_body() -> None:
    on_denied = create_default_on_denied(
        merchant_name="Test Merchant",
        support_email="support@example.com",
    )
    reason = DenialReason(
        code="wallet_signer_mismatch",
        claimed_operator="op_abc",
        expected_signer="0xclaim",
        actual_signer="0xactual",
        linked_wallets=["0xclaim", "0xactual"],
    )
    result = on_denied(reason)
    assert result.status == 403
    assert "error" in result.body


def test_wallet_not_trusted_uses_walletnottrusted_message_override() -> None:
    on_denied = create_default_on_denied(
        merchant_name="Martin Estate",
        support_email="winery@martinestate.com",
        wallet_not_trusted_message="Purchase denied by compliance policy.",
    )
    reason = DenialReason(
        code="wallet_not_trusted",
        reasons=["sanctions_flagged"],
        verify_url="https://verify.example.com",
    )
    result = on_denied(reason)
    assert result.status == 403
    assert result.body["error"]["message"] == "Purchase denied by compliance policy."
    assert result.body["reasons"] == ["sanctions_flagged"]


def test_payment_required_uses_default_message() -> None:
    on_denied = create_default_on_denied(
        merchant_name="Test",
        support_email="s@e.com",
    )
    result = on_denied(DenialReason(code="payment_required"))
    assert result.status == 403
    assert result.body["error"]["code"] == "compliance_error"


def test_token_expired_returns_401() -> None:
    on_denied = create_default_on_denied(merchant_name="Test", support_email="s@e.com")
    result = on_denied(DenialReason(code="token_expired"))
    assert result.status == 401


def test_invalid_credential_returns_401() -> None:
    on_denied = create_default_on_denied(merchant_name="Test", support_email="s@e.com")
    result = on_denied(DenialReason(code="invalid_credential"))
    assert result.status == 401


def test_api_error_returns_503_with_cache_control_no_store() -> None:
    on_denied = create_default_on_denied(merchant_name="Test", support_email="s@e.com")
    result = on_denied(DenialReason(code="api_error"))
    assert result.status == 503
    assert result.headers == {"Cache-Control": "no-store"}


def test_unknown_code_returns_403() -> None:
    on_denied = create_default_on_denied(merchant_name="Test", support_email="s@e.com")
    result = on_denied(DenialReason(code="missing_identity"))
    assert result.status == 403
