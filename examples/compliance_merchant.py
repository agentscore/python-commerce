"""Example: regulated-goods merchant showcasing the gate + denial helpers.

Scenario: you sell something that needs identity gating; wine (age 21+, US-only),
cannabis (age 21+, state allowlist), high-value items (KYC + sanctions). The
agent needs to know how to recover from each kind of denial.

What this example demonstrates:

* `Checkout(gate=CheckoutGateConfig(...))` runs the SDK gate on the settle leg.
* Custom `on_denied` callback composes the canonical denial helpers:
    - `verification_agent_instructions` for the canonical poll-and-retry block
    - `is_fixable_denial` for fixable (KYC re-do) vs unfixable
      (sanctions / age / jurisdiction_restricted) compliance fails. Gate normally
      re-routes fixable reasons to identity_verification_required upstream;
      the fixable branch is a defensive fallback if /v1/sessions mint blipped.
    - `build_contact_support_next_steps` for the unfixable branch
    - `denial_reason_to_body` + `denial_reason_status` for the standard
      fall-through (token_expired, invalid_credential, api_error get the
      right status + body for free).
* Signer-match enforcement (wallet_signer_mismatch / wallet_auth_requires_wallet_signing)
  is now automatic inside the gate; consumers don't call
  `build_signer_mismatch_body` from inside the handler anymore.

Pattern: vendors only write the BUSINESS-SPECIFIC denial branches. Everything
else is a one-line helper call.

Peer deps:
    pip install 'agentscore-commerce[fastapi]'

Env vars:
    AGENTSCORE_API_KEY — your AgentScore API key

Run: uvicorn examples.compliance_merchant:app --port 3000
"""

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce import (
    Checkout,
    CheckoutGateConfig,
    DenialReason,
    PricingResult,
    SettleOutcome,
    TempoRailSpec,
    build_contact_support_next_steps,
    build_verification_required_body,
    denial_reason_status,
    denial_reason_to_body,
    is_fixable_denial,
    verification_agent_instructions,
)
from agentscore_commerce.middleware.fastapi import RateLimitMiddleware

SUPPORT_EMAIL = "support@example.com"

# Vendor-specific extension of the canonical agent_instructions block. The commerce
# default covers steps 1-4 (present verify_url, poll, user verifies, extract token)
# plus a generic "retry the original merchant request" at step 5. `retry_step`
# REPLACES that generic step 5; `extra_steps` adds the 402-payment step that
# comes AFTER retry.
VERIFICATION_INSTRUCTIONS = verification_agent_instructions(
    retry_step=(
        "Retry the request with header X-Operator-Token set to the operator_token value AND "
        "include the order_id from this 403 in the body to resume the pending order."
    ),
    extra_steps=[
        "The retry returns 402 Payment Required with a payment challenge. Pay via tempo request or agentscore-pay pay.",
    ],
    order_ttl="Pending orders expire after 1 hour. If the order expires, start a new request.",
)


async def _on_denied(_ctx: Any, reason: DenialReason) -> dict[str, Any] | None:
    """Reshape the canonical denial body for vendor-specific copy.

    Return `{"status": <int>, "body": <dict>, "headers": <dict>?}` to override
    the gate's default envelope, or `None` to keep the canonical body.
    """
    # missing_identity → bare 403; agent must bootstrap.
    if reason.code == "missing_identity":
        body = denial_reason_to_body(reason)
        body["error"] = {"code": "identity_required", "message": "Identity verification is required for this purchase."}
        return {"status": 403, "body": body}

    # identity_verification_required → gate auto-minted a session. Use the
    # canonical body builder + overlay vendor-specific agent_instructions.
    if reason.code == "identity_verification_required":
        return {
            "status": 403,
            "body": build_verification_required_body(
                reason,
                message="Identity verification is required for this purchase.",
                agent_instructions=VERIFICATION_INSTRUCTIONS,
            ),
        }

    # wallet_not_trusted = UNFIXABLE compliance fail (sanctions / age /
    # jurisdiction_restricted). The gate auto-routes fixable reasons upstream;
    # the is_fixable_denial branch here is a defensive fallback.
    if reason.code == "wallet_not_trusted":
        reasons = reason.reasons or []
        if is_fixable_denial(reasons):
            return {
                "status": 403,
                "body": {
                    "error": {"code": "compliance_recoverable", "message": "Re-verify identity and retry."},
                    "reasons": reasons,
                    "verify_url": reason.verify_url,
                },
            }
        return {
            "status": 403,
            "body": {
                "error": {
                    "code": "compliance_denied",
                    "message": "Purchase denied by compliance policy. Not resolvable through re-verification.",
                },
                "reasons": reasons,
                "next_steps": build_contact_support_next_steps(SUPPORT_EMAIL),
            },
        }

    # token_expired (401), invalid_credential (401), api_error (503) →
    # standard body+status from commerce.
    return {"status": denial_reason_status(reason), "body": denial_reason_to_body(reason)}


async def _compute_pricing(_ctx: Any) -> PricingResult:
    return PricingResult(amount_usd=250.0)  # vendor pricing logic goes here.


async def _on_settled(ctx: Any, outcome: SettleOutcome) -> dict[str, Any]:
    return {
        "ok": True,
        "reference_id": ctx.reference_id,
        "tx_hash": outcome.tx_hash,
        "identity_status": ctx.identity_status,
    }


checkout = Checkout(
    # Minimal rails so the 402 emit path has something to advertise; vendor
    # swaps in their real rails (multi-rail, Stripe-anchored, etc.).
    rails={"tempo": TempoRailSpec(recipient=os.environ.get("TEMPO_RECIPIENT", "0xfeedface"))},
    url="https://api.example.com/buy",
    compute_pricing=_compute_pricing,
    on_settled=_on_settled,
    gate=CheckoutGateConfig(
        api_key=os.environ["AGENTSCORE_API_KEY"],
        merchant_name="Compliance Demo",
        require_kyc=True,
        require_sanctions_clear=True,
        min_age=21,
        allowed_jurisdictions=["US"],
        on_denied=_on_denied,
    ),
)


app = FastAPI()
app.add_middleware(RateLimitMiddleware)


@app.post("/buy")
async def buy(request: Request) -> JSONResponse:
    return await checkout.handle_fastapi(request)
