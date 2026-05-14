"""Sanic integration for trust-gating requests using AgentScore."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from agentscore_commerce.identity._denial import (
    FIXABLE_DENIAL_REASONS,
    build_contact_support_next_steps,
    build_signer_mismatch_body,
    denial_reason_status,
    is_fixable_denial,
    verification_agent_instructions,
)
from agentscore_commerce.identity._response import (
    QUOTA_EXCEEDED_INSTRUCTIONS,
    build_missing_identity_reason,
    denial_reason_to_body,
)
from agentscore_commerce.identity.core import (
    AgentScoreCore,
    InvalidCredentialError,
    PaymentRequiredError,
    QuotaExceededError,
    TokenDeniedError,
    build_invalid_credential_reason,
    build_token_denied_reason,
)
from agentscore_commerce.identity.sessions import CreateSessionOnMissing, try_create_session_denial_reason
from agentscore_commerce.identity.types import (
    AgentIdentity,
    DenialReason,
    GateQuotaInfo,
    Network,
    SignerVerdict,
    apply_degraded,
)
from agentscore_commerce.payment.signer import (
    extract_payment_signer,
    read_x402_payment_header,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sanic import HTTPResponse, Request, Sanic

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"
GATE_STATE_ATTR = "_agentscore_gate"
ASSESS_STATE_ATTR = "agentscore"


def _mark_degraded_sanic(request: Request, infra_reason: str) -> None:
    """Stamp the gate state on a Sanic request as fail-open'd."""
    apply_degraded(getattr(request.ctx, GATE_STATE_ATTR, None), infra_reason)


__all__ = [
    "FIXABLE_DENIAL_REASONS",
    "CreateSessionOnMissing",
    "agentscore_gate",
    "build_contact_support_next_steps",
    "build_signer_mismatch_body",
    "capture_wallet",
    "denial_reason_status",
    "denial_reason_to_body",
    "extract_payment_signer",
    "get_agentscore_data",
    "get_gate_degraded_state",
    "get_gate_quota_info",
    "get_signer_verdict",
    "is_fixable_denial",
    "read_x402_payment_header",
    "verification_agent_instructions",
]


def get_agentscore_data(request: Request) -> dict[str, Any] | None:
    """Return the `/v1/assess` response the middleware stashed on ``request.ctx``.

    Returns ``None`` when identity was missing or the gate short-circuited with a
    denial.
    """
    return getattr(request.ctx, ASSESS_STATE_ATTR, None)


def get_gate_degraded_state(request: Request) -> dict[str, Any]:
    """Return whether the gate fail-open'd due to AgentScore-side infra failure.

    Returns ``{"degraded": False}`` for normal allows; ``{"degraded": True,
    "infra_reason": "quota_exceeded" | "api_error" | "network_timeout"}`` when bypassed.
    """
    state = getattr(request.ctx, GATE_STATE_ATTR, None)
    if isinstance(state, dict) and state.get("degraded"):
        return {"degraded": True, "infra_reason": state.get("infra_reason")}
    return {"degraded": False}


def get_gate_quota_info(request: Request) -> GateQuotaInfo | None:
    """Read AgentScore assess quota observability for this request.

    Captured from ``X-Quota-*`` response headers on this request's gate evaluate.
    """
    state = getattr(request.ctx, GATE_STATE_ATTR, None)
    if isinstance(state, dict):
        quota = state.get("quota")
        if isinstance(quota, GateQuotaInfo):
            return quota
    return None


def _default_extract_identity(request: Request) -> AgentIdentity | None:
    token = request.headers.get(DEFAULT_TOKEN_HEADER)
    addr = request.headers.get(DEFAULT_ADDRESS_HEADER)
    identity = AgentIdentity()
    if token and len(token) > 0:
        identity.operator_token = token
    if addr and len(addr) > 0:
        identity.address = addr
    if identity.operator_token or identity.address:
        return identity
    return None


def _default_extract_chain(_request: Request) -> str | None:
    return None


def _default_on_denied(_request: Request, reason: DenialReason) -> tuple[dict[str, Any], int]:
    return denial_reason_to_body(reason), denial_reason_status(reason)


def agentscore_gate(
    app: Sanic,
    *,
    api_key: str,
    require_kyc: bool | None = None,
    require_sanctions_clear: bool | None = None,
    min_age: int | None = None,
    blocked_jurisdictions: list[str] | None = None,
    allowed_jurisdictions: list[str] | None = None,
    fail_open: bool = False,
    cache_seconds: int = 300,
    base_url: str = "https://api.agentscore.sh",
    chain: str | None = None,
    user_agent: str | None = None,
    extract_identity: Callable[[Request], AgentIdentity | None] | None = None,
    extract_chain: Callable[[Request], str | None] | None = None,
    on_denied: Callable[[Request, DenialReason], tuple[dict[str, Any], int]] | None = None,
    create_session_on_missing: CreateSessionOnMissing | None = None,
) -> None:
    """Register AgentScore gate as a Sanic request middleware.

    Usage::

        from sanic import Sanic
        from agentscore_commerce.identity.sanic import agentscore_commerce.identity

        app = Sanic("myapp")
        agentscore_gate(app, api_key="ask_...", require_kyc=True)
    """
    from sanic import response

    client = AgentScoreCore(
        api_key=api_key,
        require_kyc=require_kyc,
        require_sanctions_clear=require_sanctions_clear,
        min_age=min_age,
        blocked_jurisdictions=blocked_jurisdictions,
        allowed_jurisdictions=allowed_jurisdictions,
        fail_open=fail_open,
        cache_seconds=cache_seconds,
        base_url=base_url,
        chain=chain,
        user_agent=user_agent,
    )
    _resolve_identity = extract_identity or _default_extract_identity
    _extract_chain = extract_chain or _default_extract_chain
    _on_denied = on_denied or _default_on_denied

    @app.middleware("request")
    async def _agentscore_check(request: Request) -> HTTPResponse | None:
        identity = _resolve_identity(request)
        # Stash state on request.ctx so capture_wallet() can look up operator_token + client
        # after the handler runs.
        setattr(
            request.ctx,
            GATE_STATE_ATTR,
            {
                "client": client,
                "operator_token": identity.operator_token if identity else None,
                "wallet_address": identity.address if identity else None,
            },
        )

        if not identity:
            if client.fail_open:
                return None
            if create_session_on_missing is not None:
                session_reason = await try_create_session_denial_reason(
                    create_session_on_missing,
                    client.user_agent,
                    request,
                )
                if session_reason is not None:
                    body, status = _on_denied(request, session_reason)
                    return response.json(body, status=status)
            body, status = _on_denied(request, build_missing_identity_reason())
            return response.json(body, status=status)

        chain_override = _extract_chain(request)

        signer_payload: dict[str, str] | None = None
        if identity.address:
            x402_header = read_x402_payment_header(dict(request.headers))
            recovered = extract_payment_signer(x402_header)
            if recovered is not None:
                signer_payload = {"address": recovered.address, "network": recovered.network}

        try:
            result = await client.acheck_identity(identity, chain_override, signer=signer_payload)

            if result.allow:
                request.ctx.agentscore = result.raw
                if result.quota is not None:
                    state = getattr(request.ctx, GATE_STATE_ATTR, None)
                    if isinstance(state, dict):
                        state["quota"] = result.quota
                return None

            # Fixable compliance denials (kyc_required, kyc_pending, kyc_failed) get the
            # same UX as missing_identity: the gate mints a fresh verification session,
            # the agent polls until status=verified, gets a fresh opc_..., and retries
            # with X-Operator-Token. Unfixable reasons (sanctions_flagged, age_insufficient,
            # jurisdiction_restricted) keep the bare wallet_not_trusted denial.
            # `jurisdiction_restricted` is unfixable: the API only emits it after KYC is
            # verified (the user's KYC'd country is in the blocked list — re-doing KYC
            # won't change the country).
            if is_fixable_denial(result.reasons) and create_session_on_missing is not None:
                session_reason = await try_create_session_denial_reason(
                    create_session_on_missing,
                    client.user_agent,
                    request,
                )
                if session_reason is not None:
                    body, status = _on_denied(request, session_reason)
                    return response.json(body, status=status)

            reason = DenialReason(
                code="wallet_not_trusted",
                decision=result.decision,
                reasons=result.reasons,
                verify_url=result.verify_url,
            )
            body, status = _on_denied(request, reason)
            return response.json(body, status=status)
        except PaymentRequiredError:
            if client.fail_open:
                return None
            body, status = _on_denied(request, DenialReason(code="payment_required"))
            return response.json(body, status=status)
        except TokenDeniedError as err:
            reason = build_token_denied_reason(err)
            body, status = _on_denied(request, reason)
            return response.json(body, status=status)
        except InvalidCredentialError:
            # Permanent — no auto-session, agent should switch tokens or restart.
            body, status = _on_denied(request, build_invalid_credential_reason())
            return response.json(body, status=status)
        except QuotaExceededError:
            if client.fail_open:
                _mark_degraded_sanic(request, "quota_exceeded")
                return None
            body, status = _on_denied(
                request,
                DenialReason(code="api_error", agent_instructions=QUOTA_EXCEEDED_INSTRUCTIONS),
            )
            return response.json(body, status=status)
        except httpx.TimeoutException:
            if client.fail_open:
                _mark_degraded_sanic(request, "network_timeout")
                return None
            body, status = _on_denied(request, DenialReason(code="api_error"))
            return response.json(body, status=status)
        except Exception:
            if client.fail_open:
                _mark_degraded_sanic(request, "api_error")
                return None
            body, status = _on_denied(request, DenialReason(code="api_error"))
            return response.json(body, status=status)


def get_signer_verdict(request: Request) -> SignerVerdict | None:
    """Synchronous read of the cached signer verdicts for the current request.

    Returns ``None`` for operator-token-only requests, for requests with no payment
    credential, or for fail-open pass-throughs (no assess call).
    """
    state = getattr(request.ctx, GATE_STATE_ATTR, None)
    if not state or not state.get("wallet_address"):
        return None
    client = state.get("client")
    if client is None:
        return None
    return client.get_signer_verdict(state["wallet_address"])


async def capture_wallet(
    request: Request,
    wallet_address: str,
    network: Network,
    idempotency_key: str | None = None,
) -> None:
    """Report a wallet that paid under the operator_token the Sanic gate extracted on this request.

    Fire-and-forget: no-ops silently if the gate didn't run, the request was wallet-authenticated
    (no operator_token to associate), or the API call fails.

    Usage::

        @app.post("/purchase")
        async def purchase(request):
            # ... run payment, recover signer wallet from the payload ...
            await capture_wallet(request, signer, "evm", idempotency_key=payment_intent_id)
            return response.json({"ok": True})
    """
    state = getattr(request.ctx, GATE_STATE_ATTR, None)
    if not state or not state.get("operator_token"):
        return
    await state["client"].acapture_wallet(
        state["operator_token"],
        wallet_address,
        network,
        idempotency_key=idempotency_key,
    )
