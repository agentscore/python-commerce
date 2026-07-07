"""Coverage for the seamless-merchant helpers shipped in the latest SDK additions:

* ``lazy_x402_server`` / ``lazy_mppx_server``; memoized async getters
* ``extract_signer_for_precheck``; one-call signer across x402 + mpp headers
* ``make_mppx_compose_hook``; canonical ``compose_mppx`` factory
* ``purchase_mode_note`` / ``build_agentscore_onboarding_steps`` /
  ``standard_endpoint_descriptions`` / ``build_success_next_steps``
* ``build_redemption_skill_md``
* The new validation_response_* framework variants + ``validation_envelope``
* Checkout framework adapters (handle_flask / handle_django / handle_aiohttp /
  handle_sanic); handle_fastapi is already exercised in test_checkout.py.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib as _contextlib
import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agentscore_commerce import (
    Checkout,
    CheckoutRequest,
    MppxComposeOutcome,
    SettleOutcome,
    validation_envelope,
    validation_response_aiohttp,
    validation_response_django,
    validation_response_fastapi,
    validation_response_flask,
    validation_response_sanic,
)
from agentscore_commerce.checkout_hooks import make_mppx_compose_hook
from agentscore_commerce.discovery import (
    PURCHASE_MODE_NOTES,
    build_agentscore_onboarding_steps,
    build_redemption_skill_md,
    build_success_next_steps,
    purchase_mode_note,
    standard_endpoint_descriptions,
)
from agentscore_commerce.payment import (
    PaymentSigner,
    TempoRailSpec,
    X402BaseRailSpec,
    extract_signer_for_precheck,
    lazy_mppx_server,
    lazy_x402_server,
)


def _req(headers: dict[str, str] | None = None) -> CheckoutRequest:
    return CheckoutRequest(
        method="POST",
        url="https://api.example/purchase",
        headers=headers or {},
        body={"item": "wine"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# lazy_x402_server / lazy_mppx_server
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lazy_x402_server_memoizes_single_instance() -> None:
    spec = X402BaseRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD", network="eip155:84532")
    sentinel = object()
    calls = 0

    async def _fake_create(*, facilitator: str, rails: list[str]) -> object:
        nonlocal calls
        calls += 1
        assert facilitator == "http"
        assert rails == ["x402-base-sepolia"]
        return sentinel

    with patch("agentscore_commerce.payment.lazy.create_x402_server", _fake_create):
        getter = lazy_x402_server(spec=spec)
        a, b = await asyncio.gather(getter(), getter())
    assert a is sentinel
    assert b is sentinel
    assert calls == 1


@pytest.mark.asyncio
async def test_lazy_x402_server_picks_coinbase_with_full_creds() -> None:
    spec = X402BaseRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD")

    async def _fake_create(*, facilitator: str, rails: list[str]) -> str:
        return f"{facilitator}:{rails[0]}"

    with patch("agentscore_commerce.payment.lazy.create_x402_server", _fake_create):
        getter = lazy_x402_server(
            spec=spec,
            cdp_api_key_id="k",
            cdp_api_key_secret="s",
        )
        out = await getter()
    assert out == "coinbase:x402-base-mainnet"


def test_lazy_x402_server_rejects_unknown_network() -> None:
    bad = X402BaseRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD")
    object.__setattr__(bad, "network", "eip155:1")
    with pytest.raises(ValueError, match=r"unsupported X402BaseRailSpec\.network"):
        lazy_x402_server(spec=bad)


@pytest.mark.asyncio
async def test_lazy_mppx_server_memoizes_single_instance() -> None:
    spec = TempoRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD")
    sentinel = object()
    calls = 0

    async def _fake_create(*, secret_key: str, rails: Any, realm: str | None) -> object:
        nonlocal calls
        calls += 1
        assert secret_key == "secret"
        assert "tempo" in rails
        assert realm == "test-realm"
        return sentinel

    with patch("agentscore_commerce.payment.lazy.create_mppx_server", _fake_create):
        getter = lazy_mppx_server(
            rails={"tempo": spec},
            secret_key="secret",
            realm="test-realm",
        )
        a, b = await asyncio.gather(getter(), getter())
    assert a is sentinel
    assert b is sentinel
    assert calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# extract_signer_for_precheck
# ─────────────────────────────────────────────────────────────────────────────


def _encode_x402_header(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_extract_signer_for_precheck_reads_x402_payment_signature() -> None:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": "eip155:84532",
        "payload": {
            "authorization": {
                "from": "0xAbC0000000000000000000000000000000000001",
            },
        },
    }
    headers = {"Payment-Signature": _encode_x402_header(payload)}
    signer = extract_signer_for_precheck(headers)
    assert signer == PaymentSigner(
        address="0xabc0000000000000000000000000000000000001",
        network="evm",
    )


def test_extract_signer_for_precheck_reads_x_payment_alias() -> None:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": "eip155:84532",
        "payload": {"authorization": {"from": "0xAbC0000000000000000000000000000000000002"}},
    }
    signer = extract_signer_for_precheck({"X-Payment": _encode_x402_header(payload)})
    assert signer is not None
    assert signer.address.endswith("002")


def test_extract_signer_for_precheck_no_headers_returns_none() -> None:
    assert extract_signer_for_precheck({}) is None
    assert extract_signer_for_precheck({"authorization": "Bearer not-a-payment"}) is None


def test_extract_signer_for_precheck_garbled_x402_falls_through() -> None:
    # Garbled x402 returns None, then we fall through to authorization (which is missing → None).
    assert extract_signer_for_precheck({"Payment-Signature": "!!!notbase64!!!"}) is None


def test_extract_signer_for_precheck_reads_mpp_authorization() -> None:
    """With no x402 header, the precheck falls through to the MPP Authorization: Payment path."""
    # {"source": "did:pkh:eip155:4217:0xABCDef1234567890123456789012345678901234"}
    auth = "Payment eyJzb3VyY2UiOiAiZGlkOnBraDplaXAxNTU6NDIxNzoweEFCQ0RlZjEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQifQ=="
    signer = extract_signer_for_precheck({"Authorization": auth})
    assert signer == PaymentSigner(address="0xabcdef1234567890123456789012345678901234", network="evm")


# ─────────────────────────────────────────────────────────────────────────────
# make_mppx_compose_hook
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_make_mppx_compose_hook_returns_402_when_no_pricing() -> None:
    async def _getter() -> Any:
        raise AssertionError("server should not be touched when pricing is None")

    hook = make_mppx_compose_hook(server_getter=_getter)

    class _Ctx:
        request = _req()
        pricing = None

    out = await hook(_Ctx())
    assert out.status == 402


@pytest.mark.asyncio
async def test_make_mppx_compose_hook_emits_challenge_headers_on_402() -> None:
    class _Challenge:
        def to_www_authenticate(self, realm: str) -> str:
            return f'Payment realm="{realm}"'

    class _Mpp:
        realm = "test-realm"

        async def charge(self, *, authorization: str | None, amount: str) -> _Challenge:
            assert authorization is None
            assert amount == "1.00"
            return _Challenge()

    async def _getter() -> _Mpp:
        return _Mpp()

    hook = make_mppx_compose_hook(server_getter=_getter)

    class _Pricing:
        amount_usd = 1.0

    class _Ctx:
        request = _req()
        pricing = _Pricing()

    out = await hook(_Ctx())
    assert out.status == 402
    assert out.headers == {"www-authenticate": 'Payment realm="test-realm"'}


@pytest.mark.asyncio
async def test_make_mppx_compose_hook_lifts_signer_from_did_pkh_eip155() -> None:
    class _Credential:
        source = "did:pkh:eip155:8453:0xABCD000000000000000000000000000000000003"

    class _Receipt:
        reference = "0xtx_hash"
        transaction = None

    class _Mpp:
        realm = "r"

        async def charge(self, *, authorization: str | None, amount: str) -> tuple:
            return (_Credential(), _Receipt())

    async def _getter() -> _Mpp:
        return _Mpp()

    hook = make_mppx_compose_hook(server_getter=_getter)

    class _Pricing:
        amount_usd = 0.1

    class _Ctx:
        request = _req({"authorization": "Payment somevalidcred"})
        pricing = _Pricing()

    out = await hook(_Ctx())
    assert out.status == 200
    assert out.tx_hash == "0xtx_hash"
    assert out.signer_address == "0xabcd000000000000000000000000000000000003"
    assert out.signer_network == "evm"


@pytest.mark.asyncio
async def test_make_mppx_compose_hook_serializes_receipt_to_payment_receipt_header() -> None:
    """When the pympp ``Receipt`` exposes ``to_payment_receipt()``, the compose
    hook lifts the serialized header onto ``MppxComposeOutcome.payment_receipt_header``
    so Checkout can echo it as the response's ``Payment-Receipt`` header."""

    class _Credential:
        source = "did:pkh:eip155:8453:0xABCD000000000000000000000000000000000003"

    class _Receipt:
        reference = "0xtx_hash"
        transaction = None

        @staticmethod
        def to_payment_receipt() -> str:
            return "eyJzdGF0dXMiOiJzdWNjZXNzIn0"

    class _Mpp:
        realm = "r"

        async def charge(self, *, authorization: str | None, amount: str) -> tuple:
            return (_Credential(), _Receipt())

    async def _getter() -> _Mpp:
        return _Mpp()

    hook = make_mppx_compose_hook(server_getter=_getter)

    class _Pricing:
        amount_usd = 0.1

    class _Ctx:
        request = _req({"authorization": "Payment somevalidcred"})
        pricing = _Pricing()

    out = await hook(_Ctx())
    assert out.status == 200
    assert out.payment_receipt_header == "eyJzdGF0dXMiOiJzdWNjZXNzIn0"


@pytest.mark.asyncio
async def test_make_mppx_compose_hook_omits_receipt_header_when_unavailable() -> None:
    """Older pympp Receipts without ``to_payment_receipt()`` leave the header
    field None rather than raising or fabricating."""

    class _Credential:
        source = "did:pkh:eip155:8453:0xABCD000000000000000000000000000000000003"

    class _Receipt:
        reference = "0xtx_hash"
        transaction = None

    class _Mpp:
        realm = "r"

        async def charge(self, *, authorization: str | None, amount: str) -> tuple:
            return (_Credential(), _Receipt())

    async def _getter() -> _Mpp:
        return _Mpp()

    hook = make_mppx_compose_hook(server_getter=_getter)

    class _Pricing:
        amount_usd = 0.1

    class _Ctx:
        request = _req({"authorization": "Payment somevalidcred"})
        pricing = _Pricing()

    out = await hook(_Ctx())
    assert out.status == 200
    assert out.payment_receipt_header is None


@pytest.mark.asyncio
async def test_make_mppx_compose_hook_returns_402_when_charge_raises() -> None:
    class _Mpp:
        realm = "r"

        async def charge(self, *, authorization: str | None, amount: str) -> Any:
            raise RuntimeError("pympp blew up")

    async def _getter() -> _Mpp:
        return _Mpp()

    hook = make_mppx_compose_hook(server_getter=_getter)

    class _Pricing:
        amount_usd = 1.0

    class _Ctx:
        request = _req()
        pricing = _Pricing()

    out = await hook(_Ctx())
    assert out.status == 402


# ─────────────────────────────────────────────────────────────────────────────
# discovery/agentscore_content + redemption_md
# ─────────────────────────────────────────────────────────────────────────────


def test_purchase_mode_note_returns_known_modes() -> None:
    for mode in ("redemption_only", "coupon_applicable", "paid_only"):
        assert purchase_mode_note(mode) == PURCHASE_MODE_NOTES[mode]


def test_purchase_mode_note_unknown_returns_empty_string() -> None:
    assert purchase_mode_note("not-a-real-mode") == ""


def test_build_agentscore_onboarding_steps_substitutes_merchant_url_and_rails() -> None:
    steps = build_agentscore_onboarding_steps(
        merchant_name="AgentScore Store",
        app_url="https://store.example",
        accepted_rails=["tempo", "x402-base", "solana-mpp"],
        requires_kyc=True,
    )
    text = "\n".join(steps)
    assert "AgentScore Store" in text
    assert "Tempo USDC" in text
    assert "x402 USDC on Base" in text
    assert "Solana SPL USDC" in text
    assert "tempo | base | solana" in text
    assert "required for this merchant" in text
    assert "https://store.example/catalog" in text
    assert "https://store.example/purchase" in text


def test_build_agentscore_onboarding_steps_no_kyc_drops_required_clause() -> None:
    steps = build_agentscore_onboarding_steps(
        merchant_name="API Co",
        app_url="https://api.example",
        accepted_rails=["x402-base"],
        requires_kyc=False,
    )
    assert "required for this merchant" not in "\n".join(steps)


def test_build_agentscore_onboarding_steps_unknown_rails_passed_through() -> None:
    steps = build_agentscore_onboarding_steps(
        merchant_name="X",
        app_url="https://x.example",
        accepted_rails=["future-rail"],
    )
    assert "future-rail" in "\n".join(steps)
    assert "tempo|base" in steps[-1]  # default fallback when no mappable rail present


def test_standard_endpoint_descriptions_mentions_all_routes() -> None:
    desc = standard_endpoint_descriptions()
    assert "GET /catalog" in desc
    assert "POST /purchase" in desc
    assert "GET /orders/{id}" in desc
    assert "GET /orders/{id}/status" not in desc

    with_status = standard_endpoint_descriptions(include_order_status_route=True)
    assert "GET /orders/{id}/status" in with_status


def test_standard_endpoint_descriptions_api_kind_drops_catalog_routes() -> None:
    desc = standard_endpoint_descriptions(kind="api")
    assert "POST /<endpoint>" in desc
    assert "GET /usage" in desc
    assert "GET /catalog" not in desc
    assert "GET /orders/{id}" not in desc


def test_build_success_next_steps_omits_eta_when_missing() -> None:
    out = build_success_next_steps(order_status_url="https://x/orders/1")
    assert out == {
        "action": "done",
        "order_status_url": "https://x/orders/1",
        "user_message": (
            "Payment complete. Your AgentScore Passport is now active across every AgentScore-gated merchant."
        ),
    }


def test_build_success_next_steps_includes_eta_when_provided() -> None:
    out = build_success_next_steps(
        order_status_url="https://x/orders/1",
        fulfillment_eta="ships in 3-5 business days",
    )
    assert out["fulfillment_eta"] == "ships in 3-5 business days"


def test_build_redemption_skill_md_substitutes_merchant_and_url() -> None:
    md = build_redemption_skill_md(
        merchant_name="AgentScore Store",
        app_url="https://store.example",
    )
    assert "AgentScore Store" in md
    assert "https://store.example/catalog" in md
    assert "https://store.example/purchase" in md
    assert "Don't have a code?" not in md


def test_build_redemption_skill_md_with_peer_pointer_emits_section() -> None:
    md = build_redemption_skill_md(
        merchant_name="AgentScore Store",
        app_url="https://store.example",
        peer_merchant_pointer="https://martin.example",
        sku_intro="a custom SKU intro.",
    )
    assert "Don't have a code?" in md
    # `see: ` prefix anchors the substring inside the rendered markdown section
    # rather than appearing as a bare URL match (CodeQL py/incomplete-url-substring-sanitization).
    assert "see: https://martin.example\n" in md
    assert "a custom SKU intro." in md


# ─────────────────────────────────────────────────────────────────────────────
# validation_envelope + per-framework validation_response_*
# ─────────────────────────────────────────────────────────────────────────────


def test_validation_envelope_shape() -> None:
    out = validation_envelope(code="bad", message="nope", extra={"hint": "x"})
    assert out["error"]["code"] == "bad"
    assert out["error"]["message"] == "nope"
    assert out["next_steps"]["action"] == "fix_request"
    assert out["next_steps"]["user_message"] == "nope"
    assert out.get("hint") == "x"


def test_validation_response_fastapi_returns_jsonresponse_with_status() -> None:
    from fastapi.responses import JSONResponse

    resp = validation_response_fastapi(code="bad", message="nope", status=422)
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 422
    assert json.loads(resp.body)["error"]["code"] == "bad"


def test_validation_response_flask_returns_response_with_status() -> None:
    flask = pytest.importorskip("flask")

    app = flask.Flask(__name__)
    with app.app_context():
        resp = validation_response_flask(code="bad", message="nope", status=400)
        assert resp.status_code == 400
        body = json.loads(resp.get_data(as_text=True))
        assert body["error"]["code"] == "bad"


def test_validation_response_django_returns_jsonresponse_with_status() -> None:
    pytest.importorskip("django")
    from django.conf import settings as dj_settings

    if not dj_settings.configured:
        dj_settings.configure(DEBUG=True, ALLOWED_HOSTS=["*"])

    resp = validation_response_django(code="bad", message="nope", status=400)
    assert resp.status_code == 400
    body = json.loads(resp.content)
    assert body["error"]["code"] == "bad"


def test_validation_response_aiohttp_returns_web_response() -> None:
    pytest.importorskip("aiohttp")
    resp = validation_response_aiohttp(code="bad", message="nope", status=400)
    assert resp.status == 400
    assert b'"bad"' in resp.body


def test_validation_response_sanic_returns_http_response() -> None:
    pytest.importorskip("sanic")
    resp = validation_response_sanic(code="bad", message="nope", status=400)
    assert resp.status == 400
    body_bytes = resp.body if isinstance(resp.body, bytes) else resp.body.encode()
    assert b'"bad"' in body_bytes


# ─────────────────────────────────────────────────────────────────────────────
# Checkout framework adapters (handle_flask, handle_django, handle_aiohttp, handle_sanic)
# ─────────────────────────────────────────────────────────────────────────────


def _minimal_checkout() -> Checkout:
    """Build a Checkout that returns inline on settle so each adapter can exercise
    handle_<framework> end-to-end without needing a real x402 server."""
    from agentscore_commerce.payment.rail_spec import TempoRailSpec

    async def _pricing(ctx: Any) -> Any:
        from agentscore_commerce.checkout import PricingResult

        return PricingResult(amount_usd=1.0)

    async def _compose_mppx(ctx: Any) -> MppxComposeOutcome:
        # Discovery leg: no auth → 402 with WWW-Auth.
        if not ctx.request.headers.get("authorization"):
            return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="test"'})
        return MppxComposeOutcome(
            status=200,
            rail_key="tempo",
            tx_hash="0xtest",
            signer_address="0x" + "00" * 19 + "dE",
            signer_network="evm",
        )

    async def _on_settled(ctx: Any, outcome: SettleOutcome) -> dict[str, Any]:
        return {"order_id": "o-1", "tx_hash": outcome.tx_hash}

    return Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD")},
        url="https://api.example/purchase",
        compute_pricing=_pricing,
        compose_mppx=_compose_mppx,
        on_settled=_on_settled,
    )


@pytest.mark.asyncio
async def test_handle_aiohttp_returns_402_on_discovery_leg() -> None:
    aiohttp_pytest = pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    checkout = _minimal_checkout()
    req = make_mocked_request("POST", "/purchase", headers={})
    resp = await checkout.handle_aiohttp(req, body={"item": "wine"})
    assert isinstance(resp, web.Response)
    assert resp.status == 402
    _ = aiohttp_pytest  # marker for ruff


@pytest.mark.asyncio
async def test_handle_aiohttp_missing_body_falls_through_to_402() -> None:
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import make_mocked_request

    checkout = _minimal_checkout()

    class _NoJsonReq:
        method = "POST"
        url = "/purchase"
        headers: dict[str, str] = {}

        async def json(self) -> Any:
            raise ValueError("not json")

    resp = await checkout.handle_aiohttp(_NoJsonReq())
    assert resp.status == 402
    _ = make_mocked_request  # ruff


@pytest.mark.asyncio
async def test_handle_sanic_returns_402_on_discovery_leg() -> None:
    pytest.importorskip("sanic")

    class _SanicReq:
        method = "POST"
        url = "/purchase"
        headers: dict[str, str] = {}

        @property
        def json(self) -> dict[str, Any]:
            return {"item": "wine"}

    checkout = _minimal_checkout()
    resp = await checkout.handle_sanic(_SanicReq())
    assert resp.status == 402


def test_handle_flask_returns_402_on_discovery_leg() -> None:
    flask = pytest.importorskip("flask")

    app = flask.Flask(__name__)
    checkout = _minimal_checkout()

    with app.test_request_context("/purchase", method="POST", json={"item": "wine"}):
        from flask import request as flask_request

        resp = checkout.handle_flask(flask_request)
        assert resp.status_code == 402


# ─────────────────────────────────────────────────────────────────────────────
# Checkout gate hooks (CheckoutGateConfig)
# ─────────────────────────────────────────────────────────────────────────────


def _checkout_with_gate(gate: Any) -> Checkout:
    """Build a Checkout configured with `gate=...` whose settle path returns inline."""
    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.payment.rail_spec import TempoRailSpec

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    async def _compose_mppx(ctx: Any) -> MppxComposeOutcome:
        if not ctx.request.headers.get("authorization"):
            return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="test"'})
        return MppxComposeOutcome(
            status=200,
            rail_key="tempo",
            tx_hash="0xtest",
            signer_address="0x" + "00" * 19 + "dE",
            signer_network="evm",
        )

    async def _on_settled(_ctx: Any, outcome: SettleOutcome) -> dict[str, Any]:
        return {"order_id": "o-1", "tx_hash": outcome.tx_hash}

    return Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD")},
        url="https://api.example/purchase",
        compute_pricing=_pricing,
        compose_mppx=_compose_mppx,
        on_settled=_on_settled,
        gate=gate,
    )


@pytest.mark.asyncio
async def test_gate_run_gate_escape_hatch_allow_pass_through() -> None:
    """`gate.run_gate` returning None means allow → request continues to settle."""
    from agentscore_commerce.checkout import CheckoutGateConfig

    seen: list[Any] = []

    async def _run_gate(ctx: Any) -> None:
        seen.append(ctx)
        return

    gate = CheckoutGateConfig(api_key="k", run_gate=_run_gate)
    checkout = _checkout_with_gate(gate)
    result = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/purchase",
            headers={"authorization": "Payment <opaque>"},
            body={"item": "wine"},
        ),
    )
    assert result.status == 200
    assert seen  # run_gate was actually invoked


@pytest.mark.asyncio
async def test_gate_run_gate_escape_hatch_deny_returns_canonical_envelope() -> None:
    """`gate.run_gate` returning a dict → denial; status + body propagate."""
    from agentscore_commerce.checkout import CheckoutGateConfig

    async def _run_gate(_ctx: Any) -> dict[str, Any]:
        return {"status": 403, "body": {"error": {"code": "custom_denied"}}, "headers": {"X-Custom": "v"}}

    gate = CheckoutGateConfig(api_key="k", run_gate=_run_gate)
    checkout = _checkout_with_gate(gate)
    result = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/purchase",
            headers={"authorization": "Payment <opaque>"},
            body={},
        ),
    )
    assert result.status == 403
    assert result.body["error"]["code"] == "custom_denied"
    assert result.headers["X-Custom"] == "v"
    assert result.settled is False
    assert result.settle_phase == "gate_denied"


@pytest.mark.asyncio
async def test_gate_run_gate_returning_unexpected_type_raises() -> None:
    """`gate.run_gate` returning something other than None/dict raises TypeError."""
    from agentscore_commerce.checkout import CheckoutGateConfig

    async def _run_gate(_ctx: Any) -> str:
        return "not-allowed-shape"

    gate = CheckoutGateConfig(api_key="k", run_gate=_run_gate)
    checkout = _checkout_with_gate(gate)
    with pytest.raises(TypeError, match="must return None"):
        await checkout.handle(
            CheckoutRequest(
                method="POST",
                url="https://api.example/purchase",
                headers={"authorization": "Payment <opaque>"},
                body={},
            ),
        )


@pytest.mark.asyncio
async def test_gate_per_request_policy_none_routes_to_wallet_ofac_floor_denies_sdn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`per_request_policy(ctx) → None` no longer skips the gate — it falls through
    to the always-on wallet OFAC SDN floor. With an api_key + a wallet-signed
    payment, the floor screens the signer and DENIES an OFAC-SDN signer."""
    from agentscore_commerce.checkout import CheckoutGateConfig
    from agentscore_commerce.payment.signer import PaymentSigner

    async def _policy(_ctx: Any) -> None:
        return None

    gate = CheckoutGateConfig(api_key="k", per_request_policy=_policy)
    checkout = _checkout_with_gate(gate)
    sdn_signer = PaymentSigner(address="0xdead000000000000000000000000000000000bad", network="evm")
    with (
        patch("agentscore_commerce.payment.signer.extract_payment_signer", return_value=sdn_signer),
        patch(
            "agentscore_commerce.api.AgentScore.aassess",
            new=AsyncMock(return_value={"decision": "deny", "decision_reasons": ["sanctions_flagged"]}),
        ) as mock_aassess,
    ):
        result = await checkout.handle(
            CheckoutRequest(
                method="POST",
                url="https://api.example/purchase",
                headers={"authorization": "Payment <opaque>"},
                body={},
            ),
        )
    # Floor fired and denied on the SDN signer — settle must NOT proceed.
    mock_aassess.assert_called_once()
    assert result.status == 403
    assert result.settled is False


@pytest.mark.asyncio
async def test_gate_per_request_policy_none_routes_to_wallet_ofac_floor_allows_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`per_request_policy(ctx) → None` → wallet OFAC floor; a CLEAN signer passes
    the floor and settle proceeds to 200."""
    from agentscore_commerce.checkout import CheckoutGateConfig
    from agentscore_commerce.payment.signer import PaymentSigner

    async def _policy(_ctx: Any) -> None:
        return None

    gate = CheckoutGateConfig(api_key="k", per_request_policy=_policy)
    checkout = _checkout_with_gate(gate)
    clean_signer = PaymentSigner(address="0xaaa0000000000000000000000000000000000099", network="evm")
    with (
        patch("agentscore_commerce.payment.signer.extract_payment_signer", return_value=clean_signer),
        patch(
            "agentscore_commerce.api.AgentScore.aassess",
            new=AsyncMock(return_value={"decision": "allow", "decision_reasons": []}),
        ) as mock_aassess,
    ):
        result = await checkout.handle(
            CheckoutRequest(
                method="POST",
                url="https://api.example/purchase",
                headers={"authorization": "Payment <opaque>"},
                body={},
            ),
        )
    mock_aassess.assert_called_once()
    assert result.status == 200


@pytest.mark.asyncio
async def test_gate_per_request_policy_none_floor_skips_without_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`per_request_policy(ctx) → None` → wallet OFAC floor; with NO extractable
    signer (Stripe SPT / card / no crypto payment) the floor is a no-op — no
    forced wallet, no assess call, settle proceeds to 200."""
    from agentscore_commerce.checkout import CheckoutGateConfig

    async def _policy(_ctx: Any) -> None:
        return None

    gate = CheckoutGateConfig(api_key="k", per_request_policy=_policy)
    checkout = _checkout_with_gate(gate)
    with (
        patch("agentscore_commerce.payment.signer.extract_payment_signer", return_value=None),
        patch("agentscore_commerce.api.AgentScore.aassess", new=AsyncMock()) as mock_aassess,
    ):
        result = await checkout.handle(
            CheckoutRequest(
                method="POST",
                url="https://api.example/purchase",
                headers={"authorization": "Payment <opaque>"},
                body={},
            ),
        )
    # No signer → floor never reaches /v1/assess; nothing forced.
    mock_aassess.assert_not_called()
    assert result.status == 200


@pytest.mark.asyncio
async def test_gate_on_denied_callback_reshapes_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gate.on_denied` returning `{body, status}` overrides the canonical body."""
    from agentscore_commerce.checkout import CheckoutGateConfig
    from agentscore_commerce.identity.policy import GateResult

    async def _mock_run_gate(_raw: Any, _gate_instance: Any, *, enforcement: Any = None) -> GateResult:
        return GateResult(
            status="denied",
            denial_body={"error": {"code": "kyc_required"}},
            denial_status=403,
        )

    monkeypatch.setattr(
        "agentscore_commerce.identity.policy.run_gate_with_enforcement",
        _mock_run_gate,
    )

    async def _on_denied(_ctx: Any, body: dict[str, Any]) -> dict[str, Any]:
        return {"status": 402, "body": {**body, "augmented": True}}

    gate = CheckoutGateConfig(api_key="k", require_kyc=True, on_denied=_on_denied)
    checkout = _checkout_with_gate(gate)
    result = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/purchase",
            headers={"authorization": "Payment <opaque>"},
            body={},
            raw=object(),  # gate path requires `raw` to be set
        ),
    )
    assert result.status == 402
    assert result.body["augmented"] is True
    assert result.body["error"]["code"] == "kyc_required"


@pytest.mark.asyncio
async def test_gate_allow_attaches_capture_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful gate allow stashes `ctx.capture_wallet` for `on_settled`."""
    from agentscore_commerce.checkout import CheckoutGateConfig
    from agentscore_commerce.identity.policy import GateResult

    async def _mock_run_gate(_raw: Any, _gate_instance: Any, *, enforcement: Any = None) -> GateResult:
        return GateResult(status="verified", denial_body=None, denial_status=None)

    monkeypatch.setattr(
        "agentscore_commerce.identity.policy.run_gate_with_enforcement",
        _mock_run_gate,
    )

    capture_calls: list[dict[str, Any]] = []

    async def _on_settled(ctx: Any, outcome: SettleOutcome) -> dict[str, Any]:
        if ctx.capture_wallet is not None:
            # Don't actually fire — would call AgentScoreCore — but mark that the closure exists.
            capture_calls.append({"available": True, "tx": outcome.tx_hash})
        return {"order_id": "o-1"}

    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.payment.rail_spec import TempoRailSpec

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    async def _compose_mppx(ctx: Any) -> MppxComposeOutcome:
        if not ctx.request.headers.get("authorization"):
            return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="test"'})
        return MppxComposeOutcome(
            status=200,
            rail_key="tempo",
            tx_hash="0xtest",
            signer_address="0x" + "00" * 19 + "dE",
            signer_network="evm",
        )

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD")},
        url="https://api.example/purchase",
        compute_pricing=_pricing,
        compose_mppx=_compose_mppx,
        on_settled=_on_settled,
        gate=CheckoutGateConfig(api_key="k", require_kyc=True),
    )
    result = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/purchase",
            headers={"authorization": "Payment <opaque>", "x-operator-token": "opc_test_token"},
            body={},
            raw=object(),
        ),
    )
    assert result.status == 200
    assert capture_calls == [{"available": True, "tx": "0xtest"}]


# ─────────────────────────────────────────────────────────────────────────────
# Checkout discovery_probe auto-routing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkout_discovery_probe_emits_sample_402_on_empty_body() -> None:
    from agentscore_commerce import (
        Checkout,
        CheckoutRequest,
        DiscoveryProbeConfig,
        PricingResult,
    )
    from agentscore_commerce.payment.rail_spec import TempoRailSpec

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=0.01)

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE")},
        url="https://api.example/search",
        compute_pricing=_pricing,
        discovery_probe=DiscoveryProbeConfig(
            realm="api.example",
            sample_rail="tempo-mainnet",
            sample_amount_usd=0.01,
            sample_recipient="0xRecipient",
        ),
    )
    result = await checkout.handle(
        CheckoutRequest(method="POST", url="https://api.example/search", headers={}, body={}),
    )
    assert result.status == 402
    assert result.settle_phase == "discovery_probe"
    # Probe body carries the discovery marker + a payment-required error
    assert result.body.get("discovery") is True
    assert result.body.get("error", {}).get("code") == "payment_required"


@pytest.mark.asyncio
async def test_checkout_discovery_probe_skipped_when_payment_header_present() -> None:
    from agentscore_commerce import (
        Checkout,
        CheckoutRequest,
        DiscoveryProbeConfig,
        PricingResult,
    )
    from agentscore_commerce.payment.rail_spec import TempoRailSpec

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=0.01)

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE")},
        url="https://api.example/search",
        compute_pricing=_pricing,
        discovery_probe=DiscoveryProbeConfig(
            realm="api.example",
            sample_rail="tempo-mainnet",
            sample_amount_usd=0.01,
            sample_recipient="0xRecipient",
        ),
    )
    result = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/search",
            headers={"authorization": "Payment <cred>"},
            body={},
        ),
    )
    # With a payment header, falls through to regular handling (not the probe path).
    assert result.settle_phase != "discovery_probe"


@pytest.mark.asyncio
async def test_checkout_discovery_probe_skipped_when_body_nonempty() -> None:
    from agentscore_commerce import (
        Checkout,
        CheckoutRequest,
        DiscoveryProbeConfig,
        PricingResult,
    )
    from agentscore_commerce.payment.rail_spec import TempoRailSpec

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=0.01)

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE")},
        url="https://api.example/search",
        compute_pricing=_pricing,
        discovery_probe=DiscoveryProbeConfig(
            realm="api.example",
            sample_rail="tempo-mainnet",
            sample_amount_usd=0.01,
            sample_recipient="0xRecipient",
        ),
    )
    result = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/search",
            headers={},
            body={"query": "test"},
        ),
    )
    # Real business body → not a probe; falls through to regular 402 emit.
    assert result.settle_phase != "discovery_probe"


# ─────────────────────────────────────────────────────────────────────────────
# pricing_result factory
# ─────────────────────────────────────────────────────────────────────────────


def test_pricing_result_derives_amount_from_cents() -> None:
    from agentscore_commerce import pricing_result

    pr = pricing_result(subtotal_cents=25000, tax_cents=2000)
    assert pr.amount_usd == 270.0
    assert pr.currency == "USD"
    assert pr.block is not None
    assert pr.block.subtotal == "250.00"
    assert pr.block.tax == "20.00"


def test_pricing_result_includes_shipping_when_set() -> None:
    from agentscore_commerce import pricing_result

    pr = pricing_result(subtotal_cents=25000, tax_cents=2000, shipping_cents=999)
    assert pr.amount_usd == 279.99


def test_pricing_result_tax_rate_and_state_attach_to_block() -> None:
    from agentscore_commerce import pricing_result

    pr = pricing_result(subtotal_cents=25000, tax_cents=2000, tax_rate=0.08, tax_state="CA")
    assert pr.block is not None
    assert pr.block.tax_rate == 0.08
    assert pr.block.tax_state == "CA"


def test_pricing_result_passthrough_amount_usd() -> None:
    from agentscore_commerce import pricing_result

    pr = pricing_result(amount_usd=0.01)
    assert pr.amount_usd == 0.01
    assert pr.block is None


def test_pricing_result_explicit_amount_overrides_subtotal_derivation() -> None:
    from agentscore_commerce import pricing_result

    pr = pricing_result(subtotal_cents=25000, tax_cents=2000, amount_usd=999.99)
    assert pr.amount_usd == 999.99
    assert pr.block is not None
    assert pr.block.subtotal == "250.00"


def test_pricing_result_raises_when_no_amount_source() -> None:
    from agentscore_commerce import pricing_result

    with pytest.raises(ValueError, match=r"subtotal_cents.*amount_usd"):
        pricing_result(currency="USD")


def test_pricing_result_propagates_product_and_body_extras() -> None:
    from agentscore_commerce import pricing_result

    product = {"id": "sku_1", "name": "Test"}
    extras = {"redemption_code_applied": "WELCOME"}
    pr = pricing_result(subtotal_cents=100, product=product, body_extras=extras)
    assert pr.product == product
    assert pr.body_extras == extras


def test_pricing_result_full_discount_zeros_amount_and_surfaces_savings() -> None:
    # Redemption-code applied: subtotal stays list, discount equals list, total/amount are 0.
    from agentscore_commerce import pricing_result

    pr = pricing_result(subtotal_cents=7500, discount_cents=7500)
    assert pr.amount_usd == 0.0
    assert pr.block is not None
    assert pr.block.subtotal == "75.00"
    assert pr.block.discount == "75.00"
    assert pr.block.total == "0.00"


def test_pricing_result_partial_discount_settle_floor() -> None:
    # 74.99 discount against 75.00 list leaves a 1-cent settle floor.
    from agentscore_commerce import pricing_result

    pr = pricing_result(subtotal_cents=7500, discount_cents=7499)
    assert pr.amount_usd == 0.01
    assert pr.block is not None
    assert pr.block.discount == "74.99"
    assert pr.block.total == "0.01"


def test_pricing_result_discount_floors_amount_at_zero() -> None:
    from agentscore_commerce import pricing_result

    pr = pricing_result(subtotal_cents=1000, discount_cents=5000)
    assert pr.amount_usd == 0.0
    assert pr.block is not None
    assert pr.block.total == "0.00"


@pytest.mark.asyncio
async def test_checkout_accepted_rails_dedupes_per_protocol() -> None:
    """`Checkout.accepted_rails` folds tempo+tempo_session into one and emits per-protocol slugs."""
    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.payment import SolanaMppRailSpec, StripeRailSpec, TempoSessionRailSpec

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient="0x" + "00" * 20),
            "tempo_session": TempoSessionRailSpec(
                recipient="0x" + "00" * 20,
                escrow_contract="0x" + "11" * 20,
                store=object(),
            ),
            "base": X402BaseRailSpec(recipient="0x" + "00" * 20),
            "solana": SolanaMppRailSpec(recipient="SoLa"),
            "stripe": StripeRailSpec(profile_id="profile_abc"),
        },
        url="https://x/purchase",
        compute_pricing=_pricing,
    )
    rails = checkout.accepted_rails
    # tempo + tempo_session fold to "tempo_mpp"
    assert rails.count("tempo_mpp") == 1
    assert "x402_base" in rails
    assert "solana_mpp" in rails
    assert "stripe" in rails


@pytest.mark.asyncio
async def test_checkout_accepted_method_names_emits_protocol_methods() -> None:
    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.payment import SolanaMppRailSpec, StripeRailSpec

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient="0x" + "00" * 20),
            "base": X402BaseRailSpec(recipient="0x" + "00" * 20),
            "solana": SolanaMppRailSpec(recipient="SoLa"),
            "stripe": StripeRailSpec(profile_id="profile_abc"),
        },
        url="https://x/purchase",
        compute_pricing=_pricing,
    )
    names = checkout.accepted_method_names
    assert "tempo/charge" in names
    assert "x402/exact (base)" in names
    assert "solana/charge" in names
    assert "stripe/spt" in names


@pytest.mark.asyncio
async def test_capture_wallet_closure_calls_acapture_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The closure installed on ctx.capture_wallet calls AgentScoreCore.acapture_wallet."""
    from agentscore_commerce.checkout import (
        Checkout,
        CheckoutGateConfig,
        PricingResult,
    )
    from agentscore_commerce.identity.policy import GateResult

    async def _mock_run_gate(_raw: Any, _gate_instance: Any, *, enforcement: Any = None) -> GateResult:
        return GateResult(status="verified", denial_body=None, denial_status=None)

    monkeypatch.setattr(
        "agentscore_commerce.identity.policy.run_gate_with_enforcement",
        _mock_run_gate,
    )

    capture_calls: list[dict[str, Any]] = []

    async def _mock_acapture_wallet(
        self: Any, *, operator_token: str, wallet_address: str, network: str, idempotency_key: str | None = None
    ) -> None:
        capture_calls.append(
            {
                "operator_token": operator_token,
                "wallet_address": wallet_address,
                "network": network,
                "idempotency_key": idempotency_key,
            }
        )

    monkeypatch.setattr(
        "agentscore_commerce.identity.core.AgentScoreCore.acapture_wallet",
        _mock_acapture_wallet,
    )

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    async def _compose_mppx(ctx: Any) -> MppxComposeOutcome:
        if not ctx.request.headers.get("authorization"):
            return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="t"'})
        return MppxComposeOutcome(
            status=200,
            rail_key="tempo",
            tx_hash="0xtest",
            signer_address="0xabc0000000000000000000000000000000000001",
            signer_network="evm",
        )

    async def _on_settled(ctx: Any, outcome: SettleOutcome) -> dict[str, Any]:
        # Actually invoke the closure to drive the AgentScoreCore call path.
        if ctx.capture_wallet is not None and outcome.signer_address is not None:
            await ctx.capture_wallet(
                wallet_address=outcome.signer_address,
                network=outcome.signer_network or "evm",
                idempotency_key=outcome.tx_hash,
            )
        return {"order_id": "o-1"}

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD")},
        url="https://api.example/purchase",
        compute_pricing=_pricing,
        compose_mppx=_compose_mppx,
        on_settled=_on_settled,
        gate=CheckoutGateConfig(api_key="k", require_kyc=True),
    )
    result = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/purchase",
            headers={"authorization": "Payment <opaque>", "x-operator-token": "opc_test"},
            body={},
            raw=object(),
        ),
    )
    assert result.status == 200
    assert capture_calls == [
        {
            "operator_token": "opc_test",
            "wallet_address": "0xabc0000000000000000000000000000000000001",
            "network": "evm",
            "idempotency_key": "0xtest",
        }
    ]


@pytest.mark.asyncio
async def test_handle_zero_settle_mpp_carve_out() -> None:
    """zero_settle_carve_out=True + 0-amount + MPP authorization → skips settle path."""
    from agentscore_commerce.checkout import Checkout, PricingResult

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=0.0)

    async def _on_settled(_ctx: Any, outcome: SettleOutcome) -> dict[str, Any]:
        return {"order_id": "o-1", "tx_hash": outcome.tx_hash, "rail_key": outcome.rail_key}

    async def _compose_mppx(_ctx: Any) -> MppxComposeOutcome:
        return MppxComposeOutcome(status=402, headers={"www-authenticate": 'Payment realm="t"'})

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD")},
        url="https://api.example/purchase",
        compute_pricing=_pricing,
        compose_mppx=_compose_mppx,
        on_settled=_on_settled,
        zero_settle_carve_out=True,
    )
    result = await checkout.handle(
        CheckoutRequest(
            method="POST",
            url="https://api.example/purchase",
            # MPP credential present + amount $0 → carve-out path; skips real mppx.charge.
            headers={"authorization": "Payment <opaque-credential>"},
            body={"item": "wine"},
        ),
    )
    # Carve-out returns 200 with tx_hash=None (no on-chain settle for $0).
    assert result.status == 200
    assert result.body.get("tx_hash") is None
    # The rail_key was lifted from MPP path
    assert result.body.get("rail_key") in {"tempo", "tempo_mpp"}


def test_handle_django_returns_402_on_discovery_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("django")
    from django.conf import settings as dj_settings

    if not dj_settings.configured:
        dj_settings.configure(DEBUG=True, ALLOWED_HOSTS=["*"])
    monkeypatch.setattr(dj_settings, "ALLOWED_HOSTS", ["*"])

    from django.test import RequestFactory

    checkout = _minimal_checkout()
    factory = RequestFactory()
    req = factory.post(
        "/purchase",
        data=json.dumps({"item": "wine"}),
        content_type="application/json",
    )
    resp = checkout.handle_django(req)
    assert resp.status_code == 402


# ─────────────────────────────────────────────────────────────────────────────
# load_solana_fee_payer
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# buildSignedUcpResponse / buildSignedJwksResponse / bootstrapUcpSigningKey
# ─────────────────────────────────────────────────────────────────────────────


@_contextlib.contextmanager
def _env_key(jwk_dict: dict[str, Any]) -> Any:
    """Yield with UCP_SIGNING_KEY_JWK_PRIVATE set to ``jwk_dict``, restoring on exit."""
    from agentscore_commerce.identity.ucp_jwks import _reset_ucp_signing_key_cache

    _reset_ucp_signing_key_cache()
    prev = os.environ.get("UCP_SIGNING_KEY_JWK_PRIVATE")
    os.environ["UCP_SIGNING_KEY_JWK_PRIVATE"] = json.dumps(jwk_dict)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("UCP_SIGNING_KEY_JWK_PRIVATE", None)
        else:
            os.environ["UCP_SIGNING_KEY_JWK_PRIVATE"] = prev
        _reset_ucp_signing_key_cache()


def test_bootstrap_ucp_signing_key_throws_on_malformed_env() -> None:
    from agentscore_commerce.discovery import bootstrap_ucp_signing_key
    from agentscore_commerce.identity.ucp_jwks import _reset_ucp_signing_key_cache

    _reset_ucp_signing_key_cache()
    prev = os.environ.get("UCP_SIGNING_KEY_JWK_PRIVATE")
    os.environ["UCP_SIGNING_KEY_JWK_PRIVATE"] = "not-json"
    try:
        with pytest.raises((ValueError, Exception)):
            bootstrap_ucp_signing_key()
    finally:
        if prev is None:
            os.environ.pop("UCP_SIGNING_KEY_JWK_PRIVATE", None)
        else:
            os.environ["UCP_SIGNING_KEY_JWK_PRIVATE"] = prev
        _reset_ucp_signing_key_cache()


def test_bootstrap_ucp_signing_key_succeeds_with_valid_env() -> None:
    from agentscore_commerce.discovery import bootstrap_ucp_signing_key
    from agentscore_commerce.identity.ucp_jwks import generate_ucp_signing_key

    key = generate_ucp_signing_key(kid="bootstrap-test")
    private_jwk = key.private_key.as_dict(private=True)
    with _env_key(private_jwk):
        bootstrap_ucp_signing_key()  # should not raise


def test_build_signed_jwks_response_emits_jwk_set_json() -> None:
    from agentscore_commerce.discovery import build_signed_jwks_response
    from agentscore_commerce.identity.ucp_jwks import generate_ucp_signing_key

    key = generate_ucp_signing_key(kid="jwks-test")
    private_jwk = key.private_key.as_dict(private=True)
    with _env_key(private_jwk):
        resp = build_signed_jwks_response(request_headers={"X-Request-Id": "req-jwks"})
    assert resp.status == 200
    assert resp.media_type == "application/jwk-set+json"
    assert "max-age=300" in resp.headers["Cache-Control"]
    assert resp.headers["X-Request-ID"] == "req-jwks"
    body = json.loads(resp.content)
    assert len(body["keys"]) == 1


def test_build_signed_ucp_response_misconfigured_when_no_rails() -> None:
    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.discovery import build_signed_ucp_response

    async def _pricing(ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    checkout = Checkout(rails={}, url="https://x/purchase", compute_pricing=_pricing)
    resp = build_signed_ucp_response(
        checkout=checkout,
        name="X",
        well_known_ucp_url="https://x/.well-known/ucp",
        services={},
        request_headers={"X-Request-Id": "req-misc"},
    )
    assert resp.status == 503
    assert "max-age=60" in resp.headers["Cache-Control"]
    assert resp.headers["X-Request-ID"] == "req-misc"
    body = json.loads(resp.content)
    assert body["error"]["code"] == "ucp_misconfigured"


def test_build_signed_ucp_response_happy_path_signs_profile() -> None:
    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.discovery import build_signed_ucp_response
    from agentscore_commerce.identity.ucp_jwks import generate_ucp_signing_key

    async def _pricing(ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    key = generate_ucp_signing_key(kid="ucp-test")
    private_jwk = key.private_key.as_dict(private=True)
    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD"),
            "base": X402BaseRailSpec(recipient="0x" + "00" * 19 + "dE" + "aD"),
        },
        url="https://x/purchase",
        compute_pricing=_pricing,
    )
    with _env_key(private_jwk):
        resp = build_signed_ucp_response(
            checkout=checkout,
            name="AgentScore Store",
            well_known_ucp_url="https://x/.well-known/ucp",
            services={"dev.ucp.shopping": []},
            signing_kid="ucp-test",
            request_headers={"X-Request-Id": "req-ucp"},
        )
    assert resp.status == 200
    assert resp.headers["X-Request-ID"] == "req-ucp"
    assert "max-age=60" in resp.headers["Cache-Control"]
    body = json.loads(resp.content)
    assert body["ucp"]["name"] == "AgentScore Store"
    assert "signature" in body
    assert body["ucp"]["payment_handlers"]


def test_build_signed_ucp_response_includes_solana_stripe_tempo_session() -> None:
    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.discovery import build_signed_ucp_response
    from agentscore_commerce.identity.ucp_jwks import generate_ucp_signing_key
    from agentscore_commerce.payment import SolanaMppRailSpec, StripeRailSpec, TempoSessionRailSpec

    async def _pricing(ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    key = generate_ucp_signing_key(kid="ucp-multi")
    private_jwk = key.private_key.as_dict(private=True)
    checkout = Checkout(
        rails={
            "solana": SolanaMppRailSpec(recipient="SoLaNaReCiPiEnT"),
            "stripe": StripeRailSpec(profile_id="profile_abc"),
            "tempo_session": TempoSessionRailSpec(
                recipient="0x" + "00" * 20,
                escrow_contract="0x" + "11" * 20,
                store=object(),
            ),
        },
        url="https://x/purchase",
        compute_pricing=_pricing,
    )
    with _env_key(private_jwk):
        resp = build_signed_ucp_response(
            checkout=checkout,
            name="Multi-Rail",
            well_known_ucp_url="https://x/.well-known/ucp",
            services={},
            signing_kid="ucp-multi",
        )
    assert resp.status == 200
    body = json.loads(resp.content)
    keys = list(body["ucp"]["payment_handlers"].keys())
    assert any("mpp" in k or "stripe" in k for k in keys)


def test_default_a2a_services_returns_canonical_a2a_binding() -> None:
    from agentscore_commerce.discovery.well_known import default_a2a_services

    services = default_a2a_services(agent_card_url="https://x/.well-known/agent-card.json")
    assert "dev.ucp.shopping" in services
    binding = services["dev.ucp.shopping"][0]
    assert binding.transport == "a2a"
    assert binding.endpoint == "https://x/.well-known/agent-card.json"


def test_well_known_cors_preflight_headers_without_request() -> None:
    from agentscore_commerce.discovery import well_known_cors_preflight_headers

    headers = well_known_cors_preflight_headers()
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in headers["Access-Control-Allow-Methods"]
    assert "Access-Control-Allow-Headers" not in headers


def test_well_known_cors_preflight_headers_echoes_acrh() -> None:
    from agentscore_commerce.discovery import well_known_cors_preflight_headers

    headers = well_known_cors_preflight_headers(
        {"Access-Control-Request-Headers": "x-foo, x-bar"},
    )
    assert headers["Access-Control-Allow-Headers"] == "x-foo, x-bar"


# ─────────────────────────────────────────────────────────────────────────────
# load_solana_fee_payer
# ─────────────────────────────────────────────────────────────────────────────


def test_load_solana_fee_payer_returns_none_on_empty() -> None:
    from agentscore_commerce.payment.solana import load_solana_fee_payer

    assert load_solana_fee_payer(None) is None
    assert load_solana_fee_payer("") is None


def test_load_solana_fee_payer_hex_input() -> None:
    pytest.importorskip("solders")
    from solders.keypair import Keypair

    from agentscore_commerce.payment.solana import load_solana_fee_payer

    # 64-byte hex: 32-byte secret + 32-byte public (we discard the public half).
    hex_key = "01" * 64
    signer = load_solana_fee_payer(hex_key)
    assert signer is not None
    expected = Keypair.from_seed(bytes.fromhex(hex_key)[:32])
    assert bytes(signer) == bytes(expected)


def test_load_solana_fee_payer_base58_64_bytes() -> None:
    pytest.importorskip("solders")
    pytest.importorskip("base58")
    import base58
    from solders.keypair import Keypair

    from agentscore_commerce.payment.solana import load_solana_fee_payer

    kp = Keypair()
    full_bytes = bytes(kp)  # solders Keypair serializes to 64 bytes (secret+public)
    encoded = base58.b58encode(full_bytes).decode()
    signer = load_solana_fee_payer(encoded)
    assert signer is not None
    assert bytes(signer) == full_bytes


def test_load_solana_fee_payer_base58_32_bytes_seed() -> None:
    pytest.importorskip("solders")
    pytest.importorskip("base58")
    import base58
    from solders.keypair import Keypair

    from agentscore_commerce.payment.solana import load_solana_fee_payer

    seed = bytes(range(32))
    encoded = base58.b58encode(seed).decode()
    signer = load_solana_fee_payer(encoded)
    assert signer is not None
    expected = Keypair.from_seed(seed)
    assert bytes(signer) == bytes(expected)


def test_load_solana_fee_payer_base58_wrong_length_raises() -> None:
    pytest.importorskip("solders")
    pytest.importorskip("base58")
    import base58

    from agentscore_commerce.payment.solana import load_solana_fee_payer

    encoded = base58.b58encode(b"\x00" * 16).decode()  # 16 bytes; invalid
    with pytest.raises(ValueError, match="must decode to 32 or 64 bytes"):
        load_solana_fee_payer(encoded)


# ─────────────────────────────────────────────────────────────────────────────
# well_known_preflight_response
# ─────────────────────────────────────────────────────────────────────────────


def test_well_known_preflight_response_204_with_cors_headers() -> None:
    from agentscore_commerce.discovery import (
        WellKnownPreflightResponse,
        well_known_preflight_response,
    )

    resp = well_known_preflight_response()
    assert isinstance(resp, WellKnownPreflightResponse)
    assert resp.status == 204
    assert resp.content == b""
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in resp.headers["Access-Control-Allow-Methods"]
    assert "OPTIONS" in resp.headers["Access-Control-Allow-Methods"]


def test_well_known_preflight_response_echoes_request_headers() -> None:
    from agentscore_commerce.discovery import well_known_preflight_response

    resp = well_known_preflight_response({"Access-Control-Request-Headers": "x-foo, x-bar"})
    assert resp.headers["Access-Control-Allow-Headers"] == "x-foo, x-bar"


# ─────────────────────────────────────────────────────────────────────────────
# signed_response_<framework> wrappers
# ─────────────────────────────────────────────────────────────────────────────


def _neutral_signed() -> object:
    from agentscore_commerce.discovery import SignedDiscoveryResponse

    return SignedDiscoveryResponse(
        content=b'{"ok": true}',
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=60"},
        status=200,
    )


def test_signed_response_fastapi_wraps_neutral_payload() -> None:
    from agentscore_commerce.discovery import signed_response_fastapi

    out = signed_response_fastapi(_neutral_signed())
    assert out.status_code == 200
    assert out.headers["cache-control"] == "public, max-age=60"
    assert out.media_type == "application/json"
    assert out.body == b'{"ok": true}'


def test_signed_response_fastapi_handles_preflight() -> None:
    from agentscore_commerce.discovery import (
        signed_response_fastapi,
        well_known_preflight_response,
    )

    out = signed_response_fastapi(well_known_preflight_response())
    assert out.status_code == 204
    assert out.body == b""
    assert out.headers["access-control-allow-origin"] == "*"


def test_signed_response_flask_wraps_neutral_payload() -> None:
    from agentscore_commerce.discovery import signed_response_flask

    out = signed_response_flask(_neutral_signed())
    assert out.status_code == 200
    assert out.mimetype == "application/json"
    assert out.get_data() == b'{"ok": true}'
    assert out.headers.get("Cache-Control") == "public, max-age=60"


def test_signed_response_django_wraps_neutral_payload() -> None:
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(DEBUG=False, ALLOWED_HOSTS=["*"], DEFAULT_CHARSET="utf-8")
        django.setup()

    from agentscore_commerce.discovery import signed_response_django

    out = signed_response_django(_neutral_signed())
    assert out.status_code == 200
    assert out["Content-Type"].startswith("application/json")
    assert out.content == b'{"ok": true}'
    assert out["Cache-Control"] == "public, max-age=60"


def test_signed_response_aiohttp_wraps_neutral_payload() -> None:
    from agentscore_commerce.discovery import signed_response_aiohttp

    out = signed_response_aiohttp(_neutral_signed())
    assert out.status == 200
    assert out.body == b'{"ok": true}'
    assert out.content_type == "application/json"
    assert out.headers["Cache-Control"] == "public, max-age=60"


def test_signed_response_sanic_wraps_neutral_payload() -> None:
    from agentscore_commerce.discovery import signed_response_sanic

    out = signed_response_sanic(_neutral_signed())
    assert out.status == 200
    assert out.body == b'{"ok": true}'
    assert out.content_type == "application/json"


# ─────────────────────────────────────────────────────────────────────────────
# Checkout.mount_ucp_routes_<framework>
# ─────────────────────────────────────────────────────────────────────────────


def _mounted_checkout_with_key() -> tuple[Any, dict[str, Any]]:
    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.identity.ucp_jwks import generate_ucp_signing_key
    from agentscore_commerce.payment import TempoRailSpec

    async def _pricing(_ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    checkout = Checkout(
        rails={"tempo": TempoRailSpec(recipient="0xfeedface")},
        url="https://x/purchase",
        compute_pricing=_pricing,
    )
    key = generate_ucp_signing_key(kid="mount-test")
    return checkout, key.private_key.as_dict(private=True)


def test_mount_ucp_routes_fastapi_registers_three_routes() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    checkout, jwk = _mounted_checkout_with_key()
    app = FastAPI()
    with _env_key(jwk):
        checkout.mount_ucp_routes_fastapi(
            app,
            name="Mount-FastAPI",
            well_known_ucp_url="https://x/.well-known/ucp",
            services={},
            signing_kid="mount-test",
        )
        client = TestClient(app)
        ucp = client.get("/.well-known/ucp")
        jwks = client.get("/.well-known/jwks.json")
        preflight = client.options("/.well-known/ucp")

    assert ucp.status_code == 200
    assert ucp.json()["ucp"]["name"] == "Mount-FastAPI"
    assert jwks.status_code == 200
    assert "keys" in jwks.json()
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "*"


def test_mount_ucp_routes_flask_registers_three_routes() -> None:
    from flask import Flask

    checkout, jwk = _mounted_checkout_with_key()
    app = Flask(__name__)
    with _env_key(jwk):
        checkout.mount_ucp_routes_flask(
            app,
            name="Mount-Flask",
            well_known_ucp_url="https://x/.well-known/ucp",
            services={},
            signing_kid="mount-test",
        )
        client = app.test_client()
        ucp = client.get("/.well-known/ucp")
        jwks = client.get("/.well-known/jwks.json")
        preflight = client.options("/.well-known/ucp")

    assert ucp.status_code == 200
    assert ucp.get_json()["ucp"]["name"] == "Mount-Flask"
    assert jwks.status_code == 200
    assert preflight.status_code == 204


def test_mount_ucp_routes_django_appends_urlpatterns() -> None:
    import django
    from django.conf import settings
    from django.test import RequestFactory

    if not settings.configured:
        settings.configure(DEBUG=False, ALLOWED_HOSTS=["*"], DEFAULT_CHARSET="utf-8", ROOT_URLCONF=__name__)
        django.setup()

    checkout, jwk = _mounted_checkout_with_key()
    patterns: list[Any] = []
    with _env_key(jwk):
        checkout.mount_ucp_routes_django(
            patterns,
            name="Mount-Django",
            well_known_ucp_url="https://x/.well-known/ucp",
            services={},
            signing_kid="mount-test",
        )
        rf = RequestFactory()
        ucp_view = patterns[0].callback
        jwks_view = patterns[1].callback
        ucp_resp = ucp_view(rf.get("/.well-known/ucp"))
        jwks_resp = jwks_view(rf.get("/.well-known/jwks.json"))
        preflight_resp = ucp_view(rf.options("/.well-known/ucp"))

    assert ucp_resp.status_code == 200
    assert json.loads(ucp_resp.content)["ucp"]["name"] == "Mount-Django"
    assert jwks_resp.status_code == 200
    assert preflight_resp.status_code == 204


def test_mount_ucp_routes_aiohttp_registers_three_routes() -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    checkout, jwk = _mounted_checkout_with_key()

    async def _run() -> None:
        app = web.Application()
        checkout.mount_ucp_routes_aiohttp(
            app,
            name="Mount-Aiohttp",
            well_known_ucp_url="https://x/.well-known/ucp",
            services={},
            signing_kid="mount-test",
        )
        async with TestClient(TestServer(app)) as client:
            ucp_resp = await client.get("/.well-known/ucp")
            jwks_resp = await client.get("/.well-known/jwks.json")
            preflight_resp = await client.options("/.well-known/ucp")
            assert ucp_resp.status == 200
            ucp_body = await ucp_resp.json()
            assert ucp_body["ucp"]["name"] == "Mount-Aiohttp"
            assert jwks_resp.status == 200
            assert preflight_resp.status == 204

    with _env_key(jwk):
        asyncio.run(_run())


def test_mount_ucp_routes_sanic_registers_three_routes() -> None:
    from sanic import Sanic

    Sanic._app_registry.clear()
    checkout, jwk = _mounted_checkout_with_key()
    app: Any = Sanic("agentscore-mount-test")
    with _env_key(jwk):
        checkout.mount_ucp_routes_sanic(
            app,
            name="Mount-Sanic",
            well_known_ucp_url="https://x/.well-known/ucp",
            services={},
            signing_kid="mount-test",
        )
        _, ucp_resp = app.test_client.get("/.well-known/ucp")
        _, jwks_resp = app.test_client.get("/.well-known/jwks.json")
        _, preflight_resp = app.test_client.options("/.well-known/ucp")
    assert ucp_resp.status == 200
    assert ucp_resp.json["ucp"]["name"] == "Mount-Sanic"
    assert jwks_resp.status == 200
    assert preflight_resp.status == 204


# ─────────────────────────────────────────────────────────────────────────────
# build_merchant_index_json
# ─────────────────────────────────────────────────────────────────────────────


def test_build_merchant_index_json_core_fields() -> None:
    from agentscore_commerce.discovery import build_merchant_index_json

    body = build_merchant_index_json(
        name="AgentScore Store",
        description="Wine and merch for agents.",
        docs={"llms": "https://x/llms.txt", "openapi": "https://x/openapi.json"},
        endpoints={"GET /catalog": "List products."},
        supported_rails=["tempo", "x402-base"],
    )
    assert body["name"] == "AgentScore Store"
    assert body["audience"] == "agents"
    assert body["supported_rails"] == ["tempo", "x402-base"]
    assert body["docs"]["llms"] == "https://x/llms.txt"
    assert body["endpoints"]["GET /catalog"] == "List products."


def test_build_merchant_index_json_extra_merges() -> None:
    from agentscore_commerce.discovery import build_merchant_index_json

    body = build_merchant_index_json(
        name="X",
        description="Y",
        docs={},
        endpoints={},
        supported_rails=[],
        extra={"compliance": {"min_age": 21}, "website": "https://x.example"},
    )
    assert body["compliance"] == {"min_age": 21}
    assert body["website"] == "https://x.example"


# ─────────────────────────────────────────────────────────────────────────────
# x_service_info_extension + x_payment_info_from_checkout (new openapi helpers)
# ─────────────────────────────────────────────────────────────────────────────


def test_x_service_info_extension_minimal() -> None:
    from agentscore_commerce.discovery import x_service_info_extension

    ext = x_service_info_extension(categories=["commerce", "wine"])
    assert ext == {"x-service-info": {"categories": ["commerce", "wine"]}}


def test_x_service_info_extension_with_docs() -> None:
    from agentscore_commerce.discovery import x_service_info_extension

    ext = x_service_info_extension(
        categories=["commerce"],
        docs={"human": "https://x.example/about"},
    )
    assert ext["x-service-info"]["docs"] == {"human": "https://x.example/about"}


def test_x_payment_info_extension_emits_auth_mode_payment_and_description() -> None:
    from agentscore_commerce.discovery import (
        XPaymentInfoFixedPrice,
        x_payment_info_extension,
    )

    ext = x_payment_info_extension(
        price=XPaymentInfoFixedPrice(currency="USD", amount="5.00"),
        protocols=[{"x402": {}}],
        description="Per-purchase fee.",
    )
    block = ext["x-payment-info"]
    assert block["authMode"] == "payment"
    assert block["description"] == "Per-purchase fee."
    assert block["price"] == {"mode": "fixed", "currency": "USD", "amount": "5.00"}


def test_x_payment_info_extension_dynamic_price() -> None:
    from agentscore_commerce.discovery import (
        XPaymentInfoDynamicPrice,
        x_payment_info_extension,
    )

    ext = x_payment_info_extension(
        price=XPaymentInfoDynamicPrice(currency="USD", min="0.01", max="5.00"),
        protocols=[],
    )
    assert ext["x-payment-info"]["price"] == {
        "mode": "dynamic",
        "currency": "USD",
        "min": "0.01",
        "max": "5.00",
    }


def test_x_payment_info_from_checkout_lists_protocols_per_rail() -> None:
    from agentscore_commerce.discovery import (
        XPaymentInfoFixedPrice,
        x_payment_info_from_checkout,
    )

    # Reuse _minimal_checkout from the file so the rails dict matches the test fixture.
    checkout = _minimal_checkout()
    ext = x_payment_info_from_checkout(
        checkout=checkout,
        price=XPaymentInfoFixedPrice(currency="USD", amount="1.00"),
        description="Per-call fee.",
    )
    block = ext["x-payment-info"]
    assert block["authMode"] == "payment"
    assert block["description"] == "Per-call fee."
    # _minimal_checkout has a tempo rail; protocol entry is `{"mpp": {"method": "tempo", "intent": "charge", ...}}`.
    assert any(p.get("mpp", {}).get("method") == "tempo" for p in block["protocols"])


def test_x_payment_info_from_checkout_covers_all_rail_types() -> None:
    from agentscore_commerce.checkout import Checkout, PricingResult
    from agentscore_commerce.discovery import (
        XPaymentInfoFixedPrice,
        x_payment_info_from_checkout,
    )
    from agentscore_commerce.payment import SolanaMppRailSpec, StripeRailSpec

    async def _pricing(ctx: Any) -> PricingResult:
        return PricingResult(amount_usd=1.0)

    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient="0x" + "00" * 20),
            "base": X402BaseRailSpec(recipient="0x" + "00" * 20),
            "stripe": StripeRailSpec(profile_id="profile_abc"),
            "solana": SolanaMppRailSpec(recipient="SoLaNaReCiPiEnT", token="EPjFWdd5..."),
        },
        url="https://x/purchase",
        compute_pricing=_pricing,
    )
    ext = x_payment_info_from_checkout(
        checkout=checkout,
        price=XPaymentInfoFixedPrice(currency="USD", amount="1.00"),
    )
    protos = ext["x-payment-info"]["protocols"]
    methods = [p.get("mpp", {}).get("method") or "x402" for p in protos]
    assert "stripe" in methods
    assert "tempo" in methods
    assert "solana" in methods
    assert "x402" in methods
    # Solana entry should include the `currency` from token
    solana_entry = next(p["mpp"] for p in protos if p.get("mpp", {}).get("method") == "solana")
    assert solana_entry["currency"] == "EPjFWdd5..."


def test_x_payment_info_from_checkout_merges_protocol_extras() -> None:
    from agentscore_commerce.discovery import (
        XPaymentInfoFixedPrice,
        x_payment_info_from_checkout,
    )

    checkout = _minimal_checkout()
    ext = x_payment_info_from_checkout(
        checkout=checkout,
        price=XPaymentInfoFixedPrice(currency="USD", amount="1.00"),
        protocol_extras={"tempo": {"client_command": "agentscore-pay pay --chain tempo"}},
    )
    tempo_entry = next(
        p["mpp"] for p in ext["x-payment-info"]["protocols"] if p.get("mpp", {}).get("method") == "tempo"
    )
    assert tempo_entry["client_command"] == "agentscore-pay pay --chain tempo"
