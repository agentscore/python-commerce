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
using ``AgentScoreGate(...)`` directly. Most merchants will implement shipping
checks adjacent to the gate per-request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentscore_commerce.identity.fastapi import AgentScoreGate
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
    instantiate; AgentScore's response cache lives on :class:`AgentScoreCore`
    inside the gate, scoped to the lifetime of this gate instance.
    """
    if policy is None:
        return None
    if not policy.get("enforcement"):
        return None
    # Lazy import — avoids circular import at package init time
    # (identity package init pulls policy → fastapi → payment.signer → identity).
    from agentscore_commerce.identity.fastapi import AgentScoreGate

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


def validate_shipping_against_policy(
    *,
    country: str,
    state: str,
    policy: Mapping[str, Any] | None,
    product_name: str | None = None,
    error_code: str = "unsupported_jurisdiction",
    error_action: str = "change_shipping_state",
    country_message: str | None = None,
    state_message: str | None = None,
) -> None:
    """Raise :class:`CheckoutValidationError` when shipping isn't allowed by the policy.

    One-call replacement for the ``if not shipping_country_allowed(...): raise``
    + ``if not shipping_state_allowed(...): raise`` boilerplate every goods
    merchant writes in their ``pre_validate`` hook.

    ``policy`` is a :class:`PolicyBlock`-shaped mapping (or ``None``); NULL
    policy means "ship anywhere" and the function is a no-op. The reason a
    location is excluded is **merchant-defined**: it might be regulatory
    (regulated goods + state allowlist), operational (no fulfillment partner),
    or commercial (fragility, fraud-rate-by-region, etc.) — the helper
    doesn't assume.

    ``product_name`` is the user-facing item name surfaced in the error
    message ("Cannot ship 'Wine 2020' to NY ..."). Omit for a generic message.

    ``error_code`` and ``error_action`` let merchants override the canonical
    denial codes if their consumer agents expect different shapes.

    ``country_message`` / ``state_message`` override the default messages
    verbatim (use these when the default phrasing isn't right for your
    consumer agents — e.g. you want to surface the regulatory reason
    explicitly, or you want the message in a different language).
    """
    # Local import dodges the circular: checkout depends on identity.policy.
    from agentscore_commerce.checkout import CheckoutValidationError

    item = f"'{product_name}'" if product_name else "this item"
    if not shipping_country_allowed(country, policy):
        raise CheckoutValidationError(
            code=error_code,
            message=country_message or f"We can't ship {item} to {country.upper() or '<unset>'}.",
            action=error_action,
        )
    if not shipping_state_allowed(state, country, policy):
        raise CheckoutValidationError(
            code=error_code,
            message=state_message or f"We can't ship {item} to {state.upper() or '<unset>'}.",
            action=error_action,
        )


__all__ = [
    "EnforcementMode",
    "GateResult",
    "IdentityStatus",
    "PolicyBlock",
    "build_gate_from_policy",
    "run_gate_with_enforcement",
    "shipping_country_allowed",
    "shipping_state_allowed",
    "validate_shipping_against_policy",
]
