"""Example: regulated-goods merchant showcasing the gate + denial helpers

Scenario: you sell something that needs identity gating — wine (age 21+, US-only), cannabis
(age 21+, state allowlist), high-value items (KYC + sanctions). The agent needs to know how
to recover from each kind of denial.

What this example demonstrates:
    - AgentScoreGate with full compliance policy (KYC + sanctions + age + jurisdiction)
    - Custom on_denied composing commerce helpers:
        * verification_agent_instructions for the canonical poll-and-retry instructions
        * is_fixable_denial defensive fallback for fixable (KYC re-do) vs unfixable
          (sanctions / age / jurisdiction_restricted) compliance fails. Gate normally
          re-routes fixable reasons to identity_verification_required upstream — this
          branch only fires if the /v1/sessions mint blipped.
        * build_contact_support_next_steps for the unfixable branch
        * denial_reason_to_body + denial_reason_status for the standard fall-through
          (token_expired, invalid_credential, api_error get the right status + body for free)
    - get_signer_verdict (cached signer_match read) + build_signer_mismatch_body for wallet-auth verification

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
from agentscore_commerce.identity.fastapi import AgentScoreGate, get_agentscore_data, get_signer_verdict

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

    # wallet_not_trusted = UNFIXABLE compliance fail (sanctions / age / jurisdiction_restricted).
    # The gate auto-routes fixable reasons (kyc_required / kyc_pending / kyc_failed) to
    # identity_verification_required upstream — by the time on_denied sees wallet_not_trusted,
    # the reasons should be unfixable. The is_fixable_denial branch below is a defensive
    # fallback in case the gate's /v1/sessions mint blipped and fell back to bare denial.
    if reason.code == "wallet_not_trusted":
        reasons = reason.reasons or []
        if is_fixable_denial(reasons):
            # Defensive: gate normally bootstraps these into identity_verification_required.
            # If we hit this branch, the gate's /v1/sessions mint failed — surface verify_url
            # so the agent can recover via the manual session flow.
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
_gate = AgentScoreGate(
    api_key=os.environ["AGENTSCORE_API_KEY"],
    require_kyc=True,
    require_sanctions_clear=True,
    min_age=21,
    allowed_jurisdictions=["US"],
    on_denied=_on_denied,
)


# Conditional gate. Fires only when a payment credential is already attached so
# anonymous discovery returns a 402 challenge (not a 403 missing_identity). Compliance
# gating + signer-match still run on the retry leg when X-Payment / Authorization:
# Payment arrives — the full denial branching above triggers there.
async def gate_on_settle(request: Request) -> None:
    has_payment_header = bool(
        request.headers.get("payment-signature")
        or request.headers.get("x-payment")
        or (request.headers.get("authorization") or "").startswith("Payment ")
    )
    if not has_payment_header:
        return None
    return await _gate(request)


@app.post("/buy", dependencies=[Depends(gate_on_settle)])
async def buy(request: Request, assess: dict = Depends(get_agentscore_data)):
    # Wallet-auth: read the cached signer_match verdict the gate composed on its
    # primary /v1/assess call (single round trip). Returns None on operator_token paths.
    verdict = get_signer_verdict(request)
    if verdict is not None and verdict.signer_match is not None:
        mismatch_body = build_signer_mismatch_body(verdict.signer_match)
        if mismatch_body:
            return JSONResponse(mismatch_body, status_code=403)

    # Compliance + signer-match passed. Run the actual purchase.
    return {"ok": True, "identity_method": assess.get("identity_method")}
