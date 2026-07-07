"""Shared session-creation helper used by framework adapters."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from agentscore import AgentScore, AgentScoreError

from agentscore_commerce.identity.types import DenialReason, build_agent_memory_hint

logger = logging.getLogger("agentscore_gate")

# A hook can be sync or async. We call it, then await the result if it's a coroutine.
_Hookable = Any | Awaitable[Any]


@dataclass
class CreateSessionOnMissing:
    """Config for auto-creating verification sessions on missing identity.

    When supplied to any framework adapter, missing-identity requests trigger a
    ``POST /v1/sessions`` call and receive a 403 with verify_url + poll instructions
    instead of a bare ``missing_identity`` denial.

    For per-request session context (e.g. the specific product the agent was trying
    to buy), pass a ``get_session_options`` callback that returns a dict with
    ``context`` and/or ``product_name`` keys; its return is merged over the static
    ``context`` / ``product_name`` fields below.

    ``on_before_session`` is a side-effect hook that runs after the session is minted
    but before the 403 is built. Use it to pre-create a reservation/draft/pending-order
    row in your DB so agents can resume via a merchant-specific id. Return value is
    merged into ``DenialReason.extra`` so custom ``on_denied`` handlers can include
    merchant-specific fields (e.g. ``order_id``) in the 403 response.

    Both hooks can be sync or ``async def``. Hook errors are logged and swallowed — a
    failing side effect should not block the 403 from reaching the agent.
    """

    api_key: str
    base_url: str = "https://api.agentscore.com"
    context: str | None = None
    product_name: str | None = None
    # Per-request override of context / product_name. Receives the framework request
    # object; returns a dict with optional "context" and/or "product_name" keys.
    get_session_options: Callable[[Any], _Hookable] | None = None
    # Side-effect hook that runs after session creation. Return dict is merged into
    # DenialReason.extra so custom on_denied handlers can include merchant-specific
    # fields (e.g. order_id) in the 403.
    on_before_session: Callable[[Any, dict[str, Any]], _Hookable] | None = None


async def _maybe_await(value: _Hookable) -> Any:
    """Await if coroutine, else return as-is. Lets hooks be sync or async."""
    if inspect.iscoroutine(value):
        return await value
    return value


def _build_sdk(cfg: CreateSessionOnMissing, user_agent: str) -> AgentScore:
    return AgentScore(api_key=cfg.api_key, base_url=cfg.base_url, user_agent=user_agent)


def _apply_dynamic_options(body: dict[str, Any], dynamic: Any) -> dict[str, Any]:
    """Merge a per-request override dict over a base body.

    Non-dict ``dynamic`` is treated as a no-op so hooks may return ``None`` without
    crashing the path.
    """
    if not isinstance(dynamic, dict):
        return body
    if dynamic.get("context") is not None:
        body["context"] = dynamic["context"]
    if dynamic.get("product_name") is not None:
        body["product_name"] = dynamic["product_name"]
    # Accept JS-style camelCase "productName" too.
    if dynamic.get("productName") is not None:
        body["product_name"] = dynamic["productName"]
    return body


def _resolved_session_options(cfg: CreateSessionOnMissing, dynamic: Any) -> dict[str, Any]:
    """Merge static cfg fields with any dynamic per-request override dict."""
    options: dict[str, Any] = {}
    if cfg.context is not None:
        options["context"] = cfg.context
    if cfg.product_name is not None:
        options["product_name"] = cfg.product_name
    return _apply_dynamic_options(options, dynamic)


def _session_denial_reason(
    data: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> DenialReason | None:
    # Validate required fields before trusting the response. A misbehaving (or
    # mocked-wrong) API could 200 without session_id/poll_secret/verify_url, which
    # would propagate None into the 403 body and leave the agent stuck — treat that
    # as a session-create failure and let the caller fall back to missing_identity.
    if not (
        isinstance(data.get("session_id"), str)
        and isinstance(data.get("poll_secret"), str)
        and isinstance(data.get("verify_url"), str)
    ):
        logger.warning("/v1/sessions returned 200 without required fields — treating as failure")
        return None
    # The API emits structured ``next_steps`` on /v1/sessions success. Stringify it into
    # the gate's ``agent_instructions`` contract so every denial body surfaces the same
    # JSON-encoded {action, steps, user_message} envelope.
    next_steps = data.get("next_steps")
    agent_instructions = json.dumps(next_steps) if next_steps else None
    return DenialReason(
        code="identity_verification_required",
        verify_url=data["verify_url"],
        session_id=data["session_id"],
        poll_secret=data["poll_secret"],
        poll_url=data.get("poll_url"),
        agent_instructions=agent_instructions,
        agent_memory=build_agent_memory_hint(),
        extra=extra,
    )


def _session_metadata(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": data.get("session_id"),
        "verify_url": data.get("verify_url"),
        "poll_secret": data.get("poll_secret"),
        "poll_url": data.get("poll_url"),
        "expires_at": data.get("expires_at"),
    }


async def try_create_session_denial_reason(
    cfg: CreateSessionOnMissing,
    user_agent: str,
    ctx: Any = None,
) -> DenialReason | None:
    """Hit ``POST /v1/sessions`` and return a populated DenialReason, or None on failure.

    Async variant. Invokes ``cfg.get_session_options(ctx)`` and ``cfg.on_before_session(ctx, session)``
    if set — both may be sync or async.
    """
    try:
        dynamic: Any = None
        if cfg.get_session_options is not None and ctx is not None:
            try:
                dynamic = await _maybe_await(cfg.get_session_options(ctx))
            except Exception as err:
                logger.warning("get_session_options hook failed: %s", err)
                dynamic = None

        options = _resolved_session_options(cfg, dynamic)
        sdk = _build_sdk(cfg, user_agent)
        try:
            data = dict(await sdk.acreate_session(**options))
        except AgentScoreError:
            return None

        extra: dict[str, Any] | None = None
        if cfg.on_before_session is not None and ctx is not None:
            try:
                result = await _maybe_await(cfg.on_before_session(ctx, _session_metadata(data)))
                if isinstance(result, dict):
                    extra = result
            except Exception as err:
                logger.warning("on_before_session hook failed: %s", err)

        return _session_denial_reason(data, extra)
    except Exception:
        return None


def try_create_session_denial_reason_sync(
    cfg: CreateSessionOnMissing,
    user_agent: str,
    ctx: Any = None,
) -> DenialReason | None:
    """Synchronous variant of :func:`try_create_session_denial_reason` for Flask/Django.

    Hook callables MUST be sync (not ``async def``) — sync code can't await. If an
    async hook is passed in a sync adapter config, it's skipped with a warning.
    """
    try:
        dynamic: Any = None
        if cfg.get_session_options is not None and ctx is not None:
            try:
                hook_dynamic = cfg.get_session_options(ctx)
                if inspect.iscoroutine(hook_dynamic):
                    logger.warning("get_session_options returned a coroutine in a sync adapter — skipping")
                    hook_dynamic.close()
                else:
                    dynamic = hook_dynamic
            except Exception as err:
                logger.warning("get_session_options hook failed: %s", err)
                dynamic = None

        options = _resolved_session_options(cfg, dynamic)
        sdk = _build_sdk(cfg, user_agent)
        try:
            data = dict(sdk.create_session(**options))
        except AgentScoreError:
            return None

        extra: dict[str, Any] | None = None
        if cfg.on_before_session is not None and ctx is not None:
            try:
                hook_result: Any = cfg.on_before_session(ctx, _session_metadata(data))
                if inspect.iscoroutine(hook_result):
                    logger.warning("on_before_session returned a coroutine in a sync adapter — skipping")
                    hook_result.close()
                elif isinstance(hook_result, dict):
                    extra = cast("dict[str, Any]", hook_result)
            except Exception as err:
                logger.warning("on_before_session hook failed: %s", err)

        return _session_denial_reason(data, extra)
    except Exception:
        return None
