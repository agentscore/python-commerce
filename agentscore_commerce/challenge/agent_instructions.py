"""agent_instructions block builder for the 402 body."""

from dataclasses import dataclass, field
from typing import Any

DEFAULT_RECOMMENDED_TOOLS = [
    "`tempo request` for Tempo USDC (installs via `tempo add request`)",
    (
        "`agentscore-pay` (npm: `@agent-score/pay`) — single CLI for x402 on Base + Solana, "
        "also speaks tempo MPP via `--chain tempo`"
    ),
]
DEFAULT_WALLET_COMPATIBILITY = (
    "No specific wallet stack required. The 402 challenge is rail-neutral: any client that can produce a valid "
    "MPP credential (Authorization: Payment) or x402 X-Payment header is accepted. The CLI commands above are "
    "the easiest path; sign-it-yourself is fine too."
)
DEFAULT_WARNINGS = [
    (
        "Do NOT use `tempo wallet transfer` to pay to the address above. That moves USDC on-chain but does not "
        "notify this server, leaving your order in pending_identity state. Use `tempo request` instead — it performs "
        "the full MPP handshake (signs, submits Authorization: Payment, waits for server confirmation)."
    ),
    (
        "Do NOT send USDC manually to the x402 deposit addresses (e.g. via a bare wallet `transfer`). Use "
        "`agentscore-pay pay` so the X-Payment credential is signed and submitted; otherwise the order stays in "
        "pending_identity even though the deposit lands."
    ),
]


@dataclass
class BuildAgentInstructionsInput:
    how_to_pay: dict[str, Any]
    recommended_tools: list[str] | None = None
    wallet_compatibility: str | None = None
    timeout_seconds: int = 300
    warnings: list[str] | None = None
    recommended: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_agent_instructions(input: BuildAgentInstructionsInput) -> dict[str, Any]:
    """Build the agent_instructions block — combines how_to_pay with tools, warnings, compat note, timeout."""
    out: dict[str, Any] = {
        "how_to_pay": input.how_to_pay,
        "recommended_tools": input.recommended_tools or DEFAULT_RECOMMENDED_TOOLS,
        "wallet_compatibility": input.wallet_compatibility or DEFAULT_WALLET_COMPATIBILITY,
        "timeout_seconds": input.timeout_seconds,
        "warnings": input.warnings if input.warnings is not None else DEFAULT_WARNINGS,
    }
    if input.recommended:
        out["recommended"] = input.recommended
    out.update(input.extra)
    return out
