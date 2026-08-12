"""The operator handle: the identity durable merchant state keys on.

The whole reason it exists is that an ``opc_`` token is the WRONG key. It lives 24h and
rotates silently off a 90-day refresh, so anything keyed on the token instance is stranded
daily, and revoking a leaked token would forfeit a prepaid balance. The handle derives from
the account behind the token instead, and is pairwise per merchant.

It rides the gate's existing ``/v1/assess`` response rather than a lookup of its own, so
these tests pin the projection (which must refuse anything that is not a usable handle) and
the per-adapter read path across all six adapters.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

HANDLE = "oph_" + "a" * 40


# ---------------------------------------------------------------------------
# Projection: what counts as a usable handle
# ---------------------------------------------------------------------------


def _client():
    from agentscore_commerce.identity.core import AgentScoreCore

    return AgentScoreCore(api_key="as_test_key")


def test_projects_a_well_formed_handle() -> None:
    assert _client().project_operator_handle({"operator_handle": HANDLE}) == HANDLE


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        # Wallet path: no operator token was presented, so there is no account handle.
        {"decision": "allow"},
        # An unsalted or half-configured API must never hand back something that merely
        # looks usable. Anything that is not an `oph_` string reads as absent, because the
        # alternative is a merchant writing balance rows against a junk key.
        {"operator_handle": ""},
        {"operator_handle": "not_a_handle"},
        {"operator_handle": 12345},
        {"operator_handle": None},
    ],
)
def test_refuses_anything_that_is_not_a_usable_handle(raw: object) -> None:
    assert _client().project_operator_handle(raw) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Per-adapter read path
# ---------------------------------------------------------------------------


def test_asgi_reads_the_stashed_handle() -> None:
    from agentscore_commerce.identity.middleware import GATE_STATE_KEY, get_operator_handle

    request = MagicMock()
    request.state = MagicMock()
    setattr(request.state, GATE_STATE_KEY, {"operator_handle": HANDLE})
    assert get_operator_handle(request) == HANDLE


def test_asgi_returns_none_without_gate_state() -> None:
    from agentscore_commerce.identity.middleware import GATE_STATE_KEY, get_operator_handle

    request = MagicMock()
    request.state = MagicMock(spec=[])
    assert get_operator_handle(request) is None
    assert GATE_STATE_KEY  # key exists for the stash side


def test_fastapi_reads_the_stashed_handle() -> None:
    from agentscore_commerce.identity.fastapi import GATE_STATE_KEY, get_operator_handle

    request = MagicMock()
    request.state = MagicMock()
    setattr(request.state, GATE_STATE_KEY, {"operator_handle": HANDLE})
    assert get_operator_handle(request) == HANDLE


def test_aiohttp_reads_the_stashed_handle() -> None:
    from agentscore_commerce.identity.aiohttp import GATE_STATE_KEY, get_operator_handle

    request = {GATE_STATE_KEY: {"operator_handle": HANDLE}}
    assert get_operator_handle(request) == HANDLE  # type: ignore[arg-type]


def test_sanic_reads_the_stashed_handle() -> None:
    from agentscore_commerce.identity.sanic import GATE_STATE_ATTR, get_operator_handle

    request = MagicMock()
    request.ctx = MagicMock()
    setattr(request.ctx, GATE_STATE_ATTR, {"operator_handle": HANDLE})
    assert get_operator_handle(request) == HANDLE


def test_django_reads_the_stashed_handle() -> None:
    from agentscore_commerce.identity.django import get_operator_handle

    request = MagicMock(spec=["_agentscore_gate"])
    request._agentscore_gate = {"operator_handle": HANDLE}
    assert get_operator_handle(request) == HANDLE


def test_django_returns_none_without_gate_state() -> None:
    from agentscore_commerce.identity.django import get_operator_handle

    assert get_operator_handle(MagicMock(spec=[])) is None


def test_flask_returns_none_outside_an_application_context() -> None:
    # Same posture as the sibling signer-verdict accessor: no app context reads as "no
    # handle" rather than raising, so a merchant calling it off-request never 500s.
    from agentscore_commerce.identity.flask import get_operator_handle

    assert get_operator_handle() is None


def test_flask_reads_the_stashed_handle_in_context() -> None:
    flask = pytest.importorskip("flask")

    from agentscore_commerce.identity.flask import get_operator_handle

    app = flask.Flask(__name__)
    with app.test_request_context("/"):
        flask.g._agentscore_gate = {"operator_handle": HANDLE}
        assert get_operator_handle() == HANDLE
