"""AIP (Agentic Identity Protocol) token types and the claim contract.

An Agent Identity Token (AIT) is a JWT signed by an Identity Provider (IdP). It binds a
verified human's identity to the specific agent presenting it (via ``cnf``, RFC 7800) and
carries the trust level, authentication method, optional intent, and optional identity
claims the IdP attests to.

This module is the single source of truth for the claim shape on the verifier side. It
encodes the spec's required / recommended / optional claims plus the AgentScore extension
claims (sanctions, jurisdiction, structured id-verification, cross-merchant graph, payment
signer) we carry when we act as a compliance IdP.

Extensibility contract (per spec): the ``identity`` object is open. If a claim is present,
the IdP attests to it; verifiers ignore claims they don't recognize. Absence is the
"unknown" signal — IdPs do not ship ``None`` for "not checked".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict, TypeGuard, cast

# A JWK is a plain JSON object (RFC 7517). Node annotates this with ``jose``'s ``JWK``
# type; in Python a JWK is structurally a ``dict[str, Any]`` (matching the joserfc
# ``as_dict()`` shape used elsewhere in the SDK).
Jwk = dict[str, Any]

# Degree of human involvement in issuing this specific AIT.
TrustLevel = Literal["autonomous", "human_present", "human_confirmed"]

# Authentication Method Reference values (RFC 8176 / IANA AMR registry). Open set — these
# are the values relevant to agent identity; others are valid and pass through.
AmrValue = Literal["face", "fpt", "hwk", "otp", "pin", "pwd", "sms", "swk", "user", "mfa"]


class CnfClaim(TypedDict):
    """RFC 7800 confirmation claim: binds the AIT to the agent's signing key."""

    jwk: Jwk


class AgentClaim(TypedDict, total=False):
    """Agent metadata. ``provider`` is required; ``instance`` is recommended."""

    provider: str  # required
    instance: str


class AuthClaim(TypedDict, total=False):
    """How the user authorized THIS AIT (not prior authentication history)."""

    amr: list[AmrValue] | list[str]
    # When the user authenticated for this token (Unix seconds). Mirrors OIDC ``auth_time``.
    time: int


class IntentClaim(TypedDict, total=False):
    """What the agent intends to do. Optional; verifiers may require it for non-read actions."""

    actions: list[str]
    description: str


class PaymentSignerClaim(TypedDict, total=False):
    """AgentScore wallet-binding extension (orthogonal to ``cnf``, which binds the agent key)."""

    address: str  # required
    network: Literal["evm", "solana"]  # required
    # Relationship the IdP attests between signer and the operator graph.
    match: Literal["linked_operator", "claimed_operator"]


class PaymentClaim(TypedDict, total=False):
    signer: PaymentSignerClaim


class IdVerificationClaim(TypedDict, total=False):
    """Structured id-verification provenance (AgentScore compliance extension)."""

    provider: str
    method: str
    document_type: str
    verified_at: int


class LinkedWallet(TypedDict):
    """A same-operator sibling wallet attested in ``identity.linked_wallets``."""

    address: str
    network: Literal["evm", "solana"]


class IdentityClaim(TypedDict, total=False):
    """Identity claims (presence == IdP attestation).

    Spec-defined fields plus AgentScore compliance extension claims. Open by contract —
    unknown fields are allowed and ignored. (TypedDict cannot express the open
    ``[claim: string]: unknown`` index signature node declares; treat this as the
    documented subset, and rely on ``dict``-level access for any extension key.)
    """

    email: str
    email_verified: bool
    name: str
    phone: str
    phone_verified: bool
    age_over_18: bool
    age_over_21: bool
    id_verified: bool

    # --- AgentScore compliance extensions ---
    id_verification: IdVerificationClaim
    # ISO 3166-1 alpha-2, optionally with ISO 3166-2 subdivision (e.g. "US-CA").
    jurisdiction: str
    sanctions_clear: bool
    sanctions_checked_at: int
    sanctions_providers: list[str]
    linked_wallets: list[LinkedWallet]
    merchants_paid: int
    first_seen: int


class AitPayload(TypedDict, total=False):
    """The decoded AIT JWT payload.

    Required claims (``aip_version``, ``iss``, ``sub``, ``iat``, ``exp``, ``cnf``,
    ``agent``) are validated by :func:`validate_ait_payload`; ``total=False`` keeps the
    type usable for partially-decoded payloads before validation. Like node's interface,
    the payload is open — unrecognized claims pass through.
    """

    aip_version: str
    iss: str
    sub: str
    iat: int
    exp: int
    cnf: CnfClaim
    agent: AgentClaim
    trust_level: TrustLevel
    auth: AuthClaim
    intent: IntentClaim
    identity: IdentityClaim
    payment: PaymentClaim


class AitHeader(TypedDict, total=False):
    """The decoded AIT JWT header."""

    alg: str  # required
    typ: str
    kid: str


# Structural-validation failure reasons. Mirrors the reference ``AitValidationFailure`` union.
AitValidationFailure = Literal[
    "not_an_object",
    "missing_aip_version",
    "missing_iss",
    "missing_sub",
    "missing_iat",
    "missing_exp",
    "missing_cnf",
    "missing_agent_provider",
    "human_confirmed_without_amr",
]


@dataclass(frozen=True)
class AitValidationResult:
    """Result of :func:`validate_ait_payload`.

    Mirrors the reference discriminated union
    (``{ ok: true, payload } | { ok: false, reason }``). On success ``ok`` is ``True``
    and ``payload`` holds the validated claims; on failure ``ok`` is ``False`` and
    ``reason`` names the failed structural check.
    """

    ok: bool
    payload: AitPayload | None = None
    reason: AitValidationFailure | None = None


def _is_object(v: object) -> TypeGuard[dict[str, Any]]:
    """True for a JSON object (dict). Mirrors the reference ``isObject`` (excludes lists/None).

    Typed as a ``TypeGuard`` so callers narrow ``object`` → ``dict`` and ``.get(...)``
    access type-checks without per-call casts (the runtime check is plain ``isinstance``).
    """
    return isinstance(v, dict)


def _is_non_empty_string(v: object) -> bool:
    return isinstance(v, str) and len(v) > 0


def is_ait_shape(payload: object) -> bool:
    """Detect whether a decoded JWT payload is an AIT.

    Per spec, an AIT is discriminated by the presence of ``cnf`` + ``agent`` claims (not
    the ``typ`` header).
    """
    return _is_object(payload) and _is_object(payload.get("cnf")) and _is_object(payload.get("agent"))


def validate_ait_payload(payload: object) -> AitValidationResult:
    """Validate the structural contract of a decoded AIT payload.

    Confirms the required claims are present and well-typed, and enforces the one
    normative conditional in the spec: a ``human_confirmed`` token MUST carry at least one
    ``auth.amr`` value.

    This is shape/contract validation only — it does NOT verify signatures (that's the
    verifier pipeline) and does NOT apply trust policy (that's the gate / ``/v1/assess``).
    """
    if not _is_object(payload):
        return AitValidationResult(ok=False, reason="not_an_object")
    # ``payload`` is narrowed to ``dict[str, Any]`` by the TypeGuard above.
    p = payload
    if not _is_non_empty_string(p.get("aip_version")):
        return AitValidationResult(ok=False, reason="missing_aip_version")
    if not _is_non_empty_string(p.get("iss")):
        return AitValidationResult(ok=False, reason="missing_iss")
    if not _is_non_empty_string(p.get("sub")):
        return AitValidationResult(ok=False, reason="missing_sub")
    # ``bool`` is a subclass of ``int`` in Python; exclude it so a JSON boolean ``iat``
    # doesn't pass the numeric check (node's ``typeof === 'number'`` rejects booleans).
    if not _is_number(p.get("iat")):
        return AitValidationResult(ok=False, reason="missing_iat")
    if not _is_number(p.get("exp")):
        return AitValidationResult(ok=False, reason="missing_exp")
    cnf = p.get("cnf")
    if not _is_object(cnf) or not _is_object(cnf.get("jwk")):
        return AitValidationResult(ok=False, reason="missing_cnf")
    agent = p.get("agent")
    if not _is_object(agent) or not _is_non_empty_string(agent.get("provider")):
        return AitValidationResult(ok=False, reason="missing_agent_provider")

    # Normative conditional: human_confirmed requires auth.amr with at least one value.
    if p.get("trust_level") == "human_confirmed":
        auth = p.get("auth")
        amr = auth.get("amr") if _is_object(auth) else None
        if not isinstance(amr, list) or len(amr) == 0:
            return AitValidationResult(ok=False, reason="human_confirmed_without_amr")

    return AitValidationResult(ok=True, payload=cast("AitPayload", p))


def _is_number(v: object) -> bool:
    """True for an int/float that is not a bool.

    Node's ``typeof v === 'number'`` is true for any JS number and false for booleans.
    In Python ``bool`` subclasses ``int``, so exclude it to match: a JSON ``true``/``false``
    decoded into ``iat``/``exp`` must fail the numeric check exactly as it does in node.
    """
    return isinstance(v, int | float) and not isinstance(v, bool)


__all__ = [
    "AitHeader",
    "AitPayload",
    "AitValidationResult",
    "AmrValue",
    "IdentityClaim",
    "IntentClaim",
    "TrustLevel",
    "is_ait_shape",
    "validate_ait_payload",
]
