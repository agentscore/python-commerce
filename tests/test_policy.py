"""Per-product / per-tier compliance policy helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from agentscore_commerce.identity import policy as policy_mod
from agentscore_commerce.identity.policy import (
    GateResult,
    build_gate_from_policy,
    run_gate_with_enforcement,
    shipping_country_allowed,
    shipping_state_allowed,
    validate_shipping_against_policy,
)

# ── shipping helpers ─────────────────────────────────────────────────────────


def test_country_null_policy_allows_anywhere() -> None:
    assert shipping_country_allowed("JP", None) is True


def test_country_null_allowlist_allows_anywhere() -> None:
    assert shipping_country_allowed("JP", {"enforcement": "hard"}) is True


def test_country_in_allowlist() -> None:
    assert shipping_country_allowed("US", {"allowed_shipping_countries": ["US"]}) is True


def test_country_not_in_allowlist() -> None:
    assert shipping_country_allowed("GB", {"allowed_shipping_countries": ["US"]}) is False


def test_country_allowlist_case_insensitive() -> None:
    assert shipping_country_allowed("us", {"allowed_shipping_countries": ["US"]}) is True


def test_state_null_policy_allows_anywhere() -> None:
    assert shipping_state_allowed("UT", "US", None) is True


def test_state_allowlist_only_enforced_for_us() -> None:
    # Non-US country bypasses the US-state allowlist entirely.
    p: dict[str, Any] = {"allowed_shipping_states": ["CA"]}
    assert shipping_state_allowed("LN", "GB", p) is True


def test_state_in_us_allowlist() -> None:
    p: dict[str, Any] = {"allowed_shipping_states": ["CA", "NY"]}
    assert shipping_state_allowed("CA", "US", p) is True


def test_state_blocked_when_us_and_not_in_list() -> None:
    p: dict[str, Any] = {"allowed_shipping_states": ["CA", "NY"]}
    assert shipping_state_allowed("UT", "US", p) is False


def test_state_allowlist_case_insensitive() -> None:
    p: dict[str, Any] = {"allowed_shipping_states": ["CA"]}
    assert shipping_state_allowed("ca", "US", p) is True


# ── build_gate_from_policy ──────────────────────────────────────────────────


def test_build_gate_returns_none_for_null_policy() -> None:
    assert build_gate_from_policy(None, api_key="ask_test") is None


def test_build_gate_returns_none_for_missing_enforcement() -> None:
    assert build_gate_from_policy({}, api_key="ask_test") is None


def test_build_gate_returns_none_when_enforcement_explicitly_none() -> None:
    assert build_gate_from_policy({"enforcement": None}, api_key="ask_test") is None


def test_build_gate_passes_policy_fields() -> None:
    gate = build_gate_from_policy(
        {
            "enforcement": "hard",
            "require_kyc": True,
            "require_sanctions_clear": True,
            "min_age": 21,
            "allowed_jurisdictions": ["US"],
        },
        api_key="ask_test",
    )
    assert gate is not None


def test_build_gate_threads_blocked_jurisdictions_to_the_gate() -> None:
    # Regression: build_gate_from_policy previously read require_kyc / sanctions / min_age /
    # allowed_jurisdictions but DROPPED blocked_jurisdictions, so a merchant blocklist never
    # reached AgentScoreCore's policy and the API was never told to enforce it.
    gate = build_gate_from_policy(
        {"enforcement": "hard", "blocked_jurisdictions": ["IR", "KP"]},
        api_key="ask_test",
    )
    assert gate is not None
    # blocked_jurisdictions lands in the core's compiled policy → forwarded to /v1/assess.
    assert gate._client._policy.get("blocked_jurisdictions") == ["IR", "KP"]


# ── run_gate_with_enforcement ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_gate_anonymous_when_gate_is_none() -> None:
    result = await run_gate_with_enforcement(object(), None, enforcement="hard")
    assert result == GateResult(status="anonymous")


@pytest.mark.asyncio
async def test_run_gate_anonymous_when_enforcement_is_none() -> None:
    fake_gate = AsyncMock()
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement=None)
    assert result.status == "anonymous"
    fake_gate.assert_not_called()


@pytest.mark.asyncio
async def test_run_gate_verified_on_pass() -> None:
    fake_gate = AsyncMock(return_value=None)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="hard")
    assert result.status == "verified"


@pytest.mark.asyncio
async def test_run_gate_hard_propagates_denial() -> None:
    err = HTTPException(status_code=403, detail={"error": {"code": "missing_identity"}})
    fake_gate = AsyncMock(side_effect=err)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="hard")
    assert result.status == "denied"
    assert result.denial_status == 403
    assert result.denial_body == {"error": {"code": "missing_identity"}}


@pytest.mark.asyncio
async def test_run_gate_soft_swallows_denial() -> None:
    err = HTTPException(status_code=403, detail={"error": {"code": "missing_identity"}})
    fake_gate = AsyncMock(side_effect=err)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="soft")
    assert result.status == "unverified"
    assert result.denial_status == 403


@pytest.mark.asyncio
async def test_run_gate_soft_handles_string_detail() -> None:
    err = HTTPException(status_code=503, detail="api error")
    fake_gate = AsyncMock(side_effect=err)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="soft")
    assert result.status == "unverified"
    assert result.denial_body == {"error": {"message": "api error"}}


@pytest.mark.asyncio
async def test_run_gate_hard_converts_gate_denial_error() -> None:
    # Post-flatten the gate raises a FLAT _GateDenialError (not HTTPException);
    # run_gate_with_enforcement must convert it to a denied GateResult.
    from agentscore_commerce.identity.fastapi import _GateDenialError

    err = _GateDenialError({"error": {"code": "missing_identity"}}, 403)
    fake_gate = AsyncMock(side_effect=err)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="hard")
    assert result.status == "denied"
    assert result.denial_status == 403
    assert result.denial_body == {"error": {"code": "missing_identity"}}


@pytest.mark.asyncio
async def test_run_gate_soft_swallows_gate_denial_error() -> None:
    # soft mode SWALLOWS a non-sanctions _GateDenialError (KYC/age/jurisdiction), stamping
    # status="unverified" so the order completes with a degraded identity_status. (Sanctions
    # are the sole exception — see test_run_gate_soft_does_not_swallow_sanctions_*.)
    # run_gate_with_enforcement previously caught only HTTPException, so once the gate
    # started raising the flat _GateDenialError, soft mode let the denial propagate.
    from agentscore_commerce.identity.fastapi import _GateDenialError

    err = _GateDenialError({"error": {"code": "wallet_not_trusted"}, "reasons": ["kyc_required"]}, 403)
    fake_gate = AsyncMock(side_effect=err)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="soft")
    assert result.status == "unverified"
    assert result.denial_status == 403
    assert result.denial_body == {"error": {"code": "wallet_not_trusted"}, "reasons": ["kyc_required"]}


@pytest.mark.asyncio
async def test_run_gate_soft_does_not_swallow_sanctions_gate_denial_error() -> None:
    # CRITICAL: soft enforcement must NEVER swallow an OFAC SDN sanctions deny. A
    # wallet_not_trusted deny whose reasons carry `sanctions_flagged` stays terminal
    # (status="denied") even under soft, so a sanctioned wallet is never settled.
    from agentscore_commerce.identity.fastapi import _GateDenialError

    body = {"error": {"code": "wallet_not_trusted"}, "reasons": ["sanctions_flagged"]}
    err = _GateDenialError(body, 403)
    fake_gate = AsyncMock(side_effect=err)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="soft")
    assert result.status == "denied"
    assert result.denial_status == 403
    assert result.denial_body == body


@pytest.mark.asyncio
async def test_run_gate_soft_does_not_swallow_sanctions_unavailable() -> None:
    # The fail-closed unavailable-screen variant (`sanctions_check_unavailable`) is also a
    # strict-liability deny — soft must not downgrade it to settled.
    from agentscore_commerce.identity.fastapi import _GateDenialError

    body = {"error": {"code": "wallet_not_trusted"}, "reasons": ["sanctions_check_unavailable"]}
    err = _GateDenialError(body, 403)
    fake_gate = AsyncMock(side_effect=err)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="soft")
    assert result.status == "denied"
    assert result.denial_body == body


@pytest.mark.asyncio
async def test_run_gate_soft_does_not_swallow_signer_sanctions_error_code() -> None:
    # A signer-sanctions SDN deny that surfaces as a top-level error.code (not in reasons)
    # is still recognised as a sanctions deny and stays terminal under soft.
    from agentscore_commerce.identity.fastapi import _GateDenialError

    body = {"error": {"code": "sanctions_flagged", "message": "signer on SDN list"}}
    err = _GateDenialError(body, 403)
    fake_gate = AsyncMock(side_effect=err)
    result = await run_gate_with_enforcement(object(), fake_gate, enforcement="soft")
    assert result.status == "denied"


def test_module_exports_public_surface() -> None:
    for name in (
        "PolicyBlock",
        "GateResult",
        "EnforcementMode",
        "IdentityStatus",
        "build_gate_from_policy",
        "run_gate_with_enforcement",
        "shipping_country_allowed",
        "shipping_state_allowed",
    ):
        assert hasattr(policy_mod, name), name


# ── validate_shipping_against_policy ─────────────────────────────────────────


def test_validate_shipping_no_op_on_null_policy() -> None:
    # No raise — ship anywhere when policy is None.
    validate_shipping_against_policy(country="AQ", state="", policy=None)


def test_validate_shipping_no_op_when_allowlist_empty() -> None:
    # Empty allowlist == no restriction; policy with other fields is fine.
    validate_shipping_against_policy(country="JP", state="", policy={"require_kyc": True})


def test_validate_shipping_raises_on_disallowed_country() -> None:
    from agentscore_commerce.errors import CheckoutValidationError

    with pytest.raises(CheckoutValidationError) as exc:
        validate_shipping_against_policy(country="JP", state="", policy={"allowed_shipping_countries": ["US"]})
    assert exc.value.code == "unsupported_jurisdiction"
    assert "JP" in exc.value.message
    assert exc.value.action == "change_shipping_state"


def test_validate_shipping_raises_on_disallowed_state() -> None:
    from agentscore_commerce.errors import CheckoutValidationError

    policy = {"allowed_shipping_countries": ["US"], "allowed_shipping_states": ["CA", "NY"]}
    with pytest.raises(CheckoutValidationError) as exc:
        validate_shipping_against_policy(country="US", state="UT", policy=policy)
    assert exc.value.code == "unsupported_jurisdiction"
    assert "UT" in exc.value.message


def test_validate_shipping_product_name_appears_in_message() -> None:
    from agentscore_commerce.errors import CheckoutValidationError

    with pytest.raises(CheckoutValidationError) as exc:
        validate_shipping_against_policy(
            country="JP",
            state="",
            policy={"allowed_shipping_countries": ["US"]},
            product_name="Reserve Cabernet",
        )
    assert "Reserve Cabernet" in exc.value.message


def test_validate_shipping_custom_messages_override_defaults() -> None:
    from agentscore_commerce.errors import CheckoutValidationError

    with pytest.raises(CheckoutValidationError) as exc_country:
        validate_shipping_against_policy(
            country="JP",
            state="",
            policy={"allowed_shipping_countries": ["US"]},
            country_message="Sorry, regulations.",
        )
    assert exc_country.value.message == "Sorry, regulations."

    policy = {"allowed_shipping_countries": ["US"], "allowed_shipping_states": ["CA"]}
    with pytest.raises(CheckoutValidationError) as exc_state:
        validate_shipping_against_policy(
            country="US",
            state="UT",
            policy=policy,
            state_message="Fulfillment partner doesn't cover that area.",
        )
    assert exc_state.value.message == "Fulfillment partner doesn't cover that area."


def test_validate_shipping_custom_code_and_action() -> None:
    from agentscore_commerce.errors import CheckoutValidationError

    with pytest.raises(CheckoutValidationError) as exc:
        validate_shipping_against_policy(
            country="JP",
            state="",
            policy={"allowed_shipping_countries": ["US"]},
            error_code="ships_us_only",
            error_action="contact_support",
        )
    assert exc.value.code == "ships_us_only"
    assert exc.value.action == "contact_support"
