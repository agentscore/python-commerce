"""Cross-adapter `get_gate_quota_info` tests.

Each adapter exposes a `get_gate_quota_info(request)` function that reads the per-account
assess quota stashed on gate state during evaluate. These tests exercise the
read-path-only contract: prime the framework-specific state container with a fake quota
and verify the helper returns it. Full end-to-end quota propagation through evaluate is
covered by the per-adapter integration tests' fail-open / allow paths.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agentscore_commerce.identity.types import GateQuotaInfo

QUOTA = GateQuotaInfo(limit=1000, used=780, reset="2026-06-01T00:00:00Z")


def test_fastapi_get_gate_quota_info_reads_state() -> None:
    from agentscore_commerce.identity.fastapi import GATE_STATE_KEY, get_gate_quota_info

    request = MagicMock()
    request.state = MagicMock()
    setattr(request.state, GATE_STATE_KEY, {"quota": QUOTA})
    assert get_gate_quota_info(request) is QUOTA


def test_fastapi_get_gate_quota_info_returns_none_when_absent() -> None:
    from agentscore_commerce.identity.fastapi import GATE_STATE_KEY, get_gate_quota_info

    request = MagicMock()
    request.state = MagicMock()
    setattr(request.state, GATE_STATE_KEY, {"degraded": False})  # state present, quota absent
    assert get_gate_quota_info(request) is None


def test_flask_get_gate_quota_info_reads_g() -> None:
    from flask import Flask

    from agentscore_commerce.identity.flask import get_gate_quota_info

    app = Flask(__name__)
    with app.test_request_context():
        from flask import g

        g._agentscore_gate = {"quota": QUOTA}
        assert get_gate_quota_info() is QUOTA


def test_flask_get_gate_quota_info_returns_none_when_absent() -> None:
    from flask import Flask

    from agentscore_commerce.identity.flask import get_gate_quota_info

    app = Flask(__name__)
    with app.test_request_context():
        assert get_gate_quota_info() is None


def test_django_get_gate_quota_info_reads_attr() -> None:
    from agentscore_commerce.identity.django import get_gate_quota_info

    request = MagicMock()
    request._agentscore_gate = {"quota": QUOTA}
    assert get_gate_quota_info(request) is QUOTA


def test_django_get_gate_quota_info_returns_none_when_absent() -> None:
    from agentscore_commerce.identity.django import get_gate_quota_info

    request = MagicMock()
    request._agentscore_gate = None  # type: ignore[assignment]
    # Attribute may not exist at all — getattr default.
    delattr(request, "_agentscore_gate")
    assert get_gate_quota_info(request) is None


def test_aiohttp_get_gate_quota_info_reads_dict() -> None:
    from agentscore_commerce.identity.aiohttp import GATE_STATE_KEY, get_gate_quota_info

    request = MagicMock()
    request.get = lambda key, default=None: {GATE_STATE_KEY: {"quota": QUOTA}}.get(key, default)
    assert get_gate_quota_info(request) is QUOTA


def test_aiohttp_get_gate_quota_info_returns_none_when_absent() -> None:
    from agentscore_commerce.identity.aiohttp import get_gate_quota_info

    request = MagicMock()
    request.get = lambda _key, default=None: default
    assert get_gate_quota_info(request) is None


def test_sanic_get_gate_quota_info_reads_ctx() -> None:
    from agentscore_commerce.identity.sanic import GATE_STATE_ATTR, get_gate_quota_info

    request = MagicMock()
    request.ctx = MagicMock()
    setattr(request.ctx, GATE_STATE_ATTR, {"quota": QUOTA})
    assert get_gate_quota_info(request) is QUOTA


def test_sanic_get_gate_quota_info_returns_none_when_absent() -> None:
    from agentscore_commerce.identity.sanic import get_gate_quota_info

    request = MagicMock()
    request.ctx = MagicMock(spec=[])  # no GATE_STATE_ATTR set
    assert get_gate_quota_info(request) is None


def test_middleware_get_gate_quota_info_reads_scope() -> None:
    from agentscore_commerce.identity.middleware import GATE_STATE_KEY, get_gate_quota_info

    request = MagicMock()
    request.scope = {"state": {GATE_STATE_KEY: {"quota": QUOTA}}}
    assert get_gate_quota_info(request) is QUOTA


def test_middleware_get_gate_quota_info_returns_none_when_absent() -> None:
    from agentscore_commerce.identity.middleware import get_gate_quota_info

    request = MagicMock()
    request.scope = {}
    assert get_gate_quota_info(request) is None
