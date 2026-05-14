"""``build_well_known_x402``: emits the x402scan v1 ``/.well-known/x402`` discovery shape.

x402scan accepts three discovery strategies (OpenAPI > ``/.well-known/x402`` > endpoint
probe). Most AgentScore merchants already publish a richer ``/.well-known/mpp.json``,
but x402scan's strict parser only reads the v1 shape, so we emit both. The two coexist
on different paths.

Spec (verbatim, x402scan)::

    {
        "version": 1,
        "resources": ["POST /api/route", ...]
    }

Resource entries are ``"METHOD /path"`` strings, not objects. Runtime 402 behavior is
authoritative over this static metadata.
"""

from __future__ import annotations

from typing import Any, TypedDict


class WellKnownX402Resource(TypedDict):
    """Entry in the ``resources`` list."""

    #: HTTP method, uppercase: ``GET | POST | PUT | PATCH | DELETE``.
    method: str
    #: Path with leading slash: ``/purchase``.
    path: str


def build_well_known_x402(*, resources: list[WellKnownX402Resource]) -> dict[str, Any]:
    """Emit the ``/.well-known/x402`` discovery body.

    ``resources`` is a list of ``{"method": ..., "path": ...}`` dicts; each becomes
    a ``"METHOD /path"`` entry in the output.
    """
    return {
        "version": 1,
        "resources": [f"{r['method'].upper()} {r['path']}" for r in resources],
    }
