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
