from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from agentscore import Network as Network
from agentscore.types import SignerSanctions as SignerSanctions  # noqa: TC002 — runtime re-export for vendors

if TYPE_CHECKING:
    # AIP types live in the agentscore-py SDK. Type-only import (mirrors how the SDK owns the
    # AipSignatureMaterial / AipProvenance wire shapes); the gate forwards / surfaces them but
    # doesn't re-shape them into local dataclasses, same as policy_result / account_verification.
    from agentscore.types import AipProvenance, AipSignatureMaterial

# Reputation-API types (Activity, Classification, Identity, Reputation, ScoreDetail,
# Grade, ScoreStatus) live in agentscore-py — not re-exported here. Commerce SDK is
# scoped to gate + payment + 402 + discovery; reputation lookups belong to the
# AgentScore SDK. Import via `from agentscore import Activity, Classification, ...`.

DenialCode = Literal[
    "wallet_not_trusted",
    "missing_identity",
    "api_error",
    "payment_required",
    "identity_verification_required",
    # Wallet-signer binding: claimed X-Wallet-Address must resolve to the same operator as
    # the payment signer; wallet-auth is rejected on rails with no wallet signer.
    "wallet_signer_mismatch",
    "wallet_auth_requires_wallet_signing",
    # Credential is no longer valid (revoked or TTL-expired — the two cases share this
    # code deliberately; the API doesn't disclose which). The 401 body carries an
    # auto-minted session so the agent recovers without an API key.
    "token_expired",
    # The operator_token doesn't exist at all (typo, never minted, fabricated). Distinct
    # from token_expired — no auto-session is issued because the agent may have a
    # different valid token to try first. Agents should switch tokens or drop the header
    # to bootstrap a fresh session.
    "invalid_credential",
]


@dataclass
class AgentIdentity:
    """Identity of an agent — wallet address, operator token, or AIP Agent Identity Token."""

    address: str | None = None
    operator_token: str | None = None
    # Raw AIP Agent Identity Token (a JWT). When set, the gate has verified the token's RFC 9421
    # proof-of-possession at the edge as a fail-fast filter; ``check`` forwards it to ``/v1/assess``
    # as ``aip_token`` (with ``aip_signature``) for AUTHORITATIVE server-side re-verification of the
    # IdP signature + proof-of-possession + policy.
    aip_token: str | None = None
    # RFC 9421 signature material accompanying ``aip_token``, forwarded to ``/v1/assess`` as
    # ``aip_signature`` so the API re-verifies proof-of-possession itself (the edge is not trusted
    # as the authority). Always set together with ``aip_token``.
    aip_signature: AipSignatureMaterial | None = None


@dataclass
class AgentMemoryHint:
    """Cross-merchant pattern hint emitted on bootstrap denials.

    LLM-hosted agents should persist this to long-term memory so future AgentScore
    encounters skip the cold-start session flow. Contents are the pattern, not
    secrets. ``do_not_persist_in_memory`` names bearer credentials that belong in
    a credential store, not an LLM memory.
    """

    save_for_future_agentscore_gates: bool
    pattern_summary: str
    quickstart: str
    identity_check_endpoint: str
    # ``identity_paths`` may carry an optional ``agent_identity`` key (the AIP path) when the
    # merchant opted into AIP — emitted by ``build_agent_memory_hint`` alongside wallet / operator_token.
    identity_paths: dict[str, str]
    bootstrap: str
    do_not_persist_in_memory: list[str]
    persist_in_credential_store: list[str]
    list_wallets_endpoint: str | None = None
    # Issuers whose AIP Agent Identity Tokens this merchant accepts. Present only when the
    # merchant opted into AIP; an agent holding an AIT from one of these can present it via an
    # ``Agent-Identity`` header + RFC 9421 signature instead of bootstrapping a fresh credential.
    aip_trusted_issuers: list[str] | None = None


@dataclass
class DenialReason:
    """Reason a request was denied by the gate middleware."""

    code: DenialCode
    # Human-readable explanation. When None, denial_reason_to_body substitutes a per-code default.
    message: str | None = None
    decision: str | None = None
    reasons: list[str] = field(default_factory=list)
    verify_url: str | None = None
    session_id: str | None = None
    poll_secret: str | None = None
    poll_url: str | None = None
    agent_instructions: str | None = None
    # Cross-merchant memory hint. Emitted on bootstrap denials.
    agent_memory: AgentMemoryHint | None = None
    # Extra fields returned from ``CreateSessionOnMissing.on_before_session`` hook.
    # Merged into the default 403 body; custom ``on_denied`` handlers can spread
    # these into their own response shape (e.g. to include a merchant-minted
    # ``order_id``). See ``agentscore_commerce.identity.sessions.CreateSessionOnMissing``.
    extra: dict[str, Any] | None = None
    # Wallet-signer-match fields (populated only for wallet_signer_mismatch).
    claimed_operator: str | None = None
    actual_signer_operator: str | None = None
    expected_signer: str | None = None
    actual_signer: str | None = None
    linked_wallets: list[str] = field(default_factory=list)


VerifyWalletSignerKind = Literal[
    "pass",
    "wallet_signer_mismatch",
    "wallet_auth_requires_wallet_signing",
]


@dataclass
class VerifyWalletSignerResult:
    """Projected wallet-signer-match verdict surfaced inside :class:`SignerVerdict`.

    Kept as a dataclass so existing ``build_signer_mismatch_body(...)`` helpers
    consume it unchanged.
    """

    kind: VerifyWalletSignerKind
    claimed_operator: str | None = None
    signer_operator: str | None = None
    actual_signer_operator: str | None = None
    expected_signer: str | None = None
    actual_signer: str | None = None
    linked_wallets: list[str] = field(default_factory=list)
    claimed_wallet: str | None = None
    # JSON-encoded action copy (action + steps + user_message) populated on non-pass
    # kinds so the merchant can spread it directly into a 403 body and the agent sees
    # a concrete recovery path without a discovery-doc round trip.
    agent_instructions: str | None = None


@dataclass
class SignerVerdict:
    """Combined wallet-signer verdict surfaced by :meth:`AgentScoreCore.get_signer_verdict`.

    Both ``signer_match`` and ``signer_sanctions`` come through the gate's primary
    ``/v1/assess`` call (single round trip). ``signer_match`` describes the wallet-
    binding; ``signer_sanctions`` describes the OFAC SDN wallet-address check.

    Wallet-OFAC SDN enforcement is unconditional whenever a signer is in the request —
    a ``signer_sanctions`` hit OR an unavailable lookup already flips
    ``decision -> deny`` inside the gate before the handler runs. Merchant code
    typically only needs to read ``signer_match`` here for the wallet-binding verdict.
    """

    signer_match: VerifyWalletSignerResult | None = None
    signer_sanctions: SignerSanctions | None = None


# Canonical production AgentScore API; agent memory pointers are always hardcoded to this
# value regardless of how a given merchant configured their gate.
_CANONICAL_AGENTSCORE_API = "https://api.agentscore.com"


def build_agent_memory_hint(aip_trusted_issuers: list[str] | None = None) -> AgentMemoryHint:
    """Build the cross-merchant memory hint emitted on bootstrap denials.

    Memory pointers always point at the canonical production API — merchants must not be
    able to redirect the cross-merchant memory surface, since that would be a phishing
    vector for the verify URL.

    When ``aip_trusted_issuers`` is a non-empty list (the merchant opted into AIP), the hint
    advertises the AIP identity path and the accepted issuer allowlist so an agent holding an
    Agent Identity Token from one of them can present it directly.
    """
    api = _CANONICAL_AGENTSCORE_API
    accepts_aip = bool(aip_trusted_issuers)
    identity_paths = {
        "wallet": (
            "If paying from a crypto wallet you have used before on any AgentScore merchant, send "
            "`X-Wallet-Address: 0x...`. Shortest path; no token lifecycle to manage."
        ),
        "operator_token": (
            "For any rail (Stripe SPT, card, or a wallet you have not linked yet), send "
            "`X-Operator-Token: opc_...`. Reusable across merchants until the token expires."
        ),
    }
    if accepts_aip:
        identity_paths["agent_identity"] = (
            "If you hold an AIP Agent Identity Token from a trusted issuer (see aip_trusted_issuers), "
            "present it: send the JWT in an `Agent-Identity` header plus an RFC 9421 HTTP Message "
            "Signature (Signature-Input + Signature over @method @authority @path agent-identity, "
            'tag="agent-identity") signed with the token-bound cnf key. The merchant verifies it and '
            "applies its policy — no separate AgentScore credential needed."
        )
    return AgentMemoryHint(
        save_for_future_agentscore_gates=True,
        pattern_summary=(
            "AgentScore is a cross-merchant identity substrate for agentic commerce. Any endpoint that "
            "returns 403 missing_identity / identity_verification_required or 402 challenges "
            "referencing AgentScore uses "
            "the same identity system. One KYC works across all AgentScore merchants — no re-verification "
            "per site. Save this pattern (and the endpoints below) to persistent memory so future "
            "AgentScore encounters skip the cold-start session flow."
        ),
        quickstart="https://docs.agentscore.com/passport",
        identity_check_endpoint=f"{api}/v1/credentials",
        identity_paths=identity_paths,
        bootstrap=(
            "If you have neither a linked wallet nor a valid operator_token, follow the session/verify "
            "flow in the per-request `agent_instructions` block. This happens at most once per agent "
            "identity — after first verification, the operator_token and any subsequently used wallet "
            "are reusable everywhere."
        ),
        do_not_persist_in_memory=["operator_token", "poll_secret"],
        persist_in_credential_store=["operator_token"],
        aip_trusted_issuers=list(aip_trusted_issuers) if accepts_aip else None,
    )


@dataclass
class OperatorVerification:
    """Operator verification details from the assess response."""

    level: str = "none"
    operator_type: str | None = None
    verified_at: str | None = None


@dataclass
class PolicyCheck:
    """A single policy check from the assess response."""

    rule: str
    passed: bool
    required: Any = None
    actual: Any = None


@dataclass
class PolicyResult:
    """Policy evaluation result from the assess response."""

    all_passed: bool
    checks: list[PolicyCheck] = field(default_factory=list)


FailOpenInfraReason = Literal["quota_exceeded", "api_error", "network_timeout"]


def apply_degraded(state: dict[str, Any] | None, infra_reason: FailOpenInfraReason | str) -> None:
    """Mark a per-request gate state dict as degraded due to AgentScore-side infra failure.

    Per-adapter helpers resolve the state container in the framework's request-scoped store
    (``request.state`` on FastAPI, ``g`` on Flask, attribute on Django, mapping on aiohttp,
    ``request.ctx`` on Sanic, ``scope["state"]`` on ASGI) and hand that dict here. Keeps the
    contract — `degraded: True` + `infra_reason` — in one place across all 6 adapters.
    """
    if isinstance(state, dict):
        state["degraded"] = True
        state["infra_reason"] = infra_reason


@dataclass
class GateQuotaInfo:
    """Per-account assess quota observability captured from ``X-Quota-*`` response headers.

    Mirrors the SDK's ``QuotaInfo`` shape. Use to monitor approach-to-cap proactively
    (warn at 80%, alert at 95%). Numeric fields are ``None`` when the API didn't include
    the header (Enterprise / unlimited tiers).
    """

    limit: int | None
    used: int | None
    # ISO-8601 timestamp, or the literal string "never" for unlimited tiers.
    reset: str | None


@dataclass
class AssessResult:
    """Result from the AgentScore assess API."""

    allow: bool
    decision: str | None = None
    reasons: list[str] = field(default_factory=list)
    identity_method: str | None = None
    operator_verification: OperatorVerification | None = None
    # Account-level verification block (KYC level, age bracket, jurisdiction,
    # sanctions verdict); consumed by the A2A agent-card builder when emitting
    # per-card identity claims.
    account_verification: dict[str, Any] | None = None
    resolved_operator: str | None = None
    verify_url: str | None = None
    policy_result: PolicyResult | None = None
    # IdP provenance, present only when ``identity_method == "aip_token"`` — which issuer
    # attested the identity and the trust level it asserted. Surfaced as the SDK's raw
    # ``aip`` block (issuer/subject/trust_level/agent_provider/pop_verified); mirrors the
    # SDK's ``AssessResponse.aip``.
    aip: AipProvenance | None = None
    raw: dict[str, Any] | None = None
    # Per-account assess quota captured from X-Quota-* response headers. Absent on
    # Enterprise / unlimited tiers, or when the gate didn't call assess.
    quota: GateQuotaInfo | None = None
