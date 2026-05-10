"""AIOHTTP integration for trust-gating requests using AgentScore."""

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
from agentscore_commerce.identity.client import (
    GateClient,
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
    VerifyWalletSignerMatchOptions,
    VerifyWalletSignerResult,
    apply_degraded,
)
from agentscore_commerce.payment.signer import (
    extract_payment_signer,
    extract_payment_signer_address,
    read_x402_payment_header,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiohttp import web

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"
GATE_STATE_KEY = "__agentscore_gate"
ASSESS_STATE_KEY = "agentscore"


def _mark_degraded_aiohttp(request: web.Request, infra_reason: str) -> None:
    """Stamp the gate state on an aiohttp request as fail-open'd."""
    apply_degraded(request.get(GATE_STATE_KEY), infra_reason)


__all__ = [
    "FIXABLE_DENIAL_REASONS",
    "CreateSessionOnMissing",
    "agentscore_gate_middleware",
    "build_contact_support_next_steps",
    "build_signer_mismatch_body",
    "capture_wallet",
    "denial_reason_status",
    "denial_reason_to_body",
    "extract_payment_signer",
    "extract_payment_signer_address",
    "get_agentscore_data",
    "get_gate_degraded_state",
    "get_gate_quota_info",
    "is_fixable_denial",
    "read_x402_payment_header",
    "verification_agent_instructions",
    "verify_wallet_signer_match",
]


def get_agentscore_data(request: web.Request) -> dict[str, Any] | None:
    """Return the `/v1/assess` response the middleware stashed on the aiohttp request dict.

    Returns ``None`` when identity was missing or the gate short-circuited with a
    denial.
    """
    return request.get(ASSESS_STATE_KEY)


def get_gate_degraded_state(request: web.Request) -> dict[str, Any]:
    """Return whether the gate fail-open'd due to AgentScore-side infra failure.

    Returns ``{"degraded": False}`` for normal allows; ``{"degraded": True,
    "infra_reason": "quota_exceeded" | "api_error" | "network_timeout"}`` when bypassed.
    """
    state = request.get(GATE_STATE_KEY)
    if isinstance(state, dict) and state.get("degraded"):
        return {"degraded": True, "infra_reason": state.get("infra_reason")}
    return {"degraded": False}


def get_gate_quota_info(request: web.Request) -> GateQuotaInfo | None:
    """Read AgentScore assess quota observability for this request.

    Captured from ``X-Quota-*`` response headers on this request's gate evaluate.
    """
    state = request.get(GATE_STATE_KEY)
    if isinstance(state, dict):
        quota = state.get("quota")
        if isinstance(quota, GateQuotaInfo):
            return quota
    return None


def _default_extract_identity(request: web.Request) -> AgentIdentity | None:
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


def _default_extract_chain(_request: web.Request) -> str | None:
    return None


def _default_on_denied(_request: web.Request, reason: DenialReason) -> tuple[dict[str, Any], int]:
    return denial_reason_to_body(reason), denial_reason_status(reason)


def agentscore_gate_middleware(
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
    extract_identity: Callable[[web.Request], AgentIdentity | None] | None = None,
    extract_chain: Callable[[web.Request], str | None] | None = None,
    on_denied: Callable[[web.Request, DenialReason], tuple[dict[str, Any], int]] | None = None,
    create_session_on_missing: CreateSessionOnMissing | None = None,
) -> Callable[[web.Request, Callable[[web.Request], Awaitable[web.StreamResponse]]], Awaitable[web.StreamResponse]]:
    """Build an AIOHTTP middleware that gates requests on AgentScore trust.

    Usage::

        from aiohttp import web
        from agentscore_commerce.identity.aiohttp import agentscore_gate_middleware

        app = web.Application()
        app.middlewares.append(agentscore_gate_middleware(api_key="ask_...", require_kyc=True))
    """
    from aiohttp import web

    client = GateClient(
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

    @web.middleware
    async def _agentscore_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        identity = _resolve_identity(request)
        # Stash state on the request dict so capture_wallet() can read operator_token + client
        # after the handler runs.
        request[GATE_STATE_KEY] = {
            "client": client,
            "operator_token": identity.operator_token if identity else None,
            "wallet_address": identity.address if identity else None,
        }

        if not identity:
            if client.fail_open:
                return await handler(request)
            if create_session_on_missing is not None:
                session_reason = await try_create_session_denial_reason(
                    create_session_on_missing,
                    client.user_agent,
                    request,
                )
                if session_reason is not None:
                    body, status = _on_denied(request, session_reason)
                    return web.json_response(body, status=status)
            body, status = _on_denied(request, build_missing_identity_reason())
            return web.json_response(body, status=status)

        chain_override = _extract_chain(request)

        # Only acheck_identity is wrapped — the downstream handler call must NOT be in the
        # try, otherwise an exception in the user's route would be misclassified as an
        # AgentScore infra failure and (under fail_open) re-invoke their handler.
        try:
            result = await client.acheck_identity(identity, chain_override)
        except PaymentRequiredError:
            if client.fail_open:
                return await handler(request)
            body, status = _on_denied(request, DenialReason(code="payment_required"))
            return web.json_response(body, status=status)
        except TokenDeniedError as err:
            reason = build_token_denied_reason(err)
            body, status = _on_denied(request, reason)
            return web.json_response(body, status=status)
        except InvalidCredentialError:
            # Permanent — no auto-session, agent should switch tokens or restart.
            body, status = _on_denied(request, build_invalid_credential_reason())
            return web.json_response(body, status=status)
        except QuotaExceededError:
            if client.fail_open:
                _mark_degraded_aiohttp(request, "quota_exceeded")
                return await handler(request)
            body, status = _on_denied(
                request,
                DenialReason(code="api_error", agent_instructions=QUOTA_EXCEEDED_INSTRUCTIONS),
            )
            return web.json_response(body, status=status)
        except httpx.TimeoutException:
            if client.fail_open:
                _mark_degraded_aiohttp(request, "network_timeout")
                return await handler(request)
            body, status = _on_denied(request, DenialReason(code="api_error"))
            return web.json_response(body, status=status)
        except Exception:
            if client.fail_open:
                _mark_degraded_aiohttp(request, "api_error")
                return await handler(request)
            body, status = _on_denied(request, DenialReason(code="api_error"))
            return web.json_response(body, status=status)

        if result.allow:
            request["agentscore"] = result.raw
            if result.quota is not None:
                state = request.get(GATE_STATE_KEY)
                if isinstance(state, dict):
                    state["quota"] = result.quota
            return await handler(request)

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
                return web.json_response(body, status=status)

        reason = DenialReason(
            code="wallet_not_trusted",
            decision=result.decision,
            reasons=result.reasons,
            verify_url=result.verify_url,
        )
        body, status = _on_denied(request, reason)
        return web.json_response(body, status=status)

    return _agentscore_middleware


async def verify_wallet_signer_match(
    request: web.Request,
    signer: str | None,
    network: Network = "evm",
) -> VerifyWalletSignerResult:
    """Verify payment signer matches claimed X-Wallet-Address.

    No-ops when operator-token-authenticated or when both headers were sent. See
    :func:`agentscore_commerce.identity.middleware.verify_wallet_signer_match` for the full contract.
    """
    state = request.get(GATE_STATE_KEY)
    if not state or not state.get("wallet_address") or state.get("operator_token"):
        return VerifyWalletSignerResult(kind="pass")
    return await state["client"].averify_wallet_signer_match(
        VerifyWalletSignerMatchOptions(
            claimed_wallet=state["wallet_address"],
            signer=signer,
            network=network,
        ),
    )


async def capture_wallet(
    request: web.Request,
    wallet_address: str,
    network: Network,
    idempotency_key: str | None = None,
) -> None:
    """Report a wallet that paid under the operator_token the AIOHTTP gate extracted on this request.

    Fire-and-forget: no-ops silently if the gate didn't run, the request was wallet-authenticated
    (no operator_token to associate), or the API call fails.

    Usage::

        async def purchase(request):
            # ... run payment, recover signer wallet from the payload ...
            await capture_wallet(request, signer, "evm", idempotency_key=payment_intent_id)
            return web.json_response({"ok": True})
    """
    state = request.get(GATE_STATE_KEY)
    if not state or not state.get("operator_token"):
        return
    await state["client"].acapture_wallet(
        state["operator_token"],
        wallet_address,
        network,
        idempotency_key=idempotency_key,
    )
