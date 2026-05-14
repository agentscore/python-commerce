"""skill.md renderer — agentskills.io-compatible agent-discovery surface.

Emits a YAML-frontmatter + markdown body manifest describing a merchant's agent-facing
contract: payment rails, compatible clients per rail, identity requirements as outcomes,
shipping policy, endpoints, triggers, support links.

Renders strictly agent-facing data — no ``fail_open``, no mount-strategy names, no KYC
vendor names, no defense parameters, no idempotency construction. Internal posture stays
in merchant runtime config.

Spec compliance (https://agentskills.io/specification):
  * ``name`` validated against the spec regex (lowercase alphanumeric + hyphens, no
    leading/trailing/consecutive hyphens, ≤64 chars).
  * ``description`` length capped at 1024.
  * ``metadata`` values always emitted as quoted strings.
  * ``description`` (and other user scalars) double-quoted to defuse the colon /
    newline / quote pitfall the spec explicitly warns about.

The compatible-clients-per-rail table sources from the same SDK constant
(``compatible_clients_by_rails`` in ``challenge.agent_instructions``) that drives the live
402 body's ``compatible_clients`` field — single source of truth across surfaces.
"""

import re
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from agentscore_commerce.challenge.agent_instructions import (
    RailKey,
    compatible_clients_by_rails,
)

__all__ = [
    "RailKey",
    "SkillMdEndpoint",
    "SkillMdIdentityRequirements",
    "SkillMdLink",
    "SkillMdShippingPolicy",
    "build_skill_md",
    "compatible_clients_by_rails",
]

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class SkillMdEndpoint(TypedDict):
    """One row in the ``## Endpoints`` table."""

    method: HttpMethod
    path: str
    auth_required: bool
    description: str


class SkillMdIdentityRequirements(TypedDict, total=False):
    """Agent-observable identity requirements only (kyc / age / jurisdictions / sanctions).

    Internal posture (``fail_open``, mount strategy, KYC vendor) is intentionally not
    part of this shape — agents act on outcomes, not implementation.
    """

    kyc_required: bool
    min_age: int | None
    allowed_jurisdictions: list[str] | None
    sanctions_clear: bool


class SkillMdShippingPolicy(TypedDict, total=False):
    allowed_countries: list[str] | None
    blocked_states: list[str] | None


class SkillMdLink(TypedDict):
    label: str
    url: str


# Internal aggregate so the 11 private section helpers don't need 14 kwargs each;
# `build_skill_md` flattens the public API and constructs this once.
@dataclass(frozen=True)
class _SkillCtx:
    name: str
    description: str
    homepage: str
    merchant_name: str
    accepted_rails: list[RailKey]
    endpoints: list[SkillMdEndpoint]
    triggers: list[str]
    version: str | int = 1
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: str | None = None
    metadata: dict[str, str | int] = field(default_factory=dict)
    tagline: str | None = None
    intro: str | None = None
    files: list[SkillMdLink] = field(default_factory=list)
    compatible_clients: dict[str, list[str]] | None = None
    identity: SkillMdIdentityRequirements | None = None
    identity_bootstrap_url: str | None = None
    shipping: SkillMdShippingPolicy | None = None
    onboarding_steps: list[str] = field(default_factory=list)
    support_links: list[SkillMdLink] = field(default_factory=list)
    refresh_footer: bool = True


_RAIL_LABELS: dict[str, str] = {
    "tempo_mpp": "MPP on Tempo",
    "x402_base": "x402 on Base",
    "solana_mpp": "MPP on Solana",
    "stripe": "Stripe Shared Payment Token",
}

_RAIL_NOTES: dict[str, str] = {
    "tempo_mpp": (
        "USDC. Use `agentscore-pay pay --chain tempo` (or `tempo request`); "
        "MPP credential goes in `Authorization: Payment`."
    ),
    "x402_base": (
        "USDC (EIP-3009). Use `agentscore-pay pay --chain base`; X-Payment header carries the signed credential."
    ),
    "solana_mpp": (
        "USDC (SPL). Use `agentscore-pay pay --chain solana`; MPP credential goes in `Authorization: Payment`."
    ),
    "stripe": (
        "Card via Link wallet. Use `@stripe/link-cli` — `agentscore-pay` emits the "
        "handoff hint when this rail is picked."
    ),
}

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NAME_MAX = 64
_DESCRIPTION_MAX = 1024
_COMPATIBILITY_MAX = 500


def _validate(input: _SkillCtx) -> None:
    n = input.name
    if not n or len(n) > _NAME_MAX:
        raise ValueError(f"build_skill_md: name must be 1-{_NAME_MAX} characters (got {len(n) if n else 0})")
    if not _NAME_RE.match(n):
        raise ValueError(
            f'build_skill_md: name "{n}" is invalid — must be lowercase alphanumeric and hyphens, '
            "no leading/trailing/consecutive hyphens (agentskills.io spec)"
        )
    if not input.description:
        raise ValueError("build_skill_md: description is required and must be non-empty (agentskills.io spec)")
    if len(input.description) > _DESCRIPTION_MAX:
        raise ValueError(
            f"build_skill_md: description must be ≤{_DESCRIPTION_MAX} characters (got {len(input.description)})"
        )
    if input.compatibility and len(input.compatibility) > _COMPATIBILITY_MAX:
        raise ValueError(
            f"build_skill_md: compatibility must be ≤{_COMPATIBILITY_MAX} characters (got {len(input.compatibility)})"
        )


def _quote_yaml(value: str) -> str:
    r"""YAML double-quoted scalar with backslash, double-quote, and newlines escaped.

    The agentskills.io spec calls out unquoted colons in ``description`` as the most
    common parse failure across clients; emit every user-supplied scalar quoted to
    be safe.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _table_cell(value: str) -> str:
    r"""Sanitize a string for a markdown table cell.

    Escape backslashes first (so existing ``\\`` aren't treated as escapes), then escape
    pipes (which would otherwise terminate the cell).
    """
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _frontmatter(input: _SkillCtx) -> str:
    lines = ["---", f"name: {input.name}", f"description: {_quote_yaml(input.description)}"]
    if input.license:
        lines.append(f"license: {_quote_yaml(input.license)}")
    if input.compatibility:
        lines.append(f"compatibility: {_quote_yaml(input.compatibility)}")
    if input.allowed_tools:
        lines.append(f"allowed-tools: {_quote_yaml(input.allowed_tools)}")

    meta: list[tuple[str, str]] = [
        ("version", str(input.version)),
        ("homepage", input.homepage),
    ]
    for k, v in input.metadata.items():
        if k in ("version", "homepage"):
            continue
        meta.append((k, str(v)))
    lines.append("metadata:")
    for k, v in meta:
        lines.append(f"  {k}: {_quote_yaml(v)}")
    lines.append("---")
    return "\n".join(lines)


def _title_block(input: _SkillCtx) -> str:
    parts = [f"# {input.merchant_name}"]
    if input.tagline:
        parts.append(f"_{input.tagline}_")
    if input.intro:
        parts.append(input.intro)
    return "\n\n".join(parts)


def _important_files(input: _SkillCtx) -> str:
    skill_url = f"{input.homepage.rstrip('/')}/skill.md"
    rows = [
        "| File | URL |",
        "|------|-----|",
        f"| **SKILL.md** (this file) | `{skill_url}` |",
    ]
    for f in input.files:
        rows.append(f"| {_table_cell(f['label'])} | `{_table_cell(f['url'])}` |")
    return "\n".join(["## Important Files", "", *rows])


def _payment_section(input: _SkillCtx) -> str:
    override = input.compatible_clients
    defaults = compatible_clients_by_rails(input.accepted_rails) or {}
    clients: dict[str, list[str]] = {}
    for r in input.accepted_rails:
        if override is not None and r in override:
            clients[r] = override[r]
        else:
            clients[r] = defaults.get(r, [])
    rows = ["| Rail | Notes | Compatible clients |", "|---|---|---|"]
    for r in input.accepted_rails:
        client_list = ", ".join(clients.get(r, [])) or "—"
        rows.append(f"| **{_RAIL_LABELS[r]}** | {_RAIL_NOTES[r]} | {client_list} |")
    intro = (
        "Each gated route returns a 402 with `WWW-Authenticate` + `PAYMENT-REQUIRED` "
        "body listing the rails below with current pricing. Pick whichever your wallet "
        "is funded for."
    )
    return "\n".join(["## Payment", "", intro, "", *rows])


def _identity_section(input: _SkillCtx) -> str:
    id_ = input.identity
    if id_ is None:
        return ""
    reqs: list[str] = []
    if id_.get("kyc_required"):
        reqs.append("KYC verified Passport")
    min_age = id_.get("min_age")
    if min_age:
        reqs.append(f"age {min_age}+")
    allowed = id_.get("allowed_jurisdictions")
    if allowed:
        reqs.append(f"{'/'.join(allowed)} only")
    if id_.get("sanctions_clear"):
        reqs.append("sanctions clear")
    if not reqs:
        return ""
    bootstrap = ""
    if input.identity_bootstrap_url:
        bootstrap = (
            f"\n\nIf you don't have a Passport, fetch `{input.identity_bootstrap_url}` and "
            "follow the onboarding there first. Bring back the `opc_...` operator token in "
            "`X-Operator-Token` on every gated request."
        )
    denial_note = (
        "Denial bodies carry an `agent_instructions` block describing the recovery action "
        "— read the `action` field and follow it. See the identity-bootstrap skill for the "
        "canonical denial-code → action table."
    )
    return "\n".join(
        [
            "## Identity Prerequisite",
            "",
            f"This merchant uses AgentScore identity. Required: {', '.join(reqs)}.{bootstrap}",
            "",
            denial_note,
        ]
    )


def _shipping_section(input: _SkillCtx) -> str:
    s = input.shipping
    if s is None:
        return ""
    allowed_countries = s.get("allowed_countries")
    blocked_states = s.get("blocked_states")
    if not allowed_countries and not blocked_states:
        return ""
    lines = ["## Shipping", ""]
    if allowed_countries:
        lines.append(f"Ships to: {', '.join(allowed_countries)}.")
    if blocked_states:
        if len(lines) > 2:
            lines.append("")
        lines.append(f"Blocked US states: {', '.join(blocked_states)}.")
    return "\n".join(lines)


def _endpoints_section(input: _SkillCtx) -> str:
    if not input.endpoints:
        return ""
    rows = ["| Method | Path | Auth | Purpose |", "|---|---|---|---|"]
    for e in input.endpoints:
        auth_label = "identity required" if e["auth_required"] else "anonymous"
        rows.append(f"| {e['method']} | `{_table_cell(e['path'])}` | {auth_label} | {_table_cell(e['description'])} |")
    return "\n".join(["## Endpoints", "", *rows])


def _onboarding_section(input: _SkillCtx) -> str:
    if not input.onboarding_steps:
        return ""
    rows = [f"{i + 1}. {step}" for i, step in enumerate(input.onboarding_steps)]
    return "\n".join(["## Onboarding Flow", "", *rows])


def _triggers_section(input: _SkillCtx) -> str:
    if not input.triggers:
        return ""
    rows = [f"- {t}" for t in input.triggers]
    return "\n".join(["## Triggers", "", "Use this skill when the user wants to:", "", *rows])


def _support_section(input: _SkillCtx) -> str:
    if not input.support_links:
        return ""
    rows = [f"- **{link['label']}**: {link['url']}" for link in input.support_links]
    return "\n".join(["## Support", "", *rows])


def _refresh_footer(input: _SkillCtx) -> str:
    if not input.refresh_footer:
        return ""
    return "_Re-fetch this file periodically to pick up new endpoints, rails, or policies._"


def build_skill_md(
    *,
    name: str,
    description: str,
    homepage: str,
    merchant_name: str,
    accepted_rails: list[RailKey],
    endpoints: list[SkillMdEndpoint],
    triggers: list[str],
    version: str | int = 1,
    license: str | None = None,
    compatibility: str | None = None,
    allowed_tools: str | None = None,
    metadata: dict[str, str | int] | None = None,
    tagline: str | None = None,
    intro: str | None = None,
    files: list[SkillMdLink] | None = None,
    compatible_clients: dict[str, list[str]] | None = None,
    identity: SkillMdIdentityRequirements | None = None,
    identity_bootstrap_url: str | None = None,
    shipping: SkillMdShippingPolicy | None = None,
    onboarding_steps: list[str] | None = None,
    support_links: list[SkillMdLink] | None = None,
    refresh_footer: bool = True,
) -> str:
    """Render an agentskills.io-compatible ``skill.md`` for an agent-commerce merchant.

    Output is YAML frontmatter (``name`` / ``description`` / optional ``license`` /
    ``compatibility`` / ``allowed-tools`` / ``metadata``) followed by markdown sections
    describing payment rails, identity requirements, endpoints, triggers, and support
    links — exactly the agent-facing contract, with no internal posture (no
    ``fail_open``, no mount-strategy names, no KYC vendor, no defense parameters).
    """
    ctx = _SkillCtx(
        name=name,
        description=description,
        homepage=homepage,
        merchant_name=merchant_name,
        accepted_rails=accepted_rails,
        endpoints=endpoints,
        triggers=triggers,
        version=version,
        license=license,
        compatibility=compatibility,
        allowed_tools=allowed_tools,
        metadata=metadata or {},
        tagline=tagline,
        intro=intro,
        files=files or [],
        compatible_clients=compatible_clients,
        identity=identity,
        identity_bootstrap_url=identity_bootstrap_url,
        shipping=shipping,
        onboarding_steps=onboarding_steps or [],
        support_links=support_links or [],
        refresh_footer=refresh_footer,
    )
    _validate(ctx)
    sections = [
        _frontmatter(ctx),
        _title_block(ctx),
        _important_files(ctx),
        _identity_section(ctx),
        _payment_section(ctx),
        _shipping_section(ctx),
        _onboarding_section(ctx),
        _endpoints_section(ctx),
        _triggers_section(ctx),
        _support_section(ctx),
        _refresh_footer(ctx),
    ]
    body = "\n\n".join(s for s in sections if s)
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return body.rstrip() + "\n"
