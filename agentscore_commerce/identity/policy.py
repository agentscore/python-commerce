"""Per-product / per-tier compliance policy helpers.

A *policy* is a small bag of fields describing what identity the merchant
wants verified for a given resource:

- ``enforcement``:  ``"hard"`` (the regulated-goods path — 403 on miss) or ``"soft"``
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

from agentscore_commerce.errors import CheckoutValidationError

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
    blocked_jurisdictions: list[str]
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


# OFAC SDN denial reasons. These are strict-liability: soft enforcement may downgrade
# KYC / age / jurisdiction misses (the merchant accepts the order with a degraded
# identity_status), but it must NEVER swallow a sanctions deny — falsely settling for a
# sanctioned wallet is an OFAC violation regardless of the merchant's soft posture. The API
# emits `sanctions_flagged` in `decision_reasons` for BOTH the operator/wallet SDN hit and
# the payment-signer OFAC SDN hit; `sanctions_check_unavailable` is the fail-closed
# unavailable-lookup variant (a missing screen on a strict rail is also a hard deny). Match
# the canonical strings from `the AgentScore API`.
_SANCTIONS_DENIAL_REASONS: frozenset[str] = frozenset(
    {
        "sanctions_flagged",
        "sanctions_check_unavailable",
    }
)


def _is_sanctions_denial(body: Mapping[str, Any] | None) -> bool:
    """True when a gate denial body indicates an OFAC SDN sanctions hit (or unavailable screen).

    Inspects the flat denial body emitted by ``denial_reason_to_body``: a
    ``wallet_not_trusted`` (or signer-sanctions) deny carries the sanctions reason in
    ``reasons`` / ``decision_reasons``. Used by :func:`run_gate_with_enforcement` so soft
    mode can downgrade non-sanctions denials while leaving a sanctions deny terminal.
    """
    if not isinstance(body, dict):
        return False
    for key in ("reasons", "decision_reasons"):
        raw = body.get(key)
        if isinstance(raw, (list, tuple)) and any(r in _SANCTIONS_DENIAL_REASONS for r in raw):
            return True
    # The signer-sanctions SDN deny may also surface as a top-level error code.
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code in _SANCTIONS_DENIAL_REASONS


def build_gate_from_policy(
    policy: Mapping[str, Any] | None,
    *,
    api_key: str,
    base_url: str = "https://api.agentscore.com",
    create_session_on_missing: CreateSessionOnMissing | None = None,
    aip_trusted_issuers: list[str] | None = None,
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
        blocked_jurisdictions=policy.get("blocked_jurisdictions"),
        allowed_jurisdictions=policy.get("allowed_jurisdictions"),
        create_session_on_missing=create_session_on_missing,
        aip_trusted_issuers=aip_trusted_issuers,
    )


async def run_gate_with_enforcement(
    request: Any,
    gate: AgentScoreGate | None,
    *,
    enforcement: EnforcementMode | None,
) -> GateResult:
    """Run the gate respecting the enforcement mode.

    - ``gate is None`` or ``enforcement is None``: no gate fires; status="anonymous".
    - ``enforcement="hard"`` + gate denies (raises ``_GateDenialError`` or
      ``HTTPException``): status="denied"; caller returns ``denial_status`` +
      ``denial_body``.
    - ``enforcement="soft"`` + gate denies: swallow the denial; status="unverified".
    - gate accepts: status="verified".

    **Sanctions are never swallowed.** Soft mode is a commercial knob — it lets a merchant
    accept an order from an agent that didn't satisfy KYC / age / jurisdiction (stamping a
    degraded ``identity_status`` for ops). But an OFAC SDN sanctions deny is strict-liability:
    settling for a sanctioned wallet is a violation regardless of the merchant's posture. So a
    denial whose body indicates sanctions (:func:`_is_sanctions_denial`) returns
    ``status="denied"`` even under ``enforcement="soft"``; soft only downgrades the
    non-sanctions reasons.
    """
    if gate is None or enforcement is None:
        return GateResult(status="anonymous")

    # Local imports keep this module importable without FastAPI installed
    # for vendors who only use the policy types.
    from fastapi import HTTPException

    from agentscore_commerce.identity.fastapi import _GateDenialError

    try:
        await gate(request)
    except _GateDenialError as exc:
        # Post-flatten the gate raises a FLAT _GateDenialError (not HTTPException).
        # Convert it to a GateResult so soft mode can swallow the denial and hard mode
        # can propagate the flat body — same contract as the HTTPException path below.
        # A sanctions deny stays terminal in BOTH modes (see _is_sanctions_denial).
        if enforcement == "hard" or _is_sanctions_denial(exc.body):
            return GateResult(status="denied", denial_status=exc.status, denial_body=exc.body)
        return GateResult(status="unverified", denial_status=exc.status, denial_body=exc.body)
    except HTTPException as exc:
        body = exc.detail if isinstance(exc.detail, dict) else {"error": {"message": str(exc.detail)}}
        if enforcement == "hard" or _is_sanctions_denial(body):
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
