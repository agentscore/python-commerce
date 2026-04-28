"""Tests for the denial helpers."""

import pytest

from agentscore_commerce.identity._denial import (
    FIXABLE_DENIAL_REASONS,
    build_contact_support_next_steps,
    build_signer_mismatch_body,
    denial_reason_status,
    is_fixable_denial,
    verification_agent_instructions,
)
from agentscore_commerce.identity.types import DenialReason, VerifyWalletSignerResult


class TestDenialReasonStatus:
    def test_returns_401_for_token_expired(self):
        assert denial_reason_status(DenialReason(code="token_expired")) == 401

    def test_returns_401_for_invalid_credential(self):
        assert denial_reason_status(DenialReason(code="invalid_credential")) == 401

    def test_returns_503_for_api_error(self):
        assert denial_reason_status(DenialReason(code="api_error")) == 503

    @pytest.mark.parametrize(
        "code",
        [
            "missing_identity",
            "identity_verification_required",
            "wallet_not_trusted",
            "wallet_signer_mismatch",
            "wallet_auth_requires_wallet_signing",
            "payment_required",
        ],
    )
    def test_returns_403_for_everything_else(self, code):
        assert denial_reason_status(DenialReason(code=code)) == 403


class TestIsFixableDenial:
    def test_known_fixable_reasons_in_set(self):
        for r in ("kyc_required", "kyc_pending", "kyc_failed", "jurisdiction_restricted"):
            assert r in FIXABLE_DENIAL_REASONS

    def test_empty_or_none_treated_as_fixable(self):
        assert is_fixable_denial(None)
        assert is_fixable_denial([])

    def test_all_fixable_returns_true(self):
        assert is_fixable_denial(["kyc_required", "jurisdiction_restricted"])

    def test_any_permanent_returns_false(self):
        assert not is_fixable_denial(["sanctions_not_clear"])
        assert not is_fixable_denial(["age_not_verified"])
        assert not is_fixable_denial(["kyc_required", "sanctions_not_clear"])


class TestBuildSignerMismatchBody:
    def test_returns_none_for_pass_or_api_error(self):
        assert build_signer_mismatch_body(VerifyWalletSignerResult(kind="pass")) is None
        assert build_signer_mismatch_body(VerifyWalletSignerResult(kind="api_error")) is None

    def test_wallet_signer_mismatch_with_linked_wallets(self):
        result = VerifyWalletSignerResult(
            kind="wallet_signer_mismatch",
            claimed_operator="op_victim",
            actual_signer_operator="op_attacker",
            expected_signer="0xVictim",
            actual_signer="0xAttacker",
            linked_wallets=["0xLinked1", "0xLinked2"],
        )
        body = build_signer_mismatch_body(result)
        assert body["error"]["code"] == "wallet_signer_mismatch"
        assert body["claimed_operator"] == "op_victim"
        assert body["actual_signer_operator"] == "op_attacker"
        assert body["linked_wallets"] == ["0xLinked1", "0xLinked2"]
        assert "0xLinked1" in body["next_steps"]["user_message"]

    def test_no_linked_wallets_uses_fallback_message(self):
        result = VerifyWalletSignerResult(
            kind="wallet_signer_mismatch",
            claimed_operator="op_v",
            actual_signer_operator=None,
            expected_signer="0xClaim",
            actual_signer="0xSigner",
            linked_wallets=[],
        )
        body = build_signer_mismatch_body(result)
        assert "X-Operator-Token" in body["next_steps"]["user_message"]

    def test_wallet_auth_requires_wallet_signing(self):
        body = build_signer_mismatch_body(VerifyWalletSignerResult(kind="wallet_auth_requires_wallet_signing"))
        assert body["error"]["code"] == "wallet_auth_requires_wallet_signing"
        assert body["next_steps"]["action"] == "switch_to_operator_token"

    def test_custom_user_message_and_learn_more_url(self):
        body = build_signer_mismatch_body(
            VerifyWalletSignerResult(kind="wallet_auth_requires_wallet_signing"),
            user_message="Custom",
            learn_more_url="https://my.docs",
        )
        assert body["next_steps"]["user_message"] == "Custom"
        assert body["next_steps"]["learn_more_url"] == "https://my.docs"


class TestBuildContactSupportNextSteps:
    def test_default_message(self):
        ns = build_contact_support_next_steps("hello@merchant.com")
        assert ns["action"] == "contact_support"
        assert ns["support_email"] == "hello@merchant.com"
        assert "hello@merchant.com" in ns["user_message"]

    def test_custom_message(self):
        ns = build_contact_support_next_steps("hello@merchant.com", "Try our portal first.")
        assert ns["user_message"] == "Try our portal first."


class TestVerificationAgentInstructions:
    def test_default_block(self):
        inst = verification_agent_instructions()
        assert inst["action"] == "poll_for_credential"
        assert inst["poll_interval_seconds"] == 5
        assert inst["poll_secret_header"] == "X-Poll-Secret"
        assert inst["retry_token_header"] == "X-Operator-Token"
        assert inst["timeout_seconds"] == 3600
        assert len(inst["steps"]) >= 4
        assert "verify_url" in inst["steps"][0]

    def test_poll_cadence_and_timeout_overrides(self):
        inst = verification_agent_instructions(poll_interval_seconds=10, timeout_seconds=1800)
        assert inst["poll_interval_seconds"] == 10
        assert inst["timeout_seconds"] == 1800
        assert "every 10 seconds" in inst["steps"][1]

    def test_extra_steps_and_order_ttl_and_extras(self):
        inst = verification_agent_instructions(
            extra_steps=["Resume by including order_id in the retry body."],
            order_ttl="Pending orders expire after 1 hour.",
            extra={"vendor_field": "value"},
        )
        assert inst["steps"][-1] == "Resume by including order_id in the retry body."
        assert "1 hour" in inst["order_ttl"]
        assert inst["vendor_field"] == "value"

    def test_retry_step_replaces_canonical_step(self):
        custom = "Retry POST /purchase with X-Operator-Token AND include order_id from this response."
        inst = verification_agent_instructions(retry_step=custom)
        assert inst["steps"][4] == custom
        canonical = "Retry the original merchant request with header X-Operator-Token set to the operator_token value."
        assert canonical not in inst["steps"]

    def test_retry_step_and_extra_steps_compose(self):
        inst = verification_agent_instructions(
            retry_step="Custom retry.",
            extra_steps=["Then do X.", "Then do Y."],
        )
        assert len(inst["steps"]) == 7
        assert inst["steps"][4] == "Custom retry."
        assert inst["steps"][5] == "Then do X."
        assert inst["steps"][6] == "Then do Y."
