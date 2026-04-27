"""Sanic integration for trust-gating requests using AgentScore."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    from collections.abc import Callable

    from sanic import HTTPResponse, Request, Sanic

DEFAULT_ADDRESS_HEADER = "x-wallet-address"
DEFAULT_TOKEN_HEADER = "x-operator-token"
GATE_STATE_ATTR = "_agentscore_gate"
ASSESS_STATE_ATTR = "agentscore"

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
    "extract_payment_signer_address",
    "get_assess_data",
    "is_fixable_denial",
    "read_x402_payment_header",
    "verification_agent_instructions",
    "verify_wallet_signer_match",
]


def get_assess_data(request: Request) -> dict[str, Any] | None:
    """Return the `/v1/assess` response the middleware stashed on ``request.ctx``.

    Returns ``None`` when identity was missing or the gate short-circuited with a
    denial.
    """
    return getattr(request.ctx, ASSESS_STATE_ATTR, None)


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

        try:
            result = await client.acheck_identity(identity, chain_override)

            if result.allow:
                request.ctx.agentscore = result.raw
                return None

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
        except Exception:
            if client.fail_open:
                return None
            body, status = _on_denied(request, DenialReason(code="api_error"))
            return response.json(body, status=status)


async def verify_wallet_signer_match(
    request: Request,
    signer: str | None,
    network: Network = "evm",
) -> VerifyWalletSignerResult:
    """Verify payment signer matches claimed X-Wallet-Address.

    No-ops when operator-token-authenticated or when both headers were sent. See
    :func:`agentscore_commerce.identity.middleware.verify_wallet_signer_match` for the full contract.
    """
    state = getattr(request.ctx, GATE_STATE_ATTR, None)
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
