"""Tests for ``agentscore_commerce.payment.mppx_failures``."""

from agentscore_commerce.payment.mppx_failures import classify_mppx_failure


def test_returns_none_when_reason_is_falsy() -> None:
    assert classify_mppx_failure(None) is None
    assert classify_mppx_failure("") is None


def test_returns_none_for_unrecognized_reasons() -> None:
    assert classify_mppx_failure("insufficient funds") is None
    assert classify_mppx_failure("Transaction reverted: ERC20") is None


def test_classifies_tempo_keychain_rejection_by_literal_pattern() -> None:
    out = classify_mppx_failure(
        "RPC Request failed. (keychain validation failed: AccountKeychainError(KeyNotFound(KeyNotFound)))"
    )
    assert out is not None
    assert out.code == "tempo_key_not_registered"
    assert out.status == 401
    assert out.next_steps["action"] == "register_tempo_key"
    assert out.extra["upstream_error"] == "KeyNotFound"
    assert out.extra["chain"] == "tempo"


def test_matches_keynotfound_case_insensitively() -> None:
    out = classify_mppx_failure("Some shorter message containing KeyNotFound somewhere")
    assert out is not None
    assert out.code == "tempo_key_not_registered"


def test_user_message_names_both_recovery_paths() -> None:
    out = classify_mppx_failure("keychain validation failed: KeyNotFound")
    assert out is not None
    msg = out.next_steps["user_message"]
    assert "tempo wallet login" in msg
    assert "Base" in msg or "Solana" in msg


def test_solana_confirmation_timeout_is_pending_not_regenerate() -> None:
    out = classify_mppx_failure("Transaction confirmation timeout")
    assert out is not None
    assert out.code == "payment_pending_confirmation"
    # 504, not 402: a 402 would trigger x402 clients to auto-repay, the exact
    # double-charge this guards.
    assert out.status == 504
    assert out.status != 402
    assert out.next_steps["action"] != "regenerate_payment_credential"
    assert out.next_steps["action"] == "check_settlement_before_retry"
    assert out.extra["chain"] == "solana"
    assert out.extra["broadcast"] is True


def test_solana_confirmation_timeout_status_recovery_variant() -> None:
    out = classify_mppx_failure("Transaction confirmation timeout (status recovery failed: RPC error)")
    assert out is not None
    assert out.code == "payment_pending_confirmation"


def test_solana_confirmation_timeout_warns_against_double_pay() -> None:
    out = classify_mppx_failure("Transaction confirmation timeout")
    assert out is not None
    msg = out.next_steps["user_message"].lower()
    assert "confirmation timed out" in msg
    assert "check your wallet balance" in msg
    assert "not pay again" in msg or "only resubmit" in msg
