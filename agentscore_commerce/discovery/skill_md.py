"""skill.md renderer — Claude-Skill-compatible agent-discovery surface.

Emits a YAML-frontmatter + markdown body manifest describing a merchant's agent-facing
contract: payment rails, compatible clients per rail, identity requirements as outcomes,
shipping policy, endpoints, triggers, support links.

Renders strictly agent-facing data — no ``fail_open``, no mount-strategy names, no KYC
vendor names, no defense parameters, no idempotency construction. Internal posture stays
in merchant runtime config.

The compatible-clients-per-rail table sources from the same SDK constant
(``compatible_clients_by_rails`` in ``challenge.agent_instructions``) that drives the live
402 body's ``compatible_clients`` field — single source of truth across surfaces.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agentscore_commerce.challenge.agent_instructions import compatible_clients_by_rails

if TYPE_CHECKING:
    from collections.abc import Iterable

RailKey = Literal["tempo_mpp", "x402_base", "x402_solana", "stripe"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


@dataclass
class SkillMdEndpoint:
    method: HttpMethod
    path: str
    auth_required: bool
    description: str


@dataclass
class SkillMdIdentityRequirements:
    kyc_required: bool = False
    min_age: int | None = None
    allowed_jurisdictions: list[str] | None = None
    sanctions_clear: bool = False


@dataclass
class SkillMdShippingPolicy:
    allowed_countries: list[str] | None = None
    blocked_states: list[str] | None = None


@dataclass
class SkillMdLink:
    label: str
    url: str


@dataclass
class BuildSkillMdInput:
    """Inputs for ``build_skill_md``.

    Fields without defaults are required: ``name``, ``description``, ``homepage``,
    ``merchant_name``, ``accepted_rails``, ``endpoints``, ``triggers``.
    """

    # Frontmatter
    name: str
    """Skill manifest identifier — kebab-case, e.g. 'martin-estate-wine-commerce'."""
    description: str
    """One-line description of what this skill does. Surfaces in skill catalogs."""
    homepage: str
    """Merchant homepage (or domain root) — appears in frontmatter."""
    merchant_name: str
    """Human display name (e.g. 'Martin Estate Winery')."""
    accepted_rails: list[RailKey]
    """Rails the merchant accepts. Drives the Payment + Compatible Clients sections.
    Order is preserved in render. Keep these in sync with the merchant's ``respond_402``
    rail config."""
    endpoints: list[SkillMdEndpoint]
    """Agent-facing endpoints — path, method, whether auth is required, brief purpose."""
    triggers: list[str]
    """When this skill should fire (skill loader uses for trigger matching)."""

    # Frontmatter (optional)
    version: int = 1
    """Skill schema version — increment when the skill body materially changes. Default 1."""

    # Body
    tagline: str | None = None
    """Optional one-line tagline appearing under the title."""
    intro: str | None = None
    """Optional short prose intro describing what the merchant offers. Renders below the title."""

    # Linked discovery surfaces
    files: list[SkillMdLink] = field(default_factory=list)
    """Files / well-known URLs surfaced under the 'Important Files' table. The skill.md URL
    itself is added automatically — list other discovery surfaces (llms.txt, mpp.json,
    openapi.json, agent-card.json)."""

    # Override the per-rail compatible-clients matrix. When omitted, derives from
    # ``accepted_rails`` via the SDK's smoke-verified default.
    compatible_clients: dict[str, list[str]] | None = None

    # Identity requirements as agent-observable outcomes (kyc / age / jurisdiction /
    # sanctions). Internal posture (``fail_open``, mount strategy, KYC vendor) is
    # intentionally not part of this shape — agents act on outcomes, not implementation.
    identity: SkillMdIdentityRequirements | None = None
    identity_bootstrap_url: str | None = None
    """URL to the identity-bootstrap skill (typically ``https://agentscore.sh/skill.md``).
    Linked from the Identity Prerequisite section so an agent without a Passport can
    follow the bootstrap before attempting purchase."""

    # Physical-goods policy. Omit for digital merchants.
    shipping: SkillMdShippingPolicy | None = None

    # Optional numbered onboarding steps. Each entry renders as a numbered list item;
    # may include shell snippets in markdown code fences.
    onboarding_steps: list[str] = field(default_factory=list)

    # Support / homepage / docs links rendered in the Support section.
    support_links: list[SkillMdLink] = field(default_factory=list)

    # When True (default), append a footer noting clients can refresh skill.md to pick
    # up new endpoints. Set to False to suppress.
    refresh_footer: bool = True


_RAIL_LABELS: dict[str, str] = {
    "tempo_mpp": "MPP on Tempo",
    "x402_base": "x402 on Base",
    "x402_solana": "x402 on Solana",
    "stripe": "Stripe Shared Payment Token",
}

_RAIL_NOTES: dict[str, str] = {
    "tempo_mpp": (
        "USDC. Use `agentscore-pay --chain tempo` (or `tempo request`); "
        "MPP credential goes in `Authorization: Payment`."
    ),
    "x402_base": ("USDC (EIP-3009). Use `agentscore-pay`; X-Payment header carries the signed credential."),
    "x402_solana": ("USDC (SPL). Use `agentscore-pay`; X-Payment header carries the signed credential."),
    "stripe": (
        "Card via Link wallet. Use `@stripe/link-cli` — `agentscore-pay` emits the "
        "handoff hint when this rail is picked."
    ),
}


def _frontmatter(input: BuildSkillMdInput) -> str:
    return "\n".join(
        [
            "---",
            f"name: {input.name}",
            f"description: {input.description}",
            f"homepage: {input.homepage}",
            "metadata:",
            f"  version: {input.version}",
            "---",
        ]
    )


def _important_files(input: BuildSkillMdInput) -> str:
    skill_url = f"{input.homepage.rstrip('/')}/skill.md"
    rows = [
        "| File | URL |",
        "|------|-----|",
        f"| **SKILL.md** (this file) | `{skill_url}` |",
    ]
    for f in input.files:
        rows.append(f"| {f.label} | `{f.url}` |")
    return "\n".join(["## Important Files", "", *rows])


def _payment_section(input: BuildSkillMdInput) -> str:
    clients = input.compatible_clients
    if clients is None:
        clients = compatible_clients_by_rails(input.accepted_rails) or {}
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


def _identity_section(input: BuildSkillMdInput) -> str:
    id_ = input.identity
    if id_ is None:
        return ""
    reqs: list[str] = []
    if id_.kyc_required:
        reqs.append("KYC verified Passport")
    if id_.min_age:
        reqs.append(f"age {id_.min_age}+")
    if id_.allowed_jurisdictions:
        reqs.append(f"{'/'.join(id_.allowed_jurisdictions)} only")
    if id_.sanctions_clear:
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


def _shipping_section(input: BuildSkillMdInput) -> str:
    s = input.shipping
    if s is None or (not s.allowed_countries and not s.blocked_states):
        return ""
    lines = ["## Shipping", ""]
    if s.allowed_countries:
        lines.append(f"Ships to: {', '.join(s.allowed_countries)}.")
    if s.blocked_states:
        if len(lines) > 2:
            lines.append("")
        lines.append(f"Blocked US states: {', '.join(s.blocked_states)}.")
    return "\n".join(lines)


def _endpoints_section(input: BuildSkillMdInput) -> str:
    if not input.endpoints:
        return ""
    rows = ["| Method | Path | Auth | Purpose |", "|---|---|---|---|"]
    for e in input.endpoints:
        auth_label = "identity required" if e.auth_required else "anonymous"
        rows.append(f"| {e.method} | `{e.path}` | {auth_label} | {e.description} |")
    return "\n".join(["## Endpoints", "", *rows])


def _onboarding_section(input: BuildSkillMdInput) -> str:
    if not input.onboarding_steps:
        return ""
    rows = [f"{i + 1}. {step}" for i, step in enumerate(input.onboarding_steps)]
    return "\n".join(["## Onboarding Flow", "", *rows])


def _triggers_section(input: BuildSkillMdInput) -> str:
    if not input.triggers:
        return ""
    rows = [f"- {t}" for t in input.triggers]
    return "\n".join(["## Triggers", "", "Use this skill when the user wants to:", "", *rows])


def _support_section(input: BuildSkillMdInput) -> str:
    if not input.support_links:
        return ""
    rows = [f"- **{link.label}**: {link.url}" for link in input.support_links]
    return "\n".join(["## Support", "", *rows])


def _refresh_footer(input: BuildSkillMdInput) -> str:
    if not input.refresh_footer:
        return ""
    return "_Re-fetch this file periodically to pick up new endpoints, rails, or policies._"


def build_skill_md(input: BuildSkillMdInput) -> str:
    """Render a Claude-Skill-compatible ``skill.md`` for an agent-commerce merchant.

    Output is YAML frontmatter (``name`` / ``description`` / ``homepage`` /
    ``metadata.version``) followed by markdown sections describing payment rails, identity
    requirements, endpoints, triggers, and support links — exactly the agent-facing
    contract, with no internal posture (no ``fail_open``, no mount-strategy names, no KYC
    vendor, no defense parameters).

    The compatible-clients-per-rail table sources from the same SDK constant
    (``compatible_clients_by_rails``) that drives the live 402 body's
    ``compatible_clients`` field — single source of truth across surfaces.
    """
    title_block = [f"# {input.merchant_name}"]
    if input.tagline:
        title_block.append(f"\n_{input.tagline}_")
    if input.intro:
        title_block.append(f"\n{input.intro}")

    sections: Iterable[str] = (
        _frontmatter(input),
        "\n".join(title_block),
        _important_files(input),
        _identity_section(input),
        _payment_section(input),
        _shipping_section(input),
        _onboarding_section(input),
        _endpoints_section(input),
        _triggers_section(input),
        _support_section(input),
        _refresh_footer(input),
    )
    body = "\n\n".join(s for s in sections if s)
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return body.rstrip() + "\n"
