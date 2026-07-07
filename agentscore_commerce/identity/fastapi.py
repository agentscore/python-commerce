"""FastAPI native adapter for trust-gating routes using AgentScore.

The adapter plugs into FastAPI's dependency-injection system. Unlike the generic ASGI
middleware (which gates every request), this adapter lets you scope gating to specific
routes via ``dependencies=[Depends(gate)]`` and inject the assess result with
``Depends(get_agentscore_data)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn, cast

import httpx
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.requests import Request  # noqa: TC002 - runtime import required for FastAPI DI
from starlette.responses import JSONResponse

from agentscore_commerce.aip.gate import (
    AipGateOptions,
    build_aip_error_body,
    evaluate_aip_request,
)
from agentscore_commerce.aip.request import has_agent_identity_header
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
    from collections.abc import Callable

    from agentscore_commerce.aip.gate import AipErrorBody
    from agentscore_commerce.aip.jwks import JwksCache
    from agentscore_commerce.aip.types import TrustLevel
    from agentscore_commerce.aip.verify import VerifiedAit

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"
GATE_STATE_KEY = "__agentscore_gate"
ASSESS_STATE_KEY = "agentscore"


class _GateDenialError(Exception):
    """Carries a pre-rendered denial document up to the Starlette exception handler.

    Unlike ``HTTPException(detail=body)`` — which nests the document under a ``detail``
    key — this preserves the FLAT wire contract the node adapters emit (consumers read
    ``body["type"]`` / ``body["error"]`` directly). The handler installed by
    :func:`_install_gate_denial_handler` renders it via ``JSONResponse``.
    """

    def __init__(
        self,
        body: dict[str, Any],
        status: int,
        headers: dict[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        super().__init__()
        self.body = body
        self.status = status
        self.headers = headers
        self.media_type = media_type


async def _render_gate_denial(_request: Request, exc: Exception) -> JSONResponse:
    """Starlette exception handler that emits the FLAT denial body for a :class:`_GateDenialError`."""
    denial = cast("_GateDenialError", exc)
    return JSONResponse(
        denial.body,
        status_code=denial.status,
        media_type=denial.media_type or "application/json",
        headers=denial.headers,
    )


def _install_gate_denial_handler(request: Request) -> None:
    """Register :func:`_render_gate_denial` on the live app so :class:`_GateDenialError` renders flat.

    The gate mounts via ``Depends(gate)``, which never hands the gate the ``app``, so the
    handler is wired lazily on the first request instead. By the time a dependency runs the
    app's ``middleware_stack`` is already built, so we reach the singleton
    :class:`~starlette.middleware.exceptions.ExceptionMiddleware` and insert into its live
    handler map. ``_lookup_exception_handler`` reads that map at raise-time, so even the very
    first denial on the very first request is rendered flat. Idempotent: the key is only
    inserted once.
    """
    node: Any = getattr(request.app, "middleware_stack", None)
    while node is not None:
        if isinstance(node, ExceptionMiddleware):
            handlers = node._exception_handlers
            if _GateDenialError not in handlers:
                handlers[_GateDenialError] = _render_gate_denial
            return
        node = getattr(node, "app", None)


def _mark_degraded(request: Request, infra_reason: str) -> None:
    """Stamp the per-request gate state on ``request.state`` as fail-open'd.

    Resolves the framework-specific state container; the shared mutation
    contract lives in :func:`apply_degraded`.
    """
    apply_degraded(getattr(request.state, GATE_STATE_KEY, None), infra_reason)


def get_gate_degraded_state(request: Request) -> dict[str, Any]:
    """Return whether the gate fail-open'd due to AgentScore-side infra failure.

    Returns ``{"degraded": False}`` for normal allows; ``{"degraded": True,
    "infra_reason": "quota_exceeded" | "api_error" | "network_timeout"}`` when the gate
    was bypassed (compliance NOT enforced — log/alert).

    Only set when ``fail_open=True`` was configured AND the failure was an infra failure.
    Real compliance denials never trigger fail-open and so never set this flag.
    """
    state = getattr(request.state, GATE_STATE_KEY, None)
    if isinstance(state, dict) and state.get("degraded"):
        return {"degraded": True, "infra_reason": state.get("infra_reason")}
    return {"degraded": False}


def get_gate_quota_info(request: Request) -> GateQuotaInfo | None:
    """Read AgentScore assess quota observability for this request.

    Captured from ``X-Quota-*`` response headers on this request's gate evaluate.
    Returns ``None`` when the request was a fail-open pass-through (no assess call)
    or when the API didn't emit quota headers (Enterprise / unlimited tiers).
    Use to monitor approach-to-cap proactively (warn at 80%, alert at 95%).
    """
    state = getattr(request.state, GATE_STATE_KEY, None)
    if isinstance(state, dict):
        quota = state.get("quota")
        if isinstance(quota, GateQuotaInfo):
            return quota
    return None


__all__ = [
    "AgentScoreGate",
    "AipGate",
    "ConditionalAgentScoreGate",
    "ConditionalAipGate",
    "capture_wallet",
    "get_agentscore_data",
    "get_gate_degraded_state",
    "get_gate_quota_info",
    "get_signer_verdict",
    "get_verified_ait",
]


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


def _build_denial_body(reason: DenialReason) -> dict[str, Any]:
    return denial_reason_to_body(reason)


class AgentScoreGate:
    """FastAPI dependency that gates a route on AgentScore trust.

    Instantiate once at module scope, then attach to routes via ``Depends(gate)``.
    Uses FastAPI's dependency-injection system — on a denial the dependency raises an
    internal exception that an auto-registered Starlette handler renders as a FLAT denial
    document (``body["error"]["code"]``, not nested under ``detail``), matching the node
    adapters' cross-framework wire contract; the route body is skipped.

    Usage::

        from fastapi import Depends, FastAPI
        from agentscore_commerce.identity.fastapi import AgentScoreGate, get_agentscore_data

        app = FastAPI()
        gate = AgentScoreGate(api_key="ask_...", require_kyc=True, min_age=21)

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase(assess = Depends(get_agentscore_data)):
            # assess is the raw /v1/assess response dict
            ...
    """

    def __init__(
        self,
        *,
        api_key: str,
        require_kyc: bool | None = None,
        require_sanctions_clear: bool | None = None,
        min_age: int | None = None,
        blocked_jurisdictions: list[str] | None = None,
        allowed_jurisdictions: list[str] | None = None,
        fail_open: bool = False,
        cache_seconds: int = 300,
        base_url: str = "https://api.agentscore.com",
        chain: str | None = None,
        user_agent: str | None = None,
        extract_identity: Callable[[Request], AgentIdentity | None] | None = None,
        extract_chain: Callable[[Request], str | None] | None = None,
        on_denied: Callable[
            [Request, DenialReason],
            tuple[dict[str, Any], int] | tuple[dict[str, Any], int, dict[str, str]],
        ]
        | None = None,
        create_session_on_missing: CreateSessionOnMissing | None = None,
        aip_trusted_issuers: list[str] | None = None,
    ) -> None:
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
            aip_trusted_issuers=aip_trusted_issuers,
        )
        self._extract_identity = extract_identity or _default_extract_identity
        self._extract_chain = extract_chain or _default_extract_chain
        self._on_denied = on_denied
        self._create_session_on_missing = create_session_on_missing

    def _deny(self, request: Request, reason: DenialReason) -> NoReturn:
        headers: dict[str, str] | None = None
        if self._on_denied is not None:
            result = self._on_denied(request, reason)
            if len(result) == 3:
                body, status, headers = cast("tuple[dict, int, dict[str, str]]", result)
            else:
                body, status = cast("tuple[dict, int]", result)
        else:
            body, status = _build_denial_body(reason), denial_reason_status(reason)
        raise _GateDenialError(body, status, headers)

    async def __call__(self, request: Request) -> None:
        # Wire the flat-denial exception handler onto the app on first run (Depends(gate)
        # never hands us the app at construction time). Idempotent + per-app.
        _install_gate_denial_handler(request)
        identity = self._extract_identity(request)
        # Stash state on request.state so capture_wallet() can look up operator_token + client
        # after the route handler runs.
        request.state.__setattr__(
            GATE_STATE_KEY,
            {
                "client": self._client,
                "operator_token": identity.operator_token if identity else None,
                "wallet_address": identity.address if identity else None,
            },
        )

        if not identity:
            if self._client.fail_open:
                return
            if self._create_session_on_missing is not None:
                session_reason = await try_create_session_denial_reason(
                    self._create_session_on_missing,
                    self._client.user_agent,
                    request,
                )
                if session_reason is not None:
                    self._deny(request, session_reason)
            self._deny(request, build_missing_identity_reason(self._client.aip_trusted_issuers))

        chain_override = self._extract_chain(request)

        signer_payload: dict[str, str] | None = None
        if identity.address:
            x402_header = read_x402_payment_header(dict(request.headers))
            recovered = extract_payment_signer(x402_header)
            if recovered is not None:
                signer_payload = {"address": recovered.address, "network": recovered.network}

        try:
            result = await self._client.acheck_identity(identity, chain_override, signer=signer_payload)
        except PaymentRequiredError:
            if self._client.fail_open:
                return
            self._deny(request, DenialReason(code="payment_required"))
        except TokenDeniedError as err:
            self._deny(request, build_token_denied_reason(err))
        except InvalidCredentialError:
            # Permanent — no auto-session, agent should switch tokens or restart.
            self._deny(request, build_invalid_credential_reason())
        except QuotaExceededError:
            if self._client.fail_open:
                _mark_degraded(request, "quota_exceeded")
                return
            self._deny(request, DenialReason(code="api_error", agent_instructions=QUOTA_EXCEEDED_INSTRUCTIONS))
        except httpx.TimeoutException:
            if self._client.fail_open:
                _mark_degraded(request, "network_timeout")
                return
            self._deny(request, DenialReason(code="api_error"))
        except Exception:
            if self._client.fail_open:
                _mark_degraded(request, "api_error")
                return
            self._deny(request, DenialReason(code="api_error"))

        if result.allow:
            setattr(request.state, ASSESS_STATE_KEY, result.raw)
            state = getattr(request.state, GATE_STATE_KEY, None)
            if isinstance(state, dict):
                # Stash quota on gate state so get_gate_quota_info(request) can read it.
                if result.quota is not None:
                    state["quota"] = result.quota
                # Request-scope the signer verdict: project it from THIS request's raw response
                # and stash it on per-request state so get_signer_verdict(request) reads a
                # verdict that can't be raced by a concurrent request claiming the same wallet
                # with a different signer (the shared core's _last_signer_raw slot would).
                if identity.address and signer_payload is not None:
                    state["signer_verdict"] = self._client.project_signer_verdict(result.raw, identity.address)
            return

        # Fixable compliance denials (kyc_required, kyc_pending, kyc_failed) get the
        # same UX as missing_identity: the gate mints a fresh verification session, the
        # agent polls until status=verified, gets a fresh opc_..., and retries with
        # X-Operator-Token. No "go to verify_url and tell us when done" gap.
        # Unfixable reasons (sanctions_flagged, age_insufficient, jurisdiction_restricted)
        # keep the bare wallet_not_trusted denial — re-verification won't fix them.
        # `jurisdiction_restricted` is unfixable because the API only emits it AFTER KYC
        # is verified (the user's KYC'd country is in the blocked list).
        if is_fixable_denial(result.reasons) and self._create_session_on_missing is not None:
            session_reason = await try_create_session_denial_reason(
                self._create_session_on_missing,
                self._client.user_agent,
                request,
            )
            if session_reason is not None:
                self._deny(request, session_reason)

        self._deny(
            request,
            DenialReason(
                code="wallet_not_trusted",
                decision=result.decision,
                reasons=result.reasons,
                verify_url=result.verify_url,
            ),
        )


def get_agentscore_data(request: Request) -> dict[str, Any] | None:
    """FastAPI dependency that returns the raw ``/v1/assess`` response for the current request.

    Returns ``None`` when the gate was bypassed via ``fail_open`` or the route wasn't gated.
    Usage::

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase(assess = Depends(get_agentscore_data)):
            ...
    """
    return getattr(request.state, ASSESS_STATE_KEY, None)


def get_signer_verdict(request: Request) -> SignerVerdict | None:
    """Synchronous read of the cached signer verdicts for the current request.

    The gate middleware pre-extracts the payment signer and passes it to ``/v1/assess``
    so the API returns ``signer_match`` + ``signer_sanctions`` blocks on the same
    round trip. This getter reads those projected verdicts off the gate's cache; no
    extra HTTP call.

    Returns ``None`` for operator-token-only requests, for requests with no payment
    credential yet (discovery legs), and for fail-open pass-throughs (no assess call).

    Reads the request-scoped verdict stashed by the gate (projected from THIS request's
    assess response). This is concurrency-safe: a sibling request claiming the same wallet
    with a different signer has its own per-request state and can't overwrite this one.
    """
    state = getattr(request.state, GATE_STATE_KEY, None)
    if not isinstance(state, dict):
        return None
    return state.get("signer_verdict")


async def capture_wallet(
    request: Request,
    wallet_address: str,
    network: Network,
    idempotency_key: str | None = None,
) -> None:
    """Report a wallet that paid under the operator_token the FastAPI gate extracted on this request.

    Fire-and-forget: no-ops silently if the gate didn't run, the request was wallet-authenticated
    (no operator_token to associate), or the API call fails.

    Usage::

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase(request: Request, assess = Depends(get_agentscore_data)):
            # ... run payment, recover signer wallet from the payload ...
            await capture_wallet(request, signer, "evm", idempotency_key=payment_intent_id)
            return {"ok": True}
    """
    state = getattr(request.state, GATE_STATE_KEY, None)
    if not state or not state.get("operator_token"):
        return
    await state["client"].acapture_wallet(
        state["operator_token"],
        wallet_address,
        network,
        idempotency_key=idempotency_key,
    )


class ConditionalAgentScoreGate:
    """Wrap :class:`AgentScoreGate` to fire only on settle legs.

    Discovery legs (no ``payment-signature`` / ``x-payment`` /
    ``Authorization: Payment``) flow through to the handler unauthenticated;
    settle legs trigger the full gate.

    Use this for routes that should support anonymous discovery — the 402
    emit path advertises all rails to any x402 wallet, and identity is
    verified at settle time on the retry leg.

    Example::

        gate = ConditionalAgentScoreGate(api_key=..., require_kyc=True)

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase(request: Request): ...
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from agentscore_commerce.payment.payment_header import has_payment_header

        self._inner = AgentScoreGate(*args, **kwargs)
        self._has_payment_header = has_payment_header

    async def __call__(self, request: Request) -> None:
        if not self._has_payment_header(request):
            return
        await self._inner(request)


# ---------------------------------------------------------------------------
# AIP gate (Agentic Identity Protocol) — verifies a key-bound Agent Identity Token (AIT)
# from a trusted IdP instead of an opaque operator token. Cryptographic identity only;
# merchants who want compliance enrichment feed the verified claims to ``/v1/assess``.
# Starlette's ``Request`` already satisfies ``RequestLike`` (method / url / headers), so the
# FastAPI adapter verifies straight off the request via ``evaluate_aip_request`` and keeps the
# same ``Depends(gate)`` + ``Depends(get_verified_ait)`` shape as :class:`AgentScoreGate`.
# ---------------------------------------------------------------------------

AIT_STATE_KEY = "__agentscore_ait"


class AipGate:
    """FastAPI dependency that requires a valid AIT on a route.

    Instantiate once at module scope with a :class:`~agentscore_commerce.aip.jwks.JwksCache`,
    then attach to routes via ``Depends(gate)`` and read the verified token back with
    ``Depends(get_verified_ait)``. On a verify/trust failure the dependency raises an internal
    exception that an auto-registered Starlette handler renders as the FLAT RFC 9457
    ``application/problem+json`` body (``body["type"]``, not nested under ``detail``), so the
    route body is skipped.

    Usage::

        from fastapi import Depends, FastAPI
        from agentscore_commerce.aip import JwksCache
        from agentscore_commerce.identity.fastapi import AipGate, get_verified_ait

        app = FastAPI()
        # AgentScore's own issuer is always trusted; add external IdPs here.
        gate = AipGate(jwks=JwksCache(trusted_issuers=["https://issuer.example"]))

        @app.post("/checkout", dependencies=[Depends(gate)])
        async def checkout(ait = Depends(get_verified_ait)):
            return {"buyer": ait.payload.identity.email}
    """

    def __init__(
        self,
        *,
        jwks: JwksCache,
        now: float | None = None,
        max_skew_seconds: float | None = None,
        require_trust_level: TrustLevel | None = None,
        require_amr: list[str] | None = None,
        required_claims: list[str] | None = None,
        trusted_issuers: list[str] | None = None,
        on_denied: Callable[
            [Request, AipErrorBody],
            tuple[dict[str, Any], int] | tuple[dict[str, Any], int, dict[str, str]],
        ]
        | None = None,
    ) -> None:
        self._opts = AipGateOptions(
            jwks=jwks,
            now=now,
            max_skew_seconds=max_skew_seconds,
            require_trust_level=require_trust_level,
            require_amr=require_amr,
            required_claims=required_claims,
            trusted_issuers=trusted_issuers,
        )
        self._on_denied = on_denied

    def _deny(self, request: Request, body: AipErrorBody) -> NoReturn:
        headers: dict[str, str] | None = None
        media_type: str | None = None
        if self._on_denied is not None:
            result = self._on_denied(request, body)
            if len(result) == 3:
                resp_body, status, headers = cast("tuple[dict, int, dict[str, str]]", result)
            else:
                resp_body, status = cast("tuple[dict, int]", result)
        else:
            resp_body, status = body, int(body.get("status", 401))
            media_type = "application/problem+json"
        raise _GateDenialError(resp_body, status, headers, media_type)

    async def __call__(self, request: Request) -> None:
        # Wire the flat-denial exception handler onto the app on first run (Depends(gate)
        # never hands us the app at construction time). Idempotent + per-app.
        _install_gate_denial_handler(request)
        evaluation = await evaluate_aip_request(request, self._opts)
        if not evaluation.ok:
            self._deny(request, evaluation.body or build_aip_error_body("malformed_token"))
            return
        setattr(request.state, AIT_STATE_KEY, evaluation.ait)


class ConditionalAipGate:
    """Wrap :class:`AipGate` to verify only when an ``Agent-Identity`` header is present.

    Requests without the header flow through unauthenticated (e.g. so the route can fall
    back to the opaque-token gate or emit its own challenge); requests that DO carry the
    header must pass full verification.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._inner = AipGate(**kwargs)

    async def __call__(self, request: Request) -> None:
        if not has_agent_identity_header(request):
            return
        await self._inner(request)


def get_verified_ait(request: Request) -> VerifiedAit | None:
    """FastAPI dependency that returns the verified AIT attached by :class:`AipGate`.

    Returns ``None`` when the route wasn't AIP-gated or the conditional gate let an
    unauthenticated request through.

    Usage::

        @app.post("/checkout", dependencies=[Depends(gate)])
        async def checkout(ait = Depends(get_verified_ait)):
            ...
    """
    return getattr(request.state, AIT_STATE_KEY, None)
