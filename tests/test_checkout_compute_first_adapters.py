"""Tests for the per-framework `handle_*` adapter methods on ComputeFirstCheckout.

Each test exercises body parsing, header reading, and response shaping for the
specific framework, hitting the adapter code paths that the framework-neutral
`handle()` doesn't cover.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from agentscore_commerce.checkout_compute_first import (
    ComputeFirstCheckout,
    ComputeFirstRails,
    ComputeFirstWorkContext,
    WorkOutcome,
)
from agentscore_commerce.payment.rail_spec import TempoRailSpec


async def _run_one(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    return WorkOutcome(result_count=1, body={"matches": ["a"]})


async def _run_zero(_body: dict[str, Any], _ctx: ComputeFirstWorkContext) -> WorkOutcome:
    return WorkOutcome(result_count=0, body={"matches": [], "total": 0})


def _handler(name: str = "adapter_test", run: Any = _run_one) -> ComputeFirstCheckout:
    return ComputeFirstCheckout(
        name=name,
        url="https://api.example.com/search",
        unit_price_cents=1,
        rails=ComputeFirstRails(
            tempo=TempoRailSpec(recipient="0xtempo", testnet=True),
        ),
        x402_server=None,
        run_work=run,
    )


@pytest.mark.asyncio
async def test_handle_fastapi_with_pre_parsed_body() -> None:
    request = MagicMock()
    request.method = "POST"
    request.url = "https://api.example.com/search"
    request.headers = {}
    response = await _handler().handle_fastapi(request, body={"q": "fastapi"})
    assert response.status_code == 402


@pytest.mark.asyncio
async def test_handle_fastapi_zero_result_returns_200() -> None:
    request = MagicMock()
    request.method = "POST"
    request.url = "https://api.example.com/search"
    request.headers = {}
    response = await _handler(run=_run_zero).handle_fastapi(request, body={"q": "empty"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_handle_aiohttp_with_pre_parsed_body() -> None:
    request = MagicMock()
    request.method = "POST"
    request.url = "https://api.example.com/search"
    request.headers = {}
    response = await _handler().handle_aiohttp(request, body={"q": "aio"})
    assert response.status == 402


@pytest.mark.asyncio
async def test_handle_sanic_with_pre_parsed_body() -> None:
    request = MagicMock()
    request.method = "POST"
    request.url = "https://api.example.com/search"
    request.headers = {}
    response = await _handler().handle_sanic(request, body={"q": "sanic"})
    assert response.status == 402


def test_handle_flask_with_pre_parsed_body() -> None:
    from flask import Flask

    app = Flask(__name__)
    with app.test_request_context(
        path="/search",
        method="POST",
        json={"q": "flask"},
        headers={"content-type": "application/json"},
    ):
        from flask import request as flask_req

        response = _handler().handle_flask(flask_req, body={"q": "flask"})
        assert response.status_code == 402


@pytest.mark.asyncio
async def test_handle_fastapi_parses_body_when_omitted() -> None:
    async def _json() -> dict[str, Any]:
        return {"q": "auto"}

    request = MagicMock()
    request.method = "POST"
    request.url = "https://api.example.com/search"
    request.headers = {}
    request.json = _json
    response = await _handler().handle_fastapi(request)
    assert response.status_code == 402


@pytest.mark.asyncio
async def test_handle_fastapi_handles_invalid_json_body() -> None:
    async def _bad_json() -> dict[str, Any]:
        raise ValueError("malformed")

    request = MagicMock()
    request.method = "POST"
    request.url = "https://api.example.com/search"
    request.headers = {}
    request.json = _bad_json
    response = await _handler().handle_fastapi(request)
    assert response.status_code == 402


@pytest.mark.asyncio
async def test_handle_aiohttp_parses_body_when_omitted() -> None:
    async def _json() -> dict[str, Any]:
        return {"q": "auto-aio"}

    request = MagicMock()
    request.method = "POST"
    request.url = "https://api.example.com/search"
    request.headers = {}
    request.json = _json
    response = await _handler().handle_aiohttp(request)
    assert response.status == 402


def test_handle_flask_parses_body_when_omitted() -> None:
    from flask import Flask

    app = Flask(__name__)
    with app.test_request_context(
        path="/search",
        method="POST",
        json={"q": "flask-auto"},
        headers={"content-type": "application/json"},
    ):
        from flask import request as flask_req

        response = _handler().handle_flask(flask_req)
        assert response.status_code == 402


def test_handle_django_with_pre_parsed_body() -> None:
    import django
    from django.conf import settings as django_settings
    from django.http import HttpRequest

    if not django_settings.configured:
        django_settings.configure(
            DEBUG=True,
            DEFAULT_CHARSET="utf-8",
            ALLOWED_HOSTS=["*"],
        )
        django.setup()
    # Override ALLOWED_HOSTS so build_absolute_uri() works regardless of how an
    # earlier-running test (test_django.py) configured settings.
    django_settings.ALLOWED_HOSTS = ["*"]

    request = HttpRequest()
    request.method = "POST"
    request.path = "/search"
    request.META["SERVER_NAME"] = "testserver"
    request.META["SERVER_PORT"] = "80"
    request.META["wsgi.url_scheme"] = "http"
    response = _handler().handle_django(request, body={"q": "django"})
    assert response.status_code == 402
