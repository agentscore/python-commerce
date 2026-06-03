"""Django middleware for trust-gating requests using AgentScore."""

from __future__ import annotations

from typing import Any

import httpx
from django.http import HttpRequest, JsonResponse

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


def _mark_degraded_django(request: HttpRequest, infra_reason: str) -> None:
    """Stamp the gate state on a Django request as fail-open'd."""
    apply_degraded(getattr(request, "_agentscore_gate", None), infra_reason)


DEFAULT_ADDRESS_HEADER = "HTTP_X_WALLET_ADDRESS"
DEFAULT_TOKEN_HEADER = "HTTP_X_OPERATOR_TOKEN"

ASSESS_STATE_KEY = "agentscore"

__all__ = [
    "AgentScoreMiddleware",
    "ConditionalAgentScoreMiddleware",
    "capture_wallet",
    "get_agentscore_data",
    "get_gate_degraded_state",
    "get_gate_quota_info",
    "get_signer_verdict",
]


def get_agentscore_data(request: HttpRequest) -> dict[str, Any] | None:
    """Return the `/v1/assess` response the middleware stashed on the request.

    Returns ``None`` when identity was missing or the gate short-circuited with a
    denial.
    """
    return getattr(request, ASSESS_STATE_KEY, None)


def get_gate_degraded_state(request: HttpRequest) -> dict[str, Any]:
    """Return whether the gate fail-open'd due to AgentScore-side infra failure.

    Returns ``{"degraded": False}`` for normal allows; ``{"degraded": True,
    "infra_reason": "quota_exceeded" | "api_error" | "network_timeout"}`` when bypassed.
    """
    state = getattr(request, "_agentscore_gate", None)
    if isinstance(state, dict) and state.get("degraded"):
        return {"degraded": True, "infra_reason": state.get("infra_reason")}
    return {"degraded": False}


def get_gate_quota_info(request: HttpRequest) -> GateQuotaInfo | None:
    """Read AgentScore assess quota observability for this request.

    Captured from ``X-Quota-*`` response headers on this request's gate evaluate.
    """
    state = getattr(request, "_agentscore_gate", None)
    if isinstance(state, dict):
        quota = state.get("quota")
        if isinstance(quota, GateQuotaInfo):
            return quota
    return None


class AgentScoreMiddleware:
    """Django middleware that gates requests based on AgentScore wallet reputation.

    Usage in settings.py::

        MIDDLEWARE = [
            ...
            "agentscore_commerce.identity.django.AgentScoreMiddleware",
            ...
        ]

        AGENTSCORE_GATE = {
            "api_key": "ask_...",
            "require_kyc": True,
        }
    """

    def __init__(self, get_response: Any) -> None:
        from django.conf import settings

        config: dict[str, Any] = getattr(settings, "AGENTSCORE_GATE", {})

        self._client = AgentScoreCore(
            api_key=config.get("api_key", ""),
            require_kyc=config.get("require_kyc"),
            require_sanctions_clear=config.get("require_sanctions_clear"),
            min_age=config.get("min_age"),
            blocked_jurisdictions=config.get("blocked_jurisdictions"),
            allowed_jurisdictions=config.get("allowed_jurisdictions"),
            fail_open=config.get("fail_open", False),
            cache_seconds=config.get("cache_seconds", 300),
            base_url=config.get("base_url", "https://api.agentscore.com"),
            chain=config.get("chain"),
            user_agent=config.get("user_agent"),
        )
        self._extract_identity = config.get("extract_identity", self._default_extract_identity)
        self._extract_chain = config.get("extract_chain", self._default_extract_chain)
        self._on_denied = config.get("on_denied", self._default_on_denied)
        self._create_session_on_missing: CreateSessionOnMissing | None = config.get(
            "create_session_on_missing",
        )
        self._condition = config.get("condition")
        self.get_response = get_response

    @staticmethod
    def _default_extract_identity(request: HttpRequest) -> AgentIdentity | None:
        token = request.META.get(DEFAULT_TOKEN_HEADER)
        addr = request.META.get(DEFAULT_ADDRESS_HEADER)
        identity = AgentIdentity()
        if token and len(token) > 0:
            identity.operator_token = token
        if addr and len(addr) > 0:
            identity.address = addr
        if identity.operator_token or identity.address:
            return identity
        return None

    @staticmethod
    def _default_extract_chain(_request: HttpRequest) -> str | None:
        return None

    @staticmethod
    def _default_on_denied(_request: HttpRequest, reason: DenialReason) -> JsonResponse:
        return JsonResponse(denial_reason_to_body(reason), status=denial_reason_status(reason))

    def __call__(self, request: HttpRequest) -> Any:
        """Process the request."""
        if self._condition is not None and not self._condition(request):
            return self.get_response(request)
        identity = self._extract_identity(request)

        # Stash state so capture_wallet() can read operator_token + client after the view runs.
        setattr(  # noqa: B010 — dynamic attribute attach on HttpRequest
            request,
            "_agentscore_gate",
            {
                "client": self._client,
                "operator_token": identity.operator_token if identity else None,
                "wallet_address": identity.address if identity else None,
            },
        )

        if not identity:
            if self._client.fail_open:
                return self.get_response(request)
            if self._create_session_on_missing is not None:
                session_reason = try_create_session_denial_reason_sync(
                    self._create_session_on_missing,
                    self._client.user_agent,
                    request,
                )
                if session_reason is not None:
                    return self._on_denied(request, session_reason)
            return self._on_denied(request, build_missing_identity_reason())

        chain_override = self._extract_chain(request)

        signer_payload: dict[str, str] | None = None
        if identity.address:
            x402_header = read_x402_payment_header(dict(request.headers))
            recovered = extract_payment_signer(x402_header)
            if recovered is not None:
                signer_payload = {"address": recovered.address, "network": recovered.network}

        # Only check_identity is wrapped — get_response (which runs the downstream view) must
        # NOT be in the try, otherwise an exception in the user's view would be misclassified
        # as an AgentScore infra failure and (under fail_open) re-invoke their view.
        try:
            result = self._client.check_identity(identity, chain_override, signer=signer_payload)
        except PaymentRequiredError:
            if self._client.fail_open:
                return self.get_response(request)
            return self._on_denied(request, DenialReason(code="payment_required"))
        except TokenDeniedError as err:
            reason = build_token_denied_reason(err)
            return self._on_denied(request, reason)
        except InvalidCredentialError:
            # Permanent — no auto-session, agent should switch tokens or restart.
            return self._on_denied(request, build_invalid_credential_reason())
        except QuotaExceededError:
            if self._client.fail_open:
                _mark_degraded_django(request, "quota_exceeded")
                return self.get_response(request)
            return self._on_denied(
                request,
                DenialReason(code="api_error", agent_instructions=QUOTA_EXCEEDED_INSTRUCTIONS),
            )
        except httpx.TimeoutException:
            if self._client.fail_open:
                _mark_degraded_django(request, "network_timeout")
                return self.get_response(request)
            return self._on_denied(request, DenialReason(code="api_error"))
        except Exception:
            if self._client.fail_open:
                _mark_degraded_django(request, "api_error")
                return self.get_response(request)
            return self._on_denied(request, DenialReason(code="api_error"))

        if result.allow:
            setattr(request, "agentscore", result.raw)  # noqa: B010 — dynamic attribute attach on HttpRequest
            if result.quota is not None:
                state = getattr(request, "_agentscore_gate", None)
                if isinstance(state, dict):
                    state["quota"] = result.quota
            return self.get_response(request)

        # Fixable compliance denials (kyc_required, kyc_pending, kyc_failed) get the
        # same UX as missing_identity: the gate mints a fresh verification session,
        # the agent polls until status=verified, gets a fresh opc_..., and retries
        # with X-Operator-Token. Unfixable reasons (sanctions_flagged, age_insufficient,
        # jurisdiction_restricted) keep the bare wallet_not_trusted denial.
        # `jurisdiction_restricted` is unfixable: the API only emits it after KYC is
        # verified (the user's KYC'd country is in the blocked list — re-doing KYC
        # won't change the country).
        if is_fixable_denial(result.reasons) and self._create_session_on_missing is not None:
            session_reason = try_create_session_denial_reason_sync(
                self._create_session_on_missing,
                self._client.user_agent,
                request,
            )
            if session_reason is not None:
                return self._on_denied(request, session_reason)

        reason = DenialReason(
            code="wallet_not_trusted",
            decision=result.decision,
            reasons=result.reasons,
            verify_url=result.verify_url,
        )
        return self._on_denied(request, reason)


def get_signer_verdict(request: HttpRequest) -> SignerVerdict | None:
    """Synchronous read of the cached signer verdicts for the current request.

    Returns ``None`` for operator-token-only requests, for requests with no payment
    credential, or for fail-open pass-throughs (no assess call).
    """
    state = getattr(request, "_agentscore_gate", None)
    if not state or not state.get("wallet_address"):
        return None
    client = state.get("client")
    if client is None:
        return None
    return client.get_signer_verdict(state["wallet_address"])


def capture_wallet(
    request: HttpRequest,
    wallet_address: str,
    network: Network,
    idempotency_key: str | None = None,
) -> None:
    """Report a wallet that paid under the operator_token the Django gate extracted on this request.

    Fire-and-forget: no-ops silently if the gate didn't run, the request was wallet-authenticated
    (no operator_token to associate), or the API call fails.

    Usage::

        def purchase(request):
            # ... run payment, recover signer wallet from the payload ...
            capture_wallet(request, signer, "evm", idempotency_key=payment_intent_id)
            return JsonResponse({"ok": True})
    """
    state = getattr(request, "_agentscore_gate", None)
    if not state or not state.get("operator_token"):
        return
    state["client"].capture_wallet(
        state["operator_token"],
        wallet_address,
        network,
        idempotency_key=idempotency_key,
    )


class ConditionalAgentScoreMiddleware(AgentScoreMiddleware):
    """Django middleware variant that only fires when payment headers are attached.

    Discovery legs flow through; settle legs trigger the full gate.

    Settings shape is identical to :class:`AgentScoreMiddleware` — the
    ``AGENTSCORE_GATE`` dict's ``condition`` key is overwritten with the
    payment-header check.
    """

    def __init__(self, get_response: Any) -> None:
        from agentscore_commerce.payment.payment_header import has_payment_header

        super().__init__(get_response)
        self._condition = has_payment_header
