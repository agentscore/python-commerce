"""Framework-agnostic AIP gate orchestration.

``verify_ait_request`` is the one call a framework adapter makes: hand it a parsed request
plus a :class:`~agentscore_commerce.aip.jwks.JwksCache`, and it returns the verified AIT claims
or a typed failure. The helpers here also map that failure onto the AIP wire contract — HTTP
status + error code + an RFC 9457 problem-details body — so every adapter renders denials
identically.

This layer does identity *verification* only (is this a real, key-bound AIT from a trusted
IdP?). Policy enrichment — sanctions, jurisdiction, cross-merchant graph — happens when the
merchant additionally feeds the verified claims to ``/v1/assess``; that's the gate's choice,
not something this module forces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentscore_commerce.aip.request import (
    build_verify_context_from_parts,
    build_verify_context_from_request,
)
from agentscore_commerce.aip.verify import VerifyAitFailureResult, verify_ait

if TYPE_CHECKING:
    from agentscore_commerce.aip.jwks import JwksCache
    from agentscore_commerce.aip.request import VerifyContextParts
    from agentscore_commerce.aip.types import AitPayload, TrustLevel
    from agentscore_commerce.aip.verify import (
        VerifiedAit,
        VerifyAitFailure,
        VerifyRequestContext,
    )


@dataclass
class AipGateOptions:
    """Options for the AIP gate's verify + trust-enforcement entry points."""

    jwks: JwksCache
    now: float | None = None
    max_skew_seconds: float | None = None
    # Minimum ``trust_level`` (autonomous < human_present < human_confirmed) the AIT must assert —
    # the spec's human-presence gate. Insufficient -> 403 weak_auth with ``required_trust_level``.
    # Enforced by :func:`evaluate_aip_request` / :func:`evaluate_aip_parts`. Unset = any trust level.
    require_trust_level: TrustLevel | None = None
    # Acceptable ``auth.amr`` methods (RFC 8176); the AIT must carry >=1. Insufficient -> 403
    # weak_auth with ``required_amr``. Unset = not enforced.
    require_amr: list[str] | None = None
    # Identity claims the endpoint needs — surfaced as ``required_claims`` on insufficient_claims
    # denials so the agent can self-correct. Advisory only (enforce by feeding the verified claims
    # to your own policy / ``/v1/assess``; this gate does identity + trust_level/amr).
    required_claims: list[str] | None = None
    # Trusted issuer URLs surfaced as ``trusted_issuers`` on untrusted_issuer denials.
    trusted_issuers: list[str] | None = None


@dataclass
class AipGateResult:
    """Outcome of a bare AIP verify (no trust enforcement).

    Discriminated on ``ok``: when true, ``ait`` holds the verified token; when false,
    ``failure`` holds the typed verify-failure reason.
    """

    ok: bool
    ait: VerifiedAit | None = None
    failure: VerifyAitFailure | None = None


async def _verify_from_context(ctx: VerifyRequestContext, opts: AipGateOptions) -> AipGateResult:
    """Verify the AIP credential from a pre-built context. Shared by all adapter entry points."""
    result = await verify_ait(ctx, jwks=opts.jwks, now=opts.now, max_skew_seconds=opts.max_skew_seconds)
    if isinstance(result, VerifyAitFailureResult):
        return AipGateResult(ok=False, failure=result.reason)
    return AipGateResult(ok=True, ait=result.ait)


async def verify_ait_request(req: Any, opts: AipGateOptions) -> AipGateResult:
    """Verify the AIP credential on a parsed Fetch-style request (Starlette / ASGI / web)."""
    return await _verify_from_context(build_verify_context_from_request(req), opts)


async def verify_ait_parts(
    parts: VerifyContextParts,
    opts: AipGateOptions,
) -> AipGateResult:
    """Verify the AIP credential from WSGI/ASGI-style parts (header map + method + url).

    ``parts`` carries ``method``, ``url``, ``headers`` (header map), and an optional ``authority``.
    """
    return await _verify_from_context(build_verify_context_from_parts(parts), opts)


def aip_error_code(failure: VerifyAitFailure) -> str:
    """Map an internal verify failure to the AIP wire error code (per spec error taxonomy)."""
    if failure in ("no_token", "pop_signature_missing"):
        return "agent_identity_required"
    if failure == "untrusted_issuer":
        return "untrusted_issuer"
    if failure == "expired_token":
        return "expired_token"
    if failure == "invalid_claims":
        return "insufficient_claims"
    if failure == "key_unavailable":
        # The IdP's JWKS could not be fetched/resolved — our infra couldn't reach a trusted
        # issuer, not a client-side auth failure. Distinct code so agents back off + retry
        # rather than uselessly re-signing.
        return "idp_unavailable"
    # malformed_token / idp_signature_invalid / pop_signature_invalid (and any default) -> invalid_signature.
    return "invalid_signature"


def aip_error_status(failure: VerifyAitFailure) -> int:
    """HTTP status for an AIP verify failure.

    503 when our infra couldn't reach the IdP (retryable), 403 for trust/claims,
    401 for auth-presence/signature.
    """
    if failure == "key_unavailable":
        return 503
    if failure in ("untrusted_issuer", "invalid_claims"):
        return 403
    return 401


def _aip_error_detail(failure: VerifyAitFailure) -> str:
    """Human-readable detail for the failure, for the problem-details body."""
    if failure == "no_token":
        return "No Agent-Identity token was presented."
    if failure == "pop_signature_missing":
        return (
            "The request is missing the RFC 9421 HTTP Message Signature that proves possession of the token-bound key."
        )
    if failure == "untrusted_issuer":
        return "The token's issuer is not in this service's trusted-issuer list."
    if failure == "expired_token":
        return "The Agent Identity Token has expired."
    if failure == "invalid_claims":
        return "The token is missing required claims for this endpoint."
    if failure == "malformed_token":
        return "The Agent-Identity header could not be parsed as an Agent Identity Token."
    if failure == "idp_signature_invalid":
        return "The identity provider's signature on the token failed verification."
    if failure == "pop_signature_invalid":
        return "The request signature did not match the key bound to the token."
    if failure == "key_unavailable":
        return "The identity provider's signing key could not be resolved."
    return "Token verification failed."


# RFC 9457 problem-details body for an AIP denial. Known fields (``type``/``title``/``status``/
# ``detail``) are always present; ``required_*`` / ``trusted_issuers`` escalation extensions ride
# alongside them in the same flat dict (the index-signature shape from node).
AipErrorBody = dict[str, Any]


@dataclass
class AipErrorRequirements:
    """Merchant requirements attached to an AIP escalation body so the agent can self-correct."""

    trusted_issuers: list[str] | None = None
    required_claims: list[str] | None = None
    required_trust_level: TrustLevel | None = None
    required_amr: list[str] | None = None


def build_aip_error_body(
    failure: VerifyAitFailure,
    requirements: AipErrorRequirements | None = None,
) -> AipErrorBody:
    """Build an RFC 9457 problem-details body for an AIP verify failure.

    Adapters serialize this as ``application/problem+json`` with :func:`aip_error_status`.
    Optionally carries the merchant's requirements — ``trusted_issuers`` on untrusted_issuer;
    ``required_claims`` / ``required_trust_level`` / ``required_amr`` on insufficient_claims —
    so the agent learns what would satisfy the gate.
    """
    code = aip_error_code(failure)
    body: AipErrorBody = {
        "type": f"urn:aip:error:{code}",
        "title": code.replace("_", " "),
        "status": aip_error_status(failure),
        "detail": _aip_error_detail(failure),
    }
    if requirements is not None:
        if code == "untrusted_issuer" and requirements.trusted_issuers:
            body["trusted_issuers"] = requirements.trusted_issuers
        if code == "insufficient_claims":
            if requirements.required_claims:
                body["required_claims"] = requirements.required_claims
            if requirements.required_trust_level is not None:
                body["required_trust_level"] = requirements.required_trust_level
            if requirements.required_amr:
                body["required_amr"] = requirements.required_amr
    return body


def aip_policy_deny_code(code: str) -> tuple[str, int]:
    """Map an AgentScore *policy-deny* code to its spec AIP error code + HTTP status.

    A ``/v1/assess`` decision, NOT a verify failure: the policy-side counterpart to
    :func:`aip_error_code` (which maps the verify-FAILURE taxonomy).
    On the AIT-input path a denied ``/v1/assess`` decision surfaces as one of these AgentScore
    codes; the spec's fixed error set expresses each as:
      - ``token_expired`` -> ``expired_token`` (401)
      - ``invalid_credential`` -> ``invalid_signature`` (401)
      - ``api_error`` -> ``idp_unavailable`` (503, transient — the claims couldn't be evaluated)
      - everything else (compliance: ``wallet_not_trusted`` + ``sanctions_flagged`` /
        ``age_insufficient`` / ``jurisdiction_restricted`` / ``kyc_*``) -> ``insufficient_claims``
        (403): the AIT did not attest (or attested a failing value for) the required compliance claim.
    """
    if code == "token_expired":
        return ("expired_token", 401)
    if code == "invalid_credential":
        return ("invalid_signature", 401)
    if code == "api_error":
        return ("idp_unavailable", 503)
    return ("insufficient_claims", 403)


def build_aip_policy_deny_body(
    code: str,
    reasons: list[str] | None,
    body: dict[str, Any],
    requirements: AipErrorRequirements | None = None,
) -> dict[str, Any]:
    """Wrap an AgentScore AIT-path denial body in the RFC 9457 + AIP-spec superset.

    Reuses :func:`build_aip_error_body`'s SHAPE convention (``type``/``title``/``status``/``detail``
    + escalation extensions) but for the *policy-deny* case — a verified AIT that ``/v1/assess``
    then denied — which carries an AgentScore compliance/credential code, not a verify-failure
    reason.

    The result is a SUPERSET: the canonical ``{ error, agent_instructions, ... }`` body is spread
    in verbatim (so existing consumers keep parsing ``error.code``), with the RFC 9457 envelope
    layered on top. ``detail`` names the precise AgentScore reason(s); ``error.code`` stays the
    AgentScore code. Escalation fields (``required_claims`` / ``required_trust_level`` /
    ``required_amr`` on insufficient_claims, ``trusted_issuers`` on untrusted_issuer) ride along
    when known.
    """
    spec_code, status = aip_policy_deny_code(code)
    reason_list = reasons or []
    if spec_code == "insufficient_claims":
        if reason_list:
            detail = (
                "The Agent Identity Token did not attest a passing value for the required compliance "
                f"claim(s). AgentScore decision: {code} ({', '.join(reason_list)})."
            )
        else:
            detail = (
                "The Agent Identity Token did not satisfy the merchant's compliance policy. "
                f"AgentScore decision: {code}."
            )
    else:
        detail = f"AgentScore decision: {code}."
    superset: dict[str, Any] = {
        "type": f"urn:aip:error:{spec_code}",
        "title": spec_code.replace("_", " "),
        "status": status,
        "detail": detail,
    }
    # Escalation extensions, scoped exactly as the spec mandates: ``required_claims`` /
    # ``required_trust_level`` / ``required_amr`` on insufficient_claims. ``trusted_issuers``
    # belongs to untrusted_issuer — a VERIFY failure that never reaches the policy-deny path — so it
    # is not emitted here (the edge-deny ``build_aip_error_body`` owns that one).
    if requirements is not None and spec_code == "insufficient_claims":
        if requirements.required_claims:
            superset["required_claims"] = requirements.required_claims
        if requirements.required_trust_level is not None:
            superset["required_trust_level"] = requirements.required_trust_level
        if requirements.required_amr:
            superset["required_amr"] = requirements.required_amr
    # Spread the RFC 9457 envelope LAST so `type` / `title` / `status` / `detail` (and the
    # escalation extensions) always win: `body` carries merchant `extra` passthrough fields, and a
    # buggy or malicious hook must not clobber the problem+json envelope — or the HTTP status the
    # caller derives from it. The rich AgentScore fields (`error`, `agent_instructions`, `reasons`,
    # ...) don't collide with the envelope, so they still ride along verbatim.
    return {**body, **superset}


# Trust-level ordering: a token satisfies a requirement when its level >= the required level.
_TRUST_RANK: dict[str, int] = {"autonomous": 0, "human_present": 1, "human_confirmed": 2}


def check_trust_requirements(
    payload: AitPayload,
    required_trust_level: TrustLevel | None = None,
    required_amr: list[str] | None = None,
) -> str | None:
    """Check a verified AIT against the gate's ``trust_level`` / ``auth.amr`` requirements.

    This is the spec's human-presence gate. Returns a detail string when insufficient
    (-> weak_auth), else ``None``. ``payload`` is the AIT payload; ``trust_level`` and
    ``auth.amr`` are read off it.
    """
    if required_trust_level is not None:
        # Mirror node's `payload.trust_level ?? 'autonomous'`: substitute the default only for an
        # absent claim (None), NOT for a falsy-but-present value like '' (which stays verbatim so
        # the detail message echoes exactly what the token asserted).
        raw_level = payload.get("trust_level")
        token_level = raw_level if raw_level is not None else "autonomous"
        have = _TRUST_RANK.get(token_level, 0) if isinstance(token_level, str) else 0
        need = _TRUST_RANK.get(required_trust_level, 0)
        if have < need:
            return (
                f"This endpoint requires trust_level '{required_trust_level}'; "
                f"the token asserts '{token_level}'. "
                "Re-mint an AIT at the required trust level (human confirmation)."
            )
    if required_amr is not None and len(required_amr) > 0:
        auth = payload.get("auth")
        raw_amr = auth.get("amr") if isinstance(auth, dict) else None
        amr = raw_amr if isinstance(raw_amr, list) else []
        if not any(m in required_amr for m in amr):
            have_amr = ", ".join(amr) or "none"
            return (
                f"This endpoint requires an authentication method in [{', '.join(required_amr)}]; "
                f"the token carries [{have_amr}]."
            )
    return None


def build_aip_weak_auth_body(
    *,
    detail: str,
    required_trust_level: TrustLevel | None = None,
    required_amr: list[str] | None = None,
    trusted_issuers: list[str] | None = None,
) -> AipErrorBody:
    """Build an RFC 9457 ``weak_auth`` body for a token that failed the trust_level / auth.amr gate."""
    body: AipErrorBody = {
        "type": "urn:aip:error:weak_auth",
        "title": "weak auth",
        "status": 403,
        "detail": detail,
    }
    if required_trust_level is not None:
        body["required_trust_level"] = required_trust_level
    if required_amr is not None and len(required_amr) > 0:
        body["required_amr"] = required_amr
    if trusted_issuers is not None and len(trusted_issuers) > 0:
        body["trusted_issuers"] = trusted_issuers
    return body


@dataclass
class AipGateEvaluation:
    """A verified AIT, or a ready-to-render RFC 9457 denial body.

    Discriminated on ``ok``: when true, ``ait`` holds the verified token; when false, ``body``
    holds the problem-details body (HTTP status lives on ``body['status']``).
    """

    ok: bool
    ait: VerifiedAit | None = None
    body: AipErrorBody | None = None


def _requirements_from_options(opts: AipGateOptions) -> AipErrorRequirements:
    """Collect the merchant's requirements from gate options, for attaching to denial bodies."""
    return AipErrorRequirements(
        trusted_issuers=opts.trusted_issuers,
        required_claims=opts.required_claims,
        required_trust_level=opts.require_trust_level,
        required_amr=opts.require_amr,
    )


async def _evaluate_from_context(ctx: VerifyRequestContext, opts: AipGateOptions) -> AipGateEvaluation:
    """Verify the AIP credential AND enforce the gate's trust_level / auth.amr requirement.

    The standalone-adapter counterpart to :func:`verify_ait_request`, in one call. Returns
    the verified AIT, or an RFC 9457 denial body (a verify failure -> its wire code; trust
    insufficient -> weak_auth) carrying the merchant's ``required_*`` / ``trusted_issuers``
    so the agent can self-correct.
    """
    result = await verify_ait(ctx, jwks=opts.jwks, now=opts.now, max_skew_seconds=opts.max_skew_seconds)
    if isinstance(result, VerifyAitFailureResult):
        return AipGateEvaluation(ok=False, body=build_aip_error_body(result.reason, _requirements_from_options(opts)))
    weak = check_trust_requirements(result.ait.payload, opts.require_trust_level, opts.require_amr)
    if weak is not None:
        return AipGateEvaluation(
            ok=False,
            body=build_aip_weak_auth_body(
                detail=weak,
                required_trust_level=opts.require_trust_level,
                required_amr=opts.require_amr,
                trusted_issuers=opts.trusted_issuers,
            ),
        )
    return AipGateEvaluation(ok=True, ait=result.ait)


async def evaluate_aip_request(req: Any, opts: AipGateOptions) -> AipGateEvaluation:
    """Verify + trust-enforce on a parsed Fetch-style request (Starlette / ASGI / web adapters)."""
    return await _evaluate_from_context(build_verify_context_from_request(req), opts)


async def evaluate_aip_parts(
    parts: VerifyContextParts,
    opts: AipGateOptions,
) -> AipGateEvaluation:
    """Verify + trust-enforce from WSGI/ASGI-style parts (header map + method + url).

    ``parts`` carries ``method``, ``url``, ``headers`` (header map), and an optional ``authority``.
    """
    return await _evaluate_from_context(build_verify_context_from_parts(parts), opts)


__all__ = [
    "AipErrorBody",
    "AipErrorRequirements",
    "AipGateEvaluation",
    "AipGateOptions",
    "AipGateResult",
    "aip_error_code",
    "aip_error_status",
    "aip_policy_deny_code",
    "build_aip_error_body",
    "build_aip_policy_deny_body",
    "build_aip_weak_auth_body",
    "check_trust_requirements",
    "evaluate_aip_parts",
    "evaluate_aip_request",
    "verify_ait_parts",
    "verify_ait_request",
]
