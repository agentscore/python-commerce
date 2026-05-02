"""Tests for the shared session-creation helpers in agentscore_commerce.identity.sessions."""

from __future__ import annotations

import httpx
import pytest
import respx

from agentscore_commerce.identity.sessions import (
    CreateSessionOnMissing,
    try_create_session_denial_reason,
    try_create_session_denial_reason_sync,
)

SESSIONS_URL = "https://api.agentscore.sh/v1/sessions"
# API emits structured next_steps on /v1/sessions success. The gate stringifies it into
# agent_instructions for consistent rendering with every other denial body.
SESSION_RESPONSE = {
    "session_id": "sess_abc",
    "verify_url": "https://agentscore.sh/verify/sess_abc",
    "poll_secret": "ps_secret",
    "next_steps": {
        "action": "deliver_verify_url_and_poll",
        "user_message": "please verify",
    },
}


class TestSyncHelper:
    @respx.mock
    def test_returns_denial_reason_on_success(self):
        import json

        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        reason = try_create_session_denial_reason_sync(
            CreateSessionOnMissing(api_key="ask_test"),
            user_agent="agentscore-commerce/1.0",
        )
        assert reason is not None
        assert reason.code == "identity_verification_required"
        assert reason.session_id == "sess_abc"
        assert reason.verify_url == "https://agentscore.sh/verify/sess_abc"
        assert reason.poll_secret == "ps_secret"
        # agent_instructions is the JSON-stringified next_steps from the API.
        assert reason.agent_instructions is not None
        parsed = json.loads(reason.agent_instructions)
        assert parsed["action"] == "deliver_verify_url_and_poll"
        assert parsed["user_message"] == "please verify"

    @respx.mock
    def test_returns_none_on_server_error(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(500, text="oops"))
        reason = try_create_session_denial_reason_sync(
            CreateSessionOnMissing(api_key="ask_test"),
            user_agent="agentscore-commerce/1.0",
        )
        assert reason is None

    @respx.mock
    def test_returns_none_on_network_error(self):
        respx.post(SESSIONS_URL).mock(side_effect=httpx.ConnectError("boom"))
        reason = try_create_session_denial_reason_sync(
            CreateSessionOnMissing(api_key="ask_test"),
            user_agent="agentscore-commerce/1.0",
        )
        assert reason is None

    @respx.mock
    def test_forwards_context_and_product_name(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        try_create_session_denial_reason_sync(
            CreateSessionOnMissing(
                api_key="ask_test",
                context="purchase_flow",
                product_name="Martin Estate",
            ),
            user_agent="agentscore-commerce/1.0",
        )
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["context"] == "purchase_flow"
        assert body["product_name"] == "Martin Estate"

    @respx.mock
    def test_omits_context_and_product_name_when_not_provided(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        try_create_session_denial_reason_sync(
            CreateSessionOnMissing(api_key="ask_test"),
            user_agent="agentscore-commerce/1.0",
        )
        import json

        body = json.loads(route.calls[0].request.content)
        assert "context" not in body
        assert "product_name" not in body

    @respx.mock
    def test_forwards_api_key_and_user_agent(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        try_create_session_denial_reason_sync(
            CreateSessionOnMissing(api_key="ask_my_key"),
            user_agent="myapp/1.0",
        )
        headers = route.calls[0].request.headers
        assert headers["X-API-Key"] == "ask_my_key"
        # SDK appends its own UA segment to the chain: `<merchant> (agentscore-py/<v>)`
        assert headers["User-Agent"].startswith("myapp/1.0")
        assert "agentscore-py/" in headers["User-Agent"]

    @respx.mock
    def test_respects_custom_base_url(self):
        custom_url = "https://staging.agentscore.sh/v1/sessions"
        route = respx.post(custom_url).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        try_create_session_denial_reason_sync(
            CreateSessionOnMissing(api_key="ask_test", base_url="https://staging.agentscore.sh"),
            user_agent="agentscore-commerce/1.0",
        )
        assert route.called


class TestAsyncHelper:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_denial_reason_on_success(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        reason = await try_create_session_denial_reason(
            CreateSessionOnMissing(api_key="ask_test"),
            user_agent="agentscore-commerce/1.0",
        )
        assert reason is not None
        assert reason.session_id == "sess_abc"

    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_none_on_network_error(self):
        respx.post(SESSIONS_URL).mock(side_effect=httpx.ConnectError("boom"))
        reason = await try_create_session_denial_reason(
            CreateSessionOnMissing(api_key="ask_test"),
            user_agent="agentscore-commerce/1.0",
        )
        assert reason is None


# ---------------------------------------------------------------------------
# get_session_options hook
# ---------------------------------------------------------------------------


class TestGetSessionOptionsSync:
    @respx.mock
    def test_dynamic_product_name_overrides_static(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            product_name="static_name",
            get_session_options=lambda _ctx: {"product_name": "dynamic_name"},
        )
        try_create_session_denial_reason_sync(cfg, user_agent="ua", ctx={"body": "stub"})
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["product_name"] == "dynamic_name"

    @respx.mock
    def test_dynamic_context_overrides_static(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            context="static_context",
            get_session_options=lambda _ctx: {"context": "dynamic_context"},
        )
        try_create_session_denial_reason_sync(cfg, user_agent="ua", ctx={"body": "stub"})
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["context"] == "dynamic_context"

    @respx.mock
    def test_hook_not_called_when_ctx_none(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        def hook(_ctx):
            return {"product_name": "called"}

        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            product_name="static",
            get_session_options=hook,
        )
        # ctx defaults to None → hook is skipped, static value used
        try_create_session_denial_reason_sync(cfg, user_agent="ua")
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["product_name"] == "static"

    @respx.mock
    def test_hook_error_swallowed_uses_static(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        def raising(_ctx):
            raise RuntimeError("oops")

        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            product_name="static",
            get_session_options=raising,
        )
        reason = try_create_session_denial_reason_sync(cfg, user_agent="ua", ctx={"x": 1})
        # Hook threw → fall back to static, session still created
        assert reason is not None
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["product_name"] == "static"

    @respx.mock
    def test_async_hook_in_sync_adapter_skipped(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        async def async_hook(_ctx):
            return {"product_name": "async"}

        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            product_name="static",
            get_session_options=async_hook,
        )
        reason = try_create_session_denial_reason_sync(cfg, user_agent="ua", ctx={"x": 1})
        # Sync adapter can't await → skip hook, use static
        assert reason is not None
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["product_name"] == "static"


class TestGetSessionOptionsAsync:
    @pytest.mark.asyncio
    @respx.mock
    async def test_sync_hook(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            get_session_options=lambda _ctx: {"product_name": "dynamic"},
        )
        await try_create_session_denial_reason(cfg, user_agent="ua", ctx={"x": 1})
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["product_name"] == "dynamic"

    @pytest.mark.asyncio
    @respx.mock
    async def test_async_hook(self):
        route = respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        async def async_hook(_ctx):
            return {"product_name": "async_dynamic"}

        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            get_session_options=async_hook,
        )
        await try_create_session_denial_reason(cfg, user_agent="ua", ctx={"x": 1})
        import json

        body = json.loads(route.calls[0].request.content)
        assert body["product_name"] == "async_dynamic"

    @pytest.mark.asyncio
    @respx.mock
    async def test_hook_error_swallowed(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        def raising(_ctx):
            raise RuntimeError("boom")

        cfg = CreateSessionOnMissing(api_key="ask_test", get_session_options=raising)
        reason = await try_create_session_denial_reason(cfg, user_agent="ua", ctx={"x": 1})
        assert reason is not None  # session still created


# ---------------------------------------------------------------------------
# on_before_session hook
# ---------------------------------------------------------------------------


class TestOnBeforeSessionSync:
    @respx.mock
    def test_return_dict_merged_into_extra(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            on_before_session=lambda _ctx, _session: {"order_id": "ord-123"},
        )
        reason = try_create_session_denial_reason_sync(cfg, user_agent="ua", ctx={"x": 1})
        assert reason is not None
        assert reason.extra == {"order_id": "ord-123"}

    @respx.mock
    def test_hook_receives_session_metadata(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        captured = {}

        def hook(_ctx, session):
            captured.update(session)
            return {"order_id": "ord-1"}

        cfg = CreateSessionOnMissing(api_key="ask_test", on_before_session=hook)
        try_create_session_denial_reason_sync(cfg, user_agent="ua", ctx={"x": 1})
        assert captured["session_id"] == "sess_abc"
        assert captured["verify_url"] == "https://agentscore.sh/verify/sess_abc"

    @respx.mock
    def test_hook_error_swallowed_session_still_returned(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        def raising(_ctx, _session):
            raise RuntimeError("db down")

        cfg = CreateSessionOnMissing(api_key="ask_test", on_before_session=raising)
        reason = try_create_session_denial_reason_sync(cfg, user_agent="ua", ctx={"x": 1})
        assert reason is not None
        assert reason.extra is None  # hook failed → no extra


class TestOnBeforeSessionAsync:
    @pytest.mark.asyncio
    @respx.mock
    async def test_async_hook_return_merged(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))

        async def hook(_ctx, _session):
            return {"order_id": "ord-async"}

        cfg = CreateSessionOnMissing(api_key="ask_test", on_before_session=hook)
        reason = await try_create_session_denial_reason(cfg, user_agent="ua", ctx={"x": 1})
        assert reason is not None
        assert reason.extra == {"order_id": "ord-async"}

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_dict_return_ignored(self):
        respx.post(SESSIONS_URL).mock(return_value=httpx.Response(200, json=SESSION_RESPONSE))
        cfg = CreateSessionOnMissing(
            api_key="ask_test",
            on_before_session=lambda _ctx, _session: "not a dict",
        )
        reason = await try_create_session_denial_reason(cfg, user_agent="ua", ctx={"x": 1})
        assert reason is not None
        assert reason.extra is None


# ── _apply_dynamic_options edge cases ───────────────────────────────────────


def test_apply_dynamic_options_passes_through_non_dict() -> None:
    from agentscore_commerce.identity.sessions import _apply_dynamic_options

    body: dict[str, str] = {}
    assert _apply_dynamic_options(body, "not-a-dict") is body


def test_apply_dynamic_options_picks_js_style_product_name() -> None:
    from agentscore_commerce.identity.sessions import _apply_dynamic_options

    body: dict[str, str] = {}
    out = _apply_dynamic_options(body, {"productName": "Wine Store"})
    assert out["product_name"] == "Wine Store"
