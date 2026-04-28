"""Example: regulated-goods merchant showcasing the gate + denial helpers

Scenario: you sell something that needs identity gating — wine (age 21+, US-only), cannabis
(age 21+, state allowlist), high-value items (KYC + sanctions). The agent needs to know how
to recover from each kind of denial.

What this example demonstrates:
    - AgentScoreGate with full compliance policy (KYC + sanctions + age + jurisdiction)
    - Custom on_denied composing commerce helpers:
        * verification_agent_instructions for the canonical poll-and-retry instructions
        * is_fixable_denial to branch fixable (KYC re-do) vs unfixable (sanctions/age)
        * build_contact_support_next_steps for the unfixable branch
        * denial_reason_to_body + denial_reason_status for the standard fall-through
          (token_expired, invalid_credential, api_error get the right status + body for free)
    - verify_wallet_signer_match + build_signer_mismatch_body for wallet-auth verification

The pattern: vendors only write the BUSINESS-SPECIFIC denial branches. Everything else is a
one-line helper call.

Peer deps:
    pip install agentscore-commerce[fastapi]

Env vars:
    AGENTSCORE_API_KEY — your AgentScore API key

Run: uvicorn examples.compliance_merchant:app --port 3000
"""

import os
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from agentscore_commerce.identity import (
    DenialReason,
    build_contact_support_next_steps,
    build_signer_mismatch_body,
    denial_reason_status,
    denial_reason_to_body,
    is_fixable_denial,
    verification_agent_instructions,
)
from agentscore_commerce.identity.fastapi import AgentScoreGate, get_assess_data, verify_wallet_signer_match

SUPPORT_EMAIL = "support@example.com"

# Vendor-specific extension of the canonical agent_instructions block. The commerce default
# covers steps 1-4 (present verify_url, poll, user verifies, extract token) plus a generic
# "retry the original merchant request" at step 5. ``retry_step`` REPLACES that generic step 5
# with our merchant-specific retry (include order_id to resume the pending order). ``extra_steps``
# adds the genuinely-additional 402-payment step that comes AFTER retry.
VERIFICATION_INSTRUCTIONS = verification_agent_instructions(
    retry_step=(
        "Retry the request with header X-Operator-Token set to the operator_token value AND include the "
        "order_id from this 403 in the body to resume the pending order."
    ),
    extra_steps=[
        "The retry returns 402 Payment Required with a payment challenge. Pay via tempo request or agentscore-pay pay.",
    ],
    order_ttl="Pending orders expire after 1 hour. If the order expires, start a new request.",
)


def _on_denied(_request: Request, reason: DenialReason) -> tuple[dict[str, Any], int]:
    # missing_identity → bare 403 (no auto-session created — agent must bootstrap).
    if reason.code == "missing_identity":
        body = denial_reason_to_body(reason)
        body["error"] = {"code": "identity_required", "message": "Identity verification is required for this purchase."}
        return body, 403

    # identity_verification_required → gate auto-minted a session. Overlay vendor-specific
    # agent_instructions on top of the commerce body.
    if reason.code == "identity_verification_required":
        body = denial_reason_to_body(reason)
        body["agent_instructions"] = VERIFICATION_INSTRUCTIONS
        return body, 403

    # wallet_not_trusted = compliance fail. Branch on fixable vs not — fixable (KYC pending/failed/
    # required, jurisdiction) gets a fresh session; unfixable (sanctions, age) gets contact-support.
    if reason.code == "wallet_not_trusted":
        reasons = reason.reasons or []
        if is_fixable_denial(reasons):
            return {
                "error": {"code": "compliance_recoverable", "message": "Re-verify identity and retry."},
                "reasons": reasons,
                "verify_url": reason.verify_url,
            }, 403
        return {
            "error": {
                "code": "compliance_denied",
                "message": "Purchase denied by compliance policy. Not resolvable through re-verification.",
            },
            "reasons": reasons,
            "next_steps": build_contact_support_next_steps(SUPPORT_EMAIL),
        }, 403

    # token_expired (401), invalid_credential (401), api_error (503) → standard body+status from commerce.
    return denial_reason_to_body(reason), denial_reason_status(reason)


app = FastAPI()
gate = AgentScoreGate(
    api_key=os.environ["AGENTSCORE_API_KEY"],
    require_kyc=True,
    require_sanctions_clear=True,
    min_age=21,
    allowed_jurisdictions=["US"],
    on_denied=_on_denied,
)


@app.post("/buy", dependencies=[Depends(gate)])
async def buy(request: Request, assess: dict = Depends(get_assess_data)):
    # Wallet-auth: verify the payment signer matches the claimed wallet (or a same-operator
    # linked wallet). No-ops for operator_token requests. Pass `signer=` from your real x402/MPP
    # credential extraction (use extract_payment_signer from commerce.payment, etc.).
    signer_match = await verify_wallet_signer_match(request, signer=None)
    mismatch_body = build_signer_mismatch_body(signer_match)
    if mismatch_body:
        return JSONResponse(mismatch_body, status_code=403)

    # Compliance + signer-match passed. Run the actual purchase.
    return {"ok": True, "identity_method": assess.get("identity_method")}
