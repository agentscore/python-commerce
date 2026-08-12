"""Flask integration for trust-gating requests using AgentScore."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import httpx

from agentscore_commerce.aip.gate import (
    AipGateOptions,
    build_aip_error_body,
    evaluate_aip_parts,
)
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
from agentscore_commerce.identity.sessions import CreateSessionOnMissing, try_create_session_denial_reason_sync
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
    from collections.abc import Callable, Coroutine

    from flask import Flask, Request, Response

    from agentscore_commerce.aip.gate import AipErrorBody, AipGateEvaluation
    from agentscore_commerce.aip.jwks import JwksCache
    from agentscore_commerce.aip.request import VerifyContextParts
    from agentscore_commerce.aip.types import TrustLevel
    from agentscore_commerce.aip.verify import VerifiedAit

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"

ASSESS_STATE_KEY = "agentscore"

__all__ = [
    "agentscore_gate",
    "aip_gate",
    "capture_wallet",
    "conditional_agentscore_gate",
    "conditional_aip_gate",
    "get_agentscore_data",
    "get_gate_degraded_state",
    "get_gate_quota_info",
    "get_signer_verdict",
    "get_verified_ait",
]


def _run_aip_sync(coro: Coroutine[Any, Any, AipGateEvaluation]) -> AipGateEvaluation:
    """Run the async AIP evaluation from Flask's sync ``before_request``.

    Flask's WSGI request path is synchronous but :func:`evaluate_aip_parts` is a coroutine.
    The default :class:`~agentscore_commerce.aip.jwks.JwksCache` fetcher opens a throwaway
    ``httpx.AsyncClient`` per JWKS fetch (no loop affinity) and the cache stores plain dicts, so
    a fresh ``asyncio.run`` per request is safe. When called from inside a running loop
    (async-Flask on ASGI), fall back to a dedicated thread so we never re-enter the loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def get_agentscore_data() -> dict[str, Any] | None:
    """Return the `/v1/assess` response the gate stashed on Flask's request-scoped ``g``.

    Returns ``None`` outside a gated request, when identity was missing, or when the gate
    short-circuited with a denial.
    """
    from flask import g

    return getattr(g, ASSESS_STATE_KEY, None)


def get_gate_degraded_state() -> dict[str, Any]:
    """Return whether the gate fail-open'd due to AgentScore-side infra failure.

    Returns ``{"degraded": False}`` for normal allows; ``{"degraded": True,
    "infra_reason": "quota_exceeded" | "api_error" | "network_timeout"}`` when bypassed.
    Only set when ``fail_open=True`` AND the failure was infra-shape.
    """
    from flask import g

    state = getattr(g, "_agentscore_gate", None)
    if isinstance(state, dict) and state.get("degraded"):
        return {"degraded": True, "infra_reason": state.get("infra_reason")}
    return {"degraded": False}


def get_gate_quota_info() -> GateQuotaInfo | None:
    """Read AgentScore assess quota observability for this request.

    Captured from ``X-Quota-*`` response headers on this request's gate evaluate.
    Returns ``None`` when the request was a fail-open pass-through or when the API
    didn't emit quota headers.
    """
    from flask import g

    state = getattr(g, "_agentscore_gate", None)
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
    app: Flask,
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
    condition: Callable[[Request], bool] | None = None,
    aip_trusted_issuers: list[str] | None = None,
) -> None:
    """Register AgentScore gate as a Flask before_request handler.

    Usage::

        from flask import Flask
        from agentscore_commerce.identity.flask import agentscore_gate

        app = Flask(__name__)
        agentscore_gate(app, api_key="ask_...", require_kyc=True)
    """
    from flask import g, jsonify
    from flask import request as flask_request

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
        aip_trusted_issuers=aip_trusted_issuers,
    )
    _resolve_identity = extract_identity or _default_extract_identity
    _extract_chain = extract_chain or _default_extract_chain
    _on_denied = on_denied or _default_on_denied

    def _deny(reason: DenialReason) -> tuple[Response, int]:
        result = _on_denied(flask_request, reason)
        if not isinstance(result, tuple) or len(result) not in (2, 3):
            msg = "on_denied must return a (dict, int) or (dict, int, dict) tuple, e.g. ({'error': 'denied'}, 403)"
            raise TypeError(msg)
        headers: dict[str, str] = {}
        if len(result) == 3:
            body, status, headers = cast("tuple[dict, int, dict[str, str]]", result)
        else:
            body, status = cast("tuple[dict, int]", result)
        response = jsonify(body)
        for k, v in headers.items():
            response.headers[k] = v
        return response, status

    def _mark_degraded(infra_reason: str) -> None:
        """Stamp the gate state on ``g._agentscore_gate`` as fail-open'd."""
        apply_degraded(getattr(g, "_agentscore_gate", None), infra_reason)

    @app.before_request
    def _agentscore_check() -> Response | tuple[Response, int] | None:
        if condition is not None and not condition(flask_request):
            return None
        identity = _resolve_identity(flask_request)
        # Stash state so capture_wallet() can look up operator_token + client after the handler.
        g._agentscore_gate = {
            "client": client,
            "operator_token": identity.operator_token if identity else None,
            "wallet_address": identity.address if identity else None,
        }
        if not identity:
            if client.fail_open:
                return None
            denial_reason = build_missing_identity_reason(client.aip_trusted_issuers)
            if create_session_on_missing is not None:
                session_reason = try_create_session_denial_reason_sync(
                    create_session_on_missing,
                    client.user_agent,
                    flask_request,
                )
                if session_reason is not None:
                    denial_reason = session_reason
            return _deny(denial_reason)

        chain_override = _extract_chain(flask_request)

        signer_payload: dict[str, str] | None = None
        if identity.address:
            x402_header = read_x402_payment_header(dict(flask_request.headers))
            recovered = extract_payment_signer(x402_header)
            if recovered is not None:
                signer_payload = {"address": recovered.address, "network": recovered.network}

        try:
            result = client.check_identity(identity, chain_override, signer=signer_payload)

            # The pairwise account handle rides this same assess response. Stash it BEFORE
            # the allow/deny branch: it is identity rather than a verdict, so a merchant
            # recording a denial against the buyer needs it on the path where its handler
            # never runs.
            _handle_state = getattr(g, "_agentscore_gate", None)
            if isinstance(_handle_state, dict):
                _handle_state["operator_handle"] = client.project_operator_handle(result.raw)

            if result.allow:
                g.agentscore = result.raw
                state = getattr(g, "_agentscore_gate", None)
                if isinstance(state, dict):
                    if result.quota is not None:
                        state["quota"] = result.quota
                    # Request-scope the signer verdict (see fastapi.get_signer_verdict): stash the
                    # verdict projected from THIS request's raw response so a concurrent
                    # same-wallet request with a different signer can't race the shared-core slot.
                    if identity.address and signer_payload is not None:
                        state["signer_verdict"] = client.project_signer_verdict(result.raw, identity.address)
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
                session_reason = try_create_session_denial_reason_sync(
                    create_session_on_missing,
                    client.user_agent,
                    flask_request,
                )
                if session_reason is not None:
                    return _deny(session_reason)

            return _deny(
                DenialReason(
                    code="wallet_not_trusted",
                    decision=result.decision,
                    reasons=result.reasons,
                    verify_url=result.verify_url,
                ),
            )
        except PaymentRequiredError:
            if client.fail_open:
                return None
            return _deny(DenialReason(code="payment_required"))
        except TokenDeniedError as err:
            return _deny(build_token_denied_reason(err))
        except InvalidCredentialError:
            # Permanent — no auto-session, agent should switch tokens or restart.
            return _deny(build_invalid_credential_reason())
        except QuotaExceededError:
            if client.fail_open:
                _mark_degraded("quota_exceeded")
                return None
            return _deny(DenialReason(code="api_error", agent_instructions=QUOTA_EXCEEDED_INSTRUCTIONS))
        except httpx.TimeoutException:
            if client.fail_open:
                _mark_degraded("network_timeout")
                return None
            return _deny(DenialReason(code="api_error"))
        except TypeError:
            raise
        except Exception:
            if client.fail_open:
                _mark_degraded("api_error")
                return None
            return _deny(DenialReason(code="api_error"))


def get_signer_verdict() -> SignerVerdict | None:
    """Synchronous read of the cached signer verdicts for the current request.

    Reads gate state from Flask's ``g`` object. Returns ``None`` for operator-token-only
    requests, requests with no payment credential, or fail-open pass-throughs (no
    assess call). See :class:`SignerVerdict` for the verdict shape.

    Reads the request-scoped verdict stashed by the gate (projected from THIS request's
    assess response) — concurrency-safe against a sibling same-wallet request.
    """
    from flask import g

    try:
        state = getattr(g, "_agentscore_gate", None)
    except RuntimeError:
        return None
    if not isinstance(state, dict):
        return None
    return state.get("signer_verdict")


def capture_wallet(
    wallet_address: str,
    network: Network,
    idempotency_key: str | None = None,
) -> None:
    """Report a wallet that paid under the operator_token the Flask gate extracted on this request.

    Reads gate state from Flask's ``g`` object — must be called inside a request context after
    the gate's before_request handler ran. Fire-and-forget: no-ops silently if the request was
    wallet-authenticated (no operator_token) or the API call fails.

    Usage::

        @app.post("/purchase")
        def purchase():
            # ... run payment, recover signer wallet from the payload ...
            capture_wallet(signer, "evm", idempotency_key=payment_intent_id)
            return {"ok": True}
    """
    from flask import g

    # Accessing `g` outside a request context raises RuntimeError — treat as no-op so background
    # threads/workers that mistakenly import this helper don't crash user code.
    try:
        state = getattr(g, "_agentscore_gate", None)
    except RuntimeError:
        return
    if not state or not state.get("operator_token"):
        return
    state["client"].capture_wallet(
        state["operator_token"],
        wallet_address,
        network,
        idempotency_key=idempotency_key,
    )


def conditional_agentscore_gate(app: Flask, **kwargs: Any) -> None:
    """Register :func:`agentscore_gate` to fire only on settle legs.

    Discovery legs (no ``payment-signature`` / ``x-payment`` /
    ``Authorization: Payment``) flow through to the route handler
    unauthenticated; settle legs trigger the full gate.

    Accepts the same kwargs as :func:`agentscore_gate`; any ``condition`` kwarg
    passed in is replaced with the payment-header check.
    """
    from agentscore_commerce.payment.payment_header import has_payment_header

    kwargs["condition"] = has_payment_header
    agentscore_gate(app, **kwargs)


# ---------------------------------------------------------------------------
# AIP gate (Agentic Identity Protocol) — verifies a key-bound Agent Identity Token (AIT)
# from a trusted IdP instead of an opaque operator token. Cryptographic identity only;
# merchants who want compliance enrichment feed the verified claims to ``/v1/assess``.
# Flask is WSGI (no request object the async verifier accepts directly), so the gate builds
# raw request parts (method + url + header map) and runs the async ``evaluate_aip_parts`` on a
# private event loop. ``get_verified_ait`` reads the token off Flask's request-scoped ``g``.
# ---------------------------------------------------------------------------

AIT_STATE_KEY = "_agentscore_ait"


def get_verified_ait() -> VerifiedAit | None:
    """Return the verified AIT the gate stashed on Flask's request-scoped ``g``.

    Returns ``None`` outside a gated request, or when the conditional gate let an
    unauthenticated request through.
    """
    from flask import g

    try:
        return getattr(g, AIT_STATE_KEY, None)
    except RuntimeError:
        return None


def aip_gate(
    app: Flask,
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
    condition: Callable[[Request], bool] | None = None,
) -> None:
    """Register an AIP gate as a Flask ``before_request`` handler.

    Verifies the IdP signature + RFC 9421 proof-of-possession + expiry + trust offline against
    the issuer's published JWKS (no API round trip). On a verify/trust failure it returns the
    RFC 9457 ``application/problem+json`` body; on success it stashes the verified token on ``g``
    for :func:`get_verified_ait`.

    Usage::

        from flask import Flask
        from agentscore_commerce.aip import JwksCache
        from agentscore_commerce.identity.flask import aip_gate

        app = Flask(__name__)
        aip_gate(app, jwks=JwksCache(trusted_issuers=["https://issuer.example"]))
    """
    from flask import g, jsonify
    from flask import request as flask_request

    opts = AipGateOptions(
        jwks=jwks,
        now=now,
        max_skew_seconds=max_skew_seconds,
        require_trust_level=require_trust_level,
        require_amr=require_amr,
        required_claims=required_claims,
        trusted_issuers=trusted_issuers,
    )

    def _deny(body: AipErrorBody) -> tuple[Response, int]:
        if on_denied is not None:
            result = on_denied(flask_request, body)
            headers: dict[str, str] = {}
            if len(result) == 3:
                resp_body, status, headers = cast("tuple[dict, int, dict[str, str]]", result)
            else:
                resp_body, status = cast("tuple[dict, int]", result)
            response = jsonify(resp_body)
            for k, v in headers.items():
                response.headers[k] = v
            return response, status
        response = jsonify(body)
        response.headers["Content-Type"] = "application/problem+json"
        return response, int(body.get("status", 401))

    @app.before_request
    def _aip_check() -> Response | tuple[Response, int] | None:
        if condition is not None and not condition(flask_request):
            return None
        parts: VerifyContextParts = {
            "method": flask_request.method,
            "url": flask_request.url,
            "headers": dict(flask_request.headers),
        }
        evaluation = _run_aip_sync(evaluate_aip_parts(parts, opts))
        if not evaluation.ok:
            return _deny(evaluation.body or build_aip_error_body("malformed_token"))
        g._agentscore_ait = evaluation.ait
        return None


def conditional_aip_gate(app: Flask, **kwargs: Any) -> None:
    """Register :func:`aip_gate` to verify only when an ``Agent-Identity`` header is present.

    Requests without the header flow through unauthenticated; requests that carry one must
    pass full verification. Accepts the same kwargs as :func:`aip_gate`.
    """
    from agentscore_commerce.aip.request import has_agent_identity_header_parts

    def _has_header(request: Request) -> bool:
        return has_agent_identity_header_parts(dict(request.headers))

    kwargs["condition"] = _has_header
    aip_gate(app, **kwargs)


def get_operator_handle() -> str | None:
    """Read the stable pairwise operator handle for the account behind this request's token.

    This is what durable merchant state (prepaid balances first) should key on: it survives
    the token rotating, expiring or being revoked, whereas anything keyed on the token
    instance is stranded every time one rotates.

    Synchronous and free. The handle rides the gate's existing ``/v1/assess`` call, so
    reading it costs no extra round trip and nothing extra against the merchant's quota.

    Returns ``None`` when the gate did not run, when no operator token was presented (wallet
    or AIT paths), or when the API has no handle salt configured. Available on denied
    requests too, so a merchant recording a denial against a buyer can still key it.
    """
    from flask import g

    try:
        state = getattr(g, "_agentscore_gate", None)
    except RuntimeError:
        # No application context (called outside a request). Same posture as the sibling
        # accessor: absent state reads as no handle, never as an error.
        return None
    if not isinstance(state, dict):
        return None
    return state.get("operator_handle")
