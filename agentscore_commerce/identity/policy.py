"""Per-product / per-tier compliance policy helpers.

A *policy* is a small bag of fields describing what identity the merchant
wants verified for a given resource:

- ``enforcement``:  ``"hard"`` (today's wine path — 403 on miss) or ``"soft"``
                    (gate denial is swallowed; the order completes with a
                    degraded ``identity_status``). ``None`` = no gate at all.
- ``require_kyc`` / ``require_sanctions_clear`` / ``min_age``: passed through
  to :class:`AgentScoreGate`.
- ``allowed_jurisdictions``: buyer-verified country list (``["US", "CA", ...]``).
- ``allowed_shipping_countries`` / ``allowed_shipping_states``: optional
  shipping allowlists. State list is only enforced for US shipments.

This module ships three primitives:

1. :class:`PolicyBlock` — the typed shape.
2. :func:`build_gate_from_policy` — translate a block into an
   :class:`AgentScoreGate`.
3. :func:`run_gate_with_enforcement` — run the gate, swallow soft denials,
   return a structured :class:`GateResult`.

All three are additive — vendors that don't need per-product policy can keep
using ``AgentScoreGate(...)`` directly. The pattern was extracted from the
``agentscore/store`` merchant; see its ``store/routes/purchase.py`` for the
full per-request flow including shipping checks (which most merchants will
implement adjacent to the gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from agentscore_commerce.identity.fastapi import AgentScoreGate

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentscore_commerce.identity.sessions import CreateSessionOnMissing

EnforcementMode = Literal["hard", "soft"]
IdentityStatus = Literal["verified", "unverified", "anonymous", "denied"]


class PolicyBlock(TypedDict, total=False):
    """Compliance fields a merchant attaches per product / per tier.

    All fields are optional. Vendors usually source these from a database row
    (one column per field) and pass them straight through to
    :func:`build_gate_from_policy`.
    """

    enforcement: EnforcementMode
    require_kyc: bool
    require_sanctions_clear: bool
    min_age: int
    allowed_jurisdictions: list[str]
    allowed_shipping_countries: list[str]
    allowed_shipping_states: list[str]


@dataclass(frozen=True)
class GateResult:
    """Outcome of running a gate under an enforcement mode.

    - ``status="verified"``: gate accepted; identity is fully verified for the policy.
    - ``status="unverified"``: soft mode swallowed a gate denial; the agent had
      *some* identity but didn't meet the policy. Stamp this on the order so
      ops/analytics can tell apart soft passes from hard passes.
    - ``status="anonymous"``: no gate ran (policy was None / no enforcement).
    - ``status="denied"``: hard mode rejected; the caller must propagate the 403.
      ``denial_status`` and ``denial_body`` carry the original gate response so
      the caller can return it as-is.
    """

    status: IdentityStatus
    denial_status: int | None = None
    denial_body: dict[str, Any] | None = None


def build_gate_from_policy(
    policy: Mapping[str, Any] | None,
    *,
    api_key: str,
    base_url: str = "https://api.agentscore.sh",
    create_session_on_missing: CreateSessionOnMissing | None = None,
) -> AgentScoreGate | None:
    """Build a per-request :class:`AgentScoreGate` from a :class:`PolicyBlock`-shaped mapping.

    Returns ``None`` when ``policy`` is None, missing ``enforcement``, or has
    ``enforcement=None`` — the caller should treat that as "no gate; anonymous OK".

    Use a fresh gate per request rather than constructing once at module scope
    when policy varies per resource (e.g. per product). The gate is cheap to
    instantiate; AgentScore's response cache lives on :class:`GateClient`
    inside the gate, scoped to the lifetime of this gate instance.
    """
    if policy is None:
        return None
    if not policy.get("enforcement"):
        return None
    return AgentScoreGate(
        api_key=api_key,
        base_url=base_url,
        require_kyc=policy.get("require_kyc"),
        require_sanctions_clear=policy.get("require_sanctions_clear"),
        min_age=policy.get("min_age"),
        allowed_jurisdictions=policy.get("allowed_jurisdictions"),
        create_session_on_missing=create_session_on_missing,
    )


async def run_gate_with_enforcement(
    request: Any,
    gate: AgentScoreGate | None,
    *,
    enforcement: EnforcementMode | None,
) -> GateResult:
    """Run the gate respecting the enforcement mode.

    - ``gate is None`` or ``enforcement is None``: no gate fires; status="anonymous".
    - ``enforcement="hard"`` + gate raises HTTPException: status="denied"; caller
      must return ``denial_status`` + ``denial_body`` as the response.
    - ``enforcement="soft"`` + gate raises HTTPException: swallow the denial;
      status="unverified".
    - gate accepts: status="verified".
    """
    if gate is None or enforcement is None:
        return GateResult(status="anonymous")

    # Local import keeps this module importable without FastAPI installed
    # for vendors who only use the policy types.
    from fastapi import HTTPException

    try:
        await gate(request)
    except HTTPException as exc:
        body = exc.detail if isinstance(exc.detail, dict) else {"error": {"message": str(exc.detail)}}
        if enforcement == "hard":
            return GateResult(status="denied", denial_status=exc.status_code, denial_body=body)
        return GateResult(status="unverified", denial_status=exc.status_code, denial_body=body)
    return GateResult(status="verified")


def shipping_country_allowed(country: str, policy: Mapping[str, Any] | None) -> bool:
    """NULL policy / NULL allowlist → ship anywhere. Otherwise country must be in the list."""
    if policy is None:
        return True
    countries = policy.get("allowed_shipping_countries")
    if not countries:
        return True
    return country.upper() in {c.upper() for c in countries}


def shipping_state_allowed(state: str, country: str, policy: Mapping[str, Any] | None) -> bool:
    """US-state allowlist (e.g. wine).

    Only enforced for US shipments — non-US is governed by
    ``shipping_country_allowed`` independently.
    """
    if policy is None:
        return True
    states = policy.get("allowed_shipping_states")
    if not states or country.upper() != "US":
        return True
    return state.upper() in {s.upper() for s in states}


__all__ = [
    "EnforcementMode",
    "GateResult",
    "IdentityStatus",
    "PolicyBlock",
    "build_gate_from_policy",
    "run_gate_with_enforcement",
    "shipping_country_allowed",
    "shipping_state_allowed",
]
