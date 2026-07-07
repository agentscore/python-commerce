"""Per-adapter coverage for ``get_signer_verdict`` — returns ``None`` when no signer was
extracted (operator-token-only paths, no payment credential, missing gate state).

The verdict is REQUEST-SCOPED: the gate stashes ``state["signer_verdict"]`` (projected from
THIS request's assess response) on the per-request state, and ``get_signer_verdict`` reads it
back from there rather than calling the shared core (which would race a concurrent same-wallet
request signing with a different wallet). These tests assert the read path; the concurrency
guarantee is covered by ``test_signer_verdict_request_scoped.py``."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

WALLET = "0x1111111111111111111111111111111111111111"


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------


def test_asgi_get_signer_verdict_returns_none_without_state() -> None:
    from agentscore_commerce.identity.middleware import get_signer_verdict

    request = MagicMock()
    request.scope = {"state": {}}
    assert get_signer_verdict(request) is None


def test_asgi_get_signer_verdict_returns_none_for_operator_token_only() -> None:
    from agentscore_commerce.identity.middleware import GATE_STATE_KEY, get_signer_verdict

    fake_client = MagicMock()
    request = MagicMock()
    request.scope = {"state": {GATE_STATE_KEY: {"client": fake_client, "wallet_address": None}}}
    assert get_signer_verdict(request) is None
    fake_client.get_signer_verdict.assert_not_called()


def test_asgi_get_signer_verdict_reads_request_scoped_verdict() -> None:
    from agentscore_commerce.identity.middleware import GATE_STATE_KEY, get_signer_verdict
    from agentscore_commerce.identity.types import SignerVerdict

    sentinel = SignerVerdict(signer_match=None, signer_sanctions={"kind": "clear"})
    fake_client = MagicMock()
    request = MagicMock()
    request.scope = {
        "state": {GATE_STATE_KEY: {"client": fake_client, "wallet_address": WALLET, "signer_verdict": sentinel}}
    }
    verdict = get_signer_verdict(request)
    assert verdict is sentinel
    # Request-scoped: read off per-request state, never the shared core.
    fake_client.get_signer_verdict.assert_not_called()


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------


def test_fastapi_get_signer_verdict_returns_none_for_operator_token_only() -> None:
    from agentscore_commerce.identity.fastapi import GATE_STATE_KEY, get_signer_verdict

    fake_client = MagicMock()
    request = MagicMock()
    setattr(request.state, GATE_STATE_KEY, {"client": fake_client, "wallet_address": None})
    assert get_signer_verdict(request) is None
    fake_client.get_signer_verdict.assert_not_called()


def test_fastapi_get_signer_verdict_reads_request_scoped_verdict() -> None:
    from agentscore_commerce.identity.fastapi import GATE_STATE_KEY, get_signer_verdict
    from agentscore_commerce.identity.types import SignerVerdict

    sentinel = SignerVerdict(signer_match=None, signer_sanctions=None)
    fake_client = MagicMock()
    request = MagicMock()
    setattr(
        request.state, GATE_STATE_KEY, {"client": fake_client, "wallet_address": WALLET, "signer_verdict": sentinel}
    )
    verdict = get_signer_verdict(request)
    assert verdict is sentinel
    fake_client.get_signer_verdict.assert_not_called()


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------


def test_flask_get_signer_verdict_returns_none_for_operator_token_only() -> None:
    from flask import Flask

    from agentscore_commerce.identity.flask import get_signer_verdict

    app = Flask(__name__)
    fake_client = MagicMock()
    with app.test_request_context("/"):
        from flask import g

        g._agentscore_gate = {"client": fake_client, "wallet_address": None}  # type: ignore[attr-defined]
        assert get_signer_verdict() is None
    fake_client.get_signer_verdict.assert_not_called()


def test_flask_get_signer_verdict_reads_request_scoped_verdict() -> None:
    from flask import Flask

    from agentscore_commerce.identity.flask import get_signer_verdict
    from agentscore_commerce.identity.types import SignerVerdict

    sentinel = SignerVerdict(signer_match=None, signer_sanctions=None)
    app = Flask(__name__)
    fake_client = MagicMock()
    with app.test_request_context("/"):
        from flask import g

        g._agentscore_gate = {  # type: ignore[attr-defined]
            "client": fake_client,
            "wallet_address": WALLET,
            "signer_verdict": sentinel,
        }
        verdict = get_signer_verdict()
    assert verdict is sentinel
    fake_client.get_signer_verdict.assert_not_called()


# ---------------------------------------------------------------------------
# Django
# ---------------------------------------------------------------------------


def test_django_get_signer_verdict_returns_none_for_operator_token_only() -> None:
    from agentscore_commerce.identity.django import get_signer_verdict

    fake_client = MagicMock()
    request = MagicMock()
    request._agentscore_gate = {"client": fake_client, "wallet_address": None}
    assert get_signer_verdict(request) is None
    fake_client.get_signer_verdict.assert_not_called()


def test_django_get_signer_verdict_reads_request_scoped_verdict() -> None:
    from agentscore_commerce.identity.django import get_signer_verdict
    from agentscore_commerce.identity.types import SignerVerdict

    sentinel = SignerVerdict(signer_match=None, signer_sanctions=None)
    fake_client = MagicMock()
    request = MagicMock()
    request._agentscore_gate = {"client": fake_client, "wallet_address": WALLET, "signer_verdict": sentinel}
    verdict = get_signer_verdict(request)
    assert verdict is sentinel
    fake_client.get_signer_verdict.assert_not_called()


# ---------------------------------------------------------------------------
# aiohttp
# ---------------------------------------------------------------------------


def test_aiohttp_get_signer_verdict_returns_none_for_operator_token_only() -> None:
    from agentscore_commerce.identity.aiohttp import GATE_STATE_KEY, get_signer_verdict

    fake_client = MagicMock()
    request = MagicMock()
    request.get.side_effect = lambda key: (
        {"client": fake_client, "wallet_address": None} if key == GATE_STATE_KEY else None
    )
    assert get_signer_verdict(request) is None
    fake_client.get_signer_verdict.assert_not_called()


def test_aiohttp_get_signer_verdict_reads_request_scoped_verdict() -> None:
    from agentscore_commerce.identity.aiohttp import GATE_STATE_KEY, get_signer_verdict
    from agentscore_commerce.identity.types import SignerVerdict

    sentinel = SignerVerdict(signer_match=None, signer_sanctions=None)
    fake_client = MagicMock()
    request = MagicMock()
    request.get.side_effect = lambda key: (
        {"client": fake_client, "wallet_address": WALLET, "signer_verdict": sentinel} if key == GATE_STATE_KEY else None
    )
    verdict = get_signer_verdict(request)
    assert verdict is sentinel
    fake_client.get_signer_verdict.assert_not_called()


# ---------------------------------------------------------------------------
# Sanic
# ---------------------------------------------------------------------------


def test_sanic_get_signer_verdict_returns_none_for_operator_token_only() -> None:
    from agentscore_commerce.identity.sanic import GATE_STATE_ATTR, get_signer_verdict

    fake_client = MagicMock()
    request = MagicMock()
    setattr(request.ctx, GATE_STATE_ATTR, {"client": fake_client, "wallet_address": None})
    assert get_signer_verdict(request) is None
    fake_client.get_signer_verdict.assert_not_called()


def test_sanic_get_signer_verdict_reads_request_scoped_verdict() -> None:
    from agentscore_commerce.identity.sanic import GATE_STATE_ATTR, get_signer_verdict
    from agentscore_commerce.identity.types import SignerVerdict

    sentinel = SignerVerdict(signer_match=None, signer_sanctions=None)
    fake_client = MagicMock()
    request = MagicMock()
    setattr(request.ctx, GATE_STATE_ATTR, {"client": fake_client, "wallet_address": WALLET, "signer_verdict": sentinel})
    verdict = get_signer_verdict(request)
    assert verdict is sentinel
    fake_client.get_signer_verdict.assert_not_called()


# ---------------------------------------------------------------------------
# AgentScoreCore.get_signer_verdict — projection branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected_kind"),
    [
        ("pass", "pass"),
        ("wallet_auth_requires_wallet_signing", "wallet_auth_requires_wallet_signing"),
    ],
)
def test_client_get_signer_verdict_projects_each_kind(kind: str, expected_kind: str) -> None:
    """Cover the branches in _project_signer_match (pass + wallet_auth_requires_wallet_signing)."""
    from unittest.mock import patch

    from agentscore_commerce.identity.core import AgentScoreCore

    client = AgentScoreCore(api_key="test-api-key")

    def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200
        resp.json = lambda: {
            "decision": "allow",
            "decision_reasons": [],
            "resolved_operator": "op_x",
            "signer_match": {
                "kind": kind,
                "claimed_operator": "op_x",
                "signer_operator": "op_x",
                "claimed_wallet": WALLET,
            },
        }
        return resp

    with patch.object(client._sync_client, "post", side_effect=fake_post):
        client.check(address=WALLET, signer={"address": WALLET, "network": "evm"})

    verdict = client.get_signer_verdict(WALLET)
    assert verdict is not None
    signer_match = verdict.signer_match
    assert signer_match is not None
    assert signer_match.kind == expected_kind
