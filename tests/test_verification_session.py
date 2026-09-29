"""Verification-session request helpers. Ports node-commerce ``tests/verification_session.test.ts``."""

from __future__ import annotations

from agentscore_commerce import (
    VERIFICATION_SESSION_HEADER,
    build_identity_bootstrap,
    has_identity_header,
    requests_verification_session,
    should_run_conditional_gate,
)


def test_recognizes_the_header_in_any_case_trimmed() -> None:
    assert requests_verification_session({"x-verification-session": "create"})
    assert requests_verification_session({VERIFICATION_SESSION_HEADER: " CREATE "})


def test_rejects_other_values_and_a_missing_header() -> None:
    assert not requests_verification_session({"x-verification-session": "yes"})
    assert not requests_verification_session({})


def test_no_session_request_when_identity_or_payment_is_present() -> None:
    base = {"x-verification-session": "create"}
    assert not requests_verification_session({**base, "x-operator-token": "opc_x"})
    assert not requests_verification_session({**base, "x-wallet-address": "0xabc"})
    assert not requests_verification_session({**base, "agent-identity": "eyJ.e30.sig"})
    assert not requests_verification_session({**base, "authorization": "Payment abc"})
    assert not requests_verification_session({**base, "x-payment": "abc"})


def test_empty_agent_identity_is_no_identity() -> None:
    assert not has_identity_header({"agent-identity": " , "})
    assert has_identity_header({"agent-identity": "eyJ.e30.sig"})


def test_conditional_gate_runs_on_payment_or_session_request_only() -> None:
    assert should_run_conditional_gate({"authorization": "Payment abc"})
    assert should_run_conditional_gate({"payment-signature": "abc"})
    assert should_run_conditional_gate({"x-verification-session": "create"})
    assert not should_run_conditional_gate({})
    assert not should_run_conditional_gate({"x-operator-token": "opc_x"})


def test_builds_the_identity_bootstrap_block() -> None:
    block = build_identity_bootstrap()
    assert block["header"] == VERIFICATION_SESSION_HEADER
    assert block["value"] == "create"
    assert f"{VERIFICATION_SESSION_HEADER}: create" in block["instructions"]
    assert "verify_url" in block["instructions"]
