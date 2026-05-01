"""FastAPI native adapter for trust-gating routes using AgentScore.

The adapter plugs into FastAPI's dependency-injection system. Unlike the generic ASGI
middleware (which gates every request), this adapter lets you scope gating to specific
routes via ``dependencies=[Depends(gate)]`` and inject the assess result with
``Depends(get_assess_data)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

import httpx
from starlette.requests import Request  # noqa: TC002 - runtime import required for FastAPI DI

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
    from collections.abc import Callable

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"
GATE_STATE_KEY = "__agentscore_gate"
ASSESS_STATE_KEY = "agentscore"


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
    Uses FastAPI's dependency-injection system — when the dependency raises
    :class:`fastapi.HTTPException`, the route body is skipped and the error response
    is returned to the client.

    Usage::

        from fastapi import Depends, FastAPI
        from agentscore_commerce.identity.fastapi import AgentScoreGate, get_assess_data

        app = FastAPI()
        gate = AgentScoreGate(api_key="ask_...", require_kyc=True, min_age=21)

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase(assess = Depends(get_assess_data)):
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
        base_url: str = "https://api.agentscore.sh",
        chain: str | None = None,
        user_agent: str | None = None,
        extract_identity: Callable[[Request], AgentIdentity | None] | None = None,
        extract_chain: Callable[[Request], str | None] | None = None,
        on_denied: Callable[[Request, DenialReason], tuple[dict[str, Any], int]] | None = None,
        create_session_on_missing: CreateSessionOnMissing | None = None,
    ) -> None:
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
        self._extract_chain = extract_chain or _default_extract_chain
        self._on_denied = on_denied
        self._create_session_on_missing = create_session_on_missing

    def _deny(self, request: Request, reason: DenialReason) -> NoReturn:
        from fastapi import HTTPException

        if self._on_denied is not None:
            body, status = self._on_denied(request, reason)
        else:
            body, status = _build_denial_body(reason), denial_reason_status(reason)
        raise HTTPException(status_code=status, detail=body)

    async def __call__(self, request: Request) -> None:
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
            self._deny(request, build_missing_identity_reason())

        chain_override = self._extract_chain(request)

        try:
            result = await self._client.acheck_identity(identity, chain_override)
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
            return
        except httpx.TimeoutException:
            if self._client.fail_open:
                _mark_degraded(request, "network_timeout")
                return
            self._deny(request, DenialReason(code="api_error"))
            return
        except Exception:
            if self._client.fail_open:
                _mark_degraded(request, "api_error")
                return
            self._deny(request, DenialReason(code="api_error"))
            return

        if result.allow:
            setattr(request.state, ASSESS_STATE_KEY, result.raw)
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


def get_assess_data(request: Request) -> dict[str, Any] | None:
    """FastAPI dependency that returns the raw ``/v1/assess`` response for the current request.

    Returns ``None`` when the gate was bypassed via ``fail_open`` or the route wasn't gated.
    Usage::

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase(assess = Depends(get_assess_data)):
            ...
    """
    return getattr(request.state, ASSESS_STATE_KEY, None)


async def verify_wallet_signer_match(
    request: Request,
    signer: str | None,
    network: Network = "evm",
) -> VerifyWalletSignerResult:
    """Verify payment signer matches claimed X-Wallet-Address.

    No-ops when operator-token-authenticated or when both headers were sent. See
    :func:`agentscore_commerce.identity.middleware.verify_wallet_signer_match` for the full contract.
    """
    state = getattr(request.state, GATE_STATE_KEY, None)
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
    """Report a wallet that paid under the operator_token the FastAPI gate extracted on this request.

    Fire-and-forget: no-ops silently if the gate didn't run, the request was wallet-authenticated
    (no operator_token to associate), or the API call fails.

    Usage::

        @app.post("/purchase", dependencies=[Depends(gate)])
        async def purchase(request: Request, assess = Depends(get_assess_data)):
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
