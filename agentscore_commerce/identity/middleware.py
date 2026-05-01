"""ASGI middleware for trust-gating requests using AgentScore."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from agentscore_commerce.identity._denial import (
    FIXABLE_DENIAL_REASONS,
    build_contact_support_next_steps,
    build_signer_mismatch_body,
    denial_reason_status,
    is_fixable_denial,
    verification_agent_instructions,
)
from agentscore_commerce.identity._response import build_missing_identity_reason, denial_reason_to_body
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
    Network,
    VerifyWalletSignerMatchOptions,
    VerifyWalletSignerResult,
)
from agentscore_commerce.payment.signer import (
    extract_payment_signer,
    extract_payment_signer_address,
    read_x402_payment_header,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"
GATE_STATE_KEY = "__agentscore_gate"
ASSESS_STATE_KEY = "agentscore"


def _mark_degraded_asgi(scope: Scope, infra_reason: str) -> None:
    """Record fail-open due to AgentScore-side infra failure on the ASGI scope state."""
    state = scope.get("state", {}).get(GATE_STATE_KEY)
    if isinstance(state, dict):
        state["degraded"] = True
        state["infra_reason"] = infra_reason


__all__ = [
    "FIXABLE_DENIAL_REASONS",
    "AgentScoreGate",
    "CreateSessionOnMissing",
    "build_contact_support_next_steps",
    "build_signer_mismatch_body",
    "capture_wallet",
    "denial_reason_status",
    "denial_reason_to_body",
    "extract_payment_signer",
    "extract_payment_signer_address",
    "get_assess_data",
    "get_gate_degraded_state",
    "is_fixable_denial",
    "read_x402_payment_header",
    "verification_agent_instructions",
    "verify_wallet_signer_match",
]


def get_assess_data(request: Request) -> dict[str, Any] | None:
    """Return the `/v1/assess` response the middleware stashed on the request scope.

    Returns ``None`` when identity was missing or the gate short-circuited with a
    denial.
    """
    state = request.scope.get("state") or {}
    return state.get(ASSESS_STATE_KEY)


def get_gate_degraded_state(request: Request) -> dict[str, Any]:
    """Return whether the gate fail-open'd due to AgentScore-side infra failure.

    Returns ``{"degraded": False}`` for normal allows; ``{"degraded": True,
    "infra_reason": "quota_exceeded" | "api_error" | "network_timeout"}`` when bypassed.
    """
    state = (request.scope.get("state") or {}).get(GATE_STATE_KEY)
    if isinstance(state, dict) and state.get("degraded"):
        return {"degraded": True, "infra_reason": state.get("infra_reason")}
    return {"degraded": False}


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


async def _default_on_denied(_request: Request, reason: DenialReason) -> JSONResponse:
    return JSONResponse(denial_reason_to_body(reason), status_code=denial_reason_status(reason))


class AgentScoreGate:
    """ASGI middleware that gates requests based on AgentScore wallet reputation.

    Usage with Starlette / FastAPI::

        app.add_middleware(
            AgentScoreGate,
            api_key="ask_...",
            require_kyc=True,
        )
    """

    def __init__(
        self,
        app: ASGIApp,
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
        on_denied: Callable[[Request, DenialReason], Awaitable[JSONResponse]] | None = None,
        create_session_on_missing: CreateSessionOnMissing | None = None,
    ) -> None:
        self.app = app
        self._client = GateClient(
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
        self._extract_identity = extract_identity or _default_extract_identity
        self._extract_chain = extract_chain
        self._on_denied = on_denied or _default_on_denied
        self._create_session_on_missing = create_session_on_missing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)

        identity = self._extract_identity(request)
        # Stash state for capture_wallet() helper to read after the handler runs.
        scope.setdefault("state", {})
        scope["state"][GATE_STATE_KEY] = {
            "client": self._client,
            "operator_token": identity.operator_token if identity else None,
            "wallet_address": identity.address if identity else None,
        }
        if not identity:
            if self._client.fail_open:
                await self.app(scope, receive, send)
                return

            if self._create_session_on_missing:
                session_reason = await try_create_session_denial_reason(
                    self._create_session_on_missing,
                    self._client.user_agent,
                    request,
                )
                if session_reason is not None:
                    response = await self._on_denied(request, session_reason)
                    await response(scope, receive, send)
                    return

            reason = build_missing_identity_reason()
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
            return

        try:
            chain_override = self._extract_chain(request) if self._extract_chain else None
            result = await self._client.acheck_identity(identity, chain_override)

            if result.allow:
                scope["state"] = {**scope.get("state", {}), "agentscore": result.raw}
                await self.app(scope, receive, send)
                return

            # Fixable compliance denials (kyc_required, kyc_pending, kyc_failed) get the
            # same UX as missing_identity: the gate mints a fresh verification session,
            # the agent polls until status=verified, gets a fresh opc_..., and retries
            # with X-Operator-Token. Unfixable reasons (sanctions_flagged, age_insufficient,
            # jurisdiction_restricted) keep the bare wallet_not_trusted denial.
            # `jurisdiction_restricted` is unfixable: the API only emits it after KYC is
            # verified (the user's KYC'd country is in the blocked list — re-doing KYC
            # won't change the country).
            if is_fixable_denial(result.reasons) and self._create_session_on_missing is not None:
                session_reason = await try_create_session_denial_reason(
                    self._create_session_on_missing,
                    self._client.user_agent,
                    request,
                )
                if session_reason is not None:
                    response = await self._on_denied(request, session_reason)
                    await response(scope, receive, send)
                    return

            reason = DenialReason(
                code="wallet_not_trusted",
                decision=result.decision,
                reasons=result.reasons,
                verify_url=result.verify_url,
            )
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
        except PaymentRequiredError:
            if self._client.fail_open:
                await self.app(scope, receive, send)
                return
            reason = DenialReason(code="payment_required")
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
        except TokenDeniedError as err:
            reason = build_token_denied_reason(err)
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
        except InvalidCredentialError:
            # Permanent — no auto-session, agent should switch tokens or restart.
            reason = build_invalid_credential_reason()
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
        except QuotaExceededError:
            if self._client.fail_open:
                _mark_degraded_asgi(scope, "quota_exceeded")
                await self.app(scope, receive, send)
                return
            reason = DenialReason(code="api_error")
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
        except httpx.TimeoutException:
            if self._client.fail_open:
                _mark_degraded_asgi(scope, "network_timeout")
                await self.app(scope, receive, send)
                return
            reason = DenialReason(code="api_error")
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
        except Exception:
            if self._client.fail_open:
                _mark_degraded_asgi(scope, "api_error")
                await self.app(scope, receive, send)
                return
            reason = DenialReason(code="api_error")
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)


async def verify_wallet_signer_match(
    request: Request,
    signer: str | None,
    network: Network = "evm",
) -> VerifyWalletSignerResult:
    """Verify the payment signer resolves to the same operator as the claimed X-Wallet-Address.

    Call this AFTER parsing the payment credential, BEFORE settlement. Returns:

    * ``kind='pass'`` — byte-equal or same-operator match
    * ``kind='wallet_signer_mismatch'`` — different operator / unlinked signer
    * ``kind='wallet_auth_requires_wallet_signing'`` — signer is None (SPT/card)

    No-ops (returns ``pass`` with ``claimed_operator=None``) when the request was operator-token
    authenticated or when both headers were sent (operator-token wins — the caller opted out of
    strict wallet-auth). Signer-match only runs on strict wallet-auth requests.
    """
    state = request.scope.get("state", {}).get(GATE_STATE_KEY)
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
    request: Request,
    wallet_address: str,
    network: Network,
    idempotency_key: str | None = None,
) -> None:
    """Report a wallet that paid under the operator_token the ASGI gate extracted on this request.

    Fire-and-forget: no-ops silently if the gate didn't run, the request was wallet-authenticated
    (no operator_token to associate), or the API call fails. Use the payment intent id / tx hash
    as ``idempotency_key`` so agent retries of the same payment don't inflate transaction_count.

    Usage (FastAPI)::

        @app.post("/purchase")
        async def purchase(request: Request):
            # ... run payment, recover signer wallet from the payload ...
            await capture_wallet(request, signer, "evm", idempotency_key=payment_intent_id)
            return {"ok": True}
    """
    state = request.scope.get("state", {}).get(GATE_STATE_KEY)
    if not state or not state.get("operator_token"):
        return
    await state["client"].acapture_wallet(
        state["operator_token"],
        wallet_address,
        network,
        idempotency_key=idempotency_key,
    )
