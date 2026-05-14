"""Flask integration for trust-gating requests using AgentScore."""

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
    from collections.abc import Callable

    from flask import Flask, Request, Response

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"

ASSESS_STATE_KEY = "agentscore"

__all__ = [
    "FIXABLE_DENIAL_REASONS",
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
    base_url: str = "https://api.agentscore.sh",
    chain: str | None = None,
    user_agent: str | None = None,
    extract_identity: Callable[[Request], AgentIdentity | None] | None = None,
    extract_chain: Callable[[Request], str | None] | None = None,
    on_denied: Callable[[Request, DenialReason], tuple[dict[str, Any], int]] | None = None,
    create_session_on_missing: CreateSessionOnMissing | None = None,
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
    )
    _resolve_identity = extract_identity or _default_extract_identity
    _extract_chain = extract_chain or _default_extract_chain
    _on_denied = on_denied or _default_on_denied

    def _deny(reason: DenialReason) -> tuple[Response, int]:
        try:
            body, status = _on_denied(flask_request, reason)
        except (TypeError, ValueError) as exc:
            msg = "on_denied must return a (dict, int) tuple, e.g. ({'error': 'denied'}, 403)"
            raise TypeError(msg) from exc
        return jsonify(body), status

    def _mark_degraded(infra_reason: str) -> None:
        """Stamp the gate state on ``g._agentscore_gate`` as fail-open'd."""
        apply_degraded(getattr(g, "_agentscore_gate", None), infra_reason)

    @app.before_request
    def _agentscore_check() -> Response | tuple[Response, int] | None:
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
            denial_reason = build_missing_identity_reason()
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

            if result.allow:
                g.agentscore = result.raw
                if result.quota is not None:
                    state = getattr(g, "_agentscore_gate", None)
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
    """
    from flask import g

    try:
        state = getattr(g, "_agentscore_gate", None)
    except RuntimeError:
        return None
    if not state or not state.get("wallet_address"):
        return None
    client = state.get("client")
    if client is None:
        return None
    return client.get_signer_verdict(state["wallet_address"])


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
