"""ASGI middleware for trust-gating requests using AgentScore."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from agentscore_commerce.identity._denial import (
    denial_reason_status,
    is_fixable_denial,
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
    from collections.abc import Awaitable, Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"
GATE_STATE_KEY = "__agentscore_gate"
ASSESS_STATE_KEY = "agentscore"


def _mark_degraded_asgi(scope: Scope, infra_reason: str) -> None:
    """Stamp the gate state on the ASGI scope as fail-open'd."""
    apply_degraded(scope.get("state", {}).get(GATE_STATE_KEY), infra_reason)


__all__ = [
    "AgentScoreGate",
    "ConditionalAgentScoreGate",
    "capture_wallet",
    "get_agentscore_data",
    "get_gate_degraded_state",
    "get_gate_quota_info",
    "get_signer_verdict",
]


def get_agentscore_data(request: Request) -> dict[str, Any] | None:
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


def get_gate_quota_info(request: Request) -> GateQuotaInfo | None:
    """Read AgentScore assess quota observability for this request.

    Captured from ``X-Quota-*`` response headers on this request's gate evaluate.
    """
    state = (request.scope.get("state") or {}).get(GATE_STATE_KEY)
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
        condition: Callable[[Request], bool] | None = None,
    ) -> None:
        self.app = app
        self._condition = condition
        self._client = AgentScoreCore(
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

        if self._condition is not None and not self._condition(request):
            await self.app(scope, receive, send)
            return

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

        chain_override = self._extract_chain(request) if self._extract_chain else None

        signer_payload: dict[str, str] | None = None
        if identity.address:
            x402_header = read_x402_payment_header(dict(request.headers))
            recovered = extract_payment_signer(x402_header)
            if recovered is not None:
                signer_payload = {"address": recovered.address, "network": recovered.network}

        # Only acheck_identity is wrapped — `await self.app(...)` (which runs the downstream
        # ASGI app) must NOT be in the try, otherwise an exception in the user's app would
        # be misclassified as an AgentScore infra failure and (under fail_open) re-invoke it.
        try:
            result = await self._client.acheck_identity(identity, chain_override, signer=signer_payload)
        except PaymentRequiredError:
            if self._client.fail_open:
                await self.app(scope, receive, send)
                return
            reason = DenialReason(code="payment_required")
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
            return
        except TokenDeniedError as err:
            reason = build_token_denied_reason(err)
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
            return
        except InvalidCredentialError:
            # Permanent — no auto-session, agent should switch tokens or restart.
            reason = build_invalid_credential_reason()
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
            return
        except QuotaExceededError:
            if self._client.fail_open:
                _mark_degraded_asgi(scope, "quota_exceeded")
                await self.app(scope, receive, send)
                return
            reason = DenialReason(code="api_error", agent_instructions=QUOTA_EXCEEDED_INSTRUCTIONS)
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
            return
        except httpx.TimeoutException:
            if self._client.fail_open:
                _mark_degraded_asgi(scope, "network_timeout")
                await self.app(scope, receive, send)
                return
            reason = DenialReason(code="api_error")
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
            return
        except Exception:
            if self._client.fail_open:
                _mark_degraded_asgi(scope, "api_error")
                await self.app(scope, receive, send)
                return
            reason = DenialReason(code="api_error")
            response = await self._on_denied(request, reason)
            await response(scope, receive, send)
            return

        if result.allow:
            scope["state"] = {**scope.get("state", {}), "agentscore": result.raw}
            if result.quota is not None:
                state = scope["state"].get(GATE_STATE_KEY)
                if isinstance(state, dict):
                    state["quota"] = result.quota
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


def get_signer_verdict(request: Request) -> SignerVerdict | None:
    """Synchronous read of the cached signer verdicts for the current request.

    Both ``signer_match`` (wallet-binding) and ``signer_sanctions`` (OFAC SDN wallet check)
    are composed by the gate's primary ``/v1/assess`` call on this request — single round trip.
    This getter projects them off the gate's cache; no extra HTTP call.

    Returns ``None`` when the gate didn't run with a signer: operator-token-only paths,
    discovery legs that arrive without a payment credential, and fail-open pass-throughs.

    Wallet-OFAC SDN enforcement is unconditional whenever a signer is in the request —
    SDN wallet-address hits already flip the gate to ``decision=deny`` before the handler
    runs. Merchant code typically only reads ``signer_match`` for the wallet-binding
    verdict (e.g. via :func:`build_signer_mismatch_body`).
    """
    state = request.scope.get("state", {}).get(GATE_STATE_KEY)
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


class ConditionalAgentScoreGate(AgentScoreGate):
    """ASGI middleware variant of :class:`AgentScoreGate` that fires only on settle legs.

    Discovery legs flow through to the downstream handler unauthenticated;
    settle legs trigger the full gate.

    Accepts the same kwargs as :class:`AgentScoreGate`; any ``condition`` kwarg
    is replaced with the payment-header check.
    """

    def __init__(self, app: Any, **kwargs: Any) -> None:
        from agentscore_commerce.payment.payment_header import has_payment_header

        kwargs["condition"] = has_payment_header
        super().__init__(app, **kwargs)
