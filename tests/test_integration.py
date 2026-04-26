import os

import httpx
import pytest

API_KEY = os.environ.get("AGENTSCORE_API_KEY")
BASE_URL = os.environ.get("AGENTSCORE_BASE_URL", "http://api.dev.agentscore.internal")
TEST_ADDRESS = "0x339559a2d1cd15059365fc7bd36b3047bba480e0"

pytestmark = pytest.mark.skipif(not API_KEY, reason="AGENTSCORE_API_KEY not set")


def _assess(body=None):
    return httpx.post(
        f"{BASE_URL}/v1/assess",
        headers={"X-API-Key": API_KEY},
        json={"address": TEST_ADDRESS, **(body or {})},
    ).json()


def test_assess_flat_decision_shape():
    data = _assess()

    assert "decision" in data
    assert isinstance(data["decision_reasons"], list)
    assert data["identity_method"] == "wallet"
    assert "operator_verification" in data
    assert isinstance(data.get("on_the_fly"), bool)


def test_assess_without_policy_allows():
    data = _assess()

    assert data["decision"] == "allow"
    assert "no_policy_applied" in data["decision_reasons"]


def test_assess_policy_deny():
    data = _assess({"policy": {"require_kyc": True}})

    assert data["decision"] == "deny"
    assert "kyc_required" in data["decision_reasons"]
    assert "verify_url" in data
    assert "/verify" in data["verify_url"]
    assert "policy_result" in data
    assert data["policy_result"]["all_passed"] is False
