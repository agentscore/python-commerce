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
