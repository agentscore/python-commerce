"""agent_instructions block builder for the 402 body."""

from collections.abc import Iterable
from typing import Any, Literal

_TEMPO_WARNING = (
    "Do NOT use `tempo wallet transfer` to pay to the address above. That moves USDC on-chain but does not "
    "notify this server, so the order will not complete. Use `tempo request` instead; it performs the full MPP "
    "handshake (signs, submits Authorization: Payment, waits for server confirmation)."
)
_X402_WARNING = (
    "Do NOT send USDC manually to the x402 deposit addresses (e.g. via a bare wallet `transfer`). Use "
    "`agentscore-pay pay` so the X-Payment credential is signed and submitted; otherwise the order will not "
    "complete even though the deposit lands."
)
_TEMPO_TOOL = "`tempo request` for Tempo USDC (installs via `tempo add request`)"
_AGENTSCORE_PAY_TOOL = (
    "`agentscore-pay` (npm: `@agent-score/pay`); single CLI for x402 on Base and MPP on Tempo + Solana"
)

DEFAULT_WALLET_COMPATIBILITY = (
    "No specific wallet stack required. The 402 challenge is rail-neutral: any client that can produce a valid "
    "MPP credential (Authorization: Payment) or x402 X-Payment header is accepted. The CLI commands above are "
    "the easiest path; sign-it-yourself is fine too."
)


def _default_recommended_tools(how_to_pay: dict[str, Any]) -> list[str]:
    tools: list[str] = []
    has_tempo = "tempo" in how_to_pay
    has_x402 = "x402_base" in how_to_pay or "solana_mpp" in how_to_pay
    if has_tempo:
        tools.append(_TEMPO_TOOL)
    if has_tempo or has_x402:
        tools.append(_AGENTSCORE_PAY_TOOL)
    return tools


def _default_warnings(how_to_pay: dict[str, Any]) -> list[str]:
    w: list[str] = []
    if "tempo" in how_to_pay:
        w.append(_TEMPO_WARNING)
    if "x402_base" in how_to_pay or "solana_mpp" in how_to_pay:
        w.append(_X402_WARNING)
    return w


RailKey = Literal["tempo_mpp", "x402_base", "solana_mpp", "stripe"]

_RAIL_CLIENTS: dict[str, list[str]] = {
    "tempo_mpp": ["agentscore-pay", "tempo request", "x402-proxy"],
    "x402_base": ["agentscore-pay", "x402-proxy", "purl (omit --network flag)"],
    "solana_mpp": ["agentscore-pay"],
    "stripe": ["link-cli"],
}


def compatible_clients_by_rails(rails: Iterable[str]) -> dict[str, list[str]] | None:
    """Smoke-verified client list for a set of rail keys.

    The single source of truth for "which CLIs we've verified end-to-end on each rail" —
    consumed both by the 402-body builder (``build_agent_instructions``) and by discovery
    surfaces (skill.md, llms.txt, etc.). Update here, every surface inherits.
    """
    out: dict[str, list[str]] = {}
    for r in rails:
        clients = _RAIL_CLIENTS.get(r)
        if clients is not None:
            out[r] = list(clients)
    return out or None


def _default_compatible_clients(how_to_pay: dict[str, Any]) -> dict[str, list[str]] | None:
    """Default ``compatible_clients`` derived from the rails declared in ``how_to_pay``.

    Vendors override via the ``compatible_clients`` kwarg of ``build_agent_instructions``
    to add their own tested clients or remove entries that don't fit their endpoint.
    Verified state as of the SDK release.
    """
    rails: list[str] = []
    if "tempo" in how_to_pay:
        rails.append("tempo_mpp")
    if "x402_base" in how_to_pay:
        rails.append("x402_base")
    if "solana_mpp" in how_to_pay:
        rails.append("solana_mpp")
    if "stripe" in how_to_pay:
        rails.append("stripe")
    return compatible_clients_by_rails(rails)


def build_agent_instructions(
    *,
    how_to_pay: dict[str, Any],
    recommended_tools: list[str] | None = None,
    wallet_compatibility: str | None = None,
    timeout_seconds: int = 300,
    warnings: list[str] | None = None,
    # Appended to the default protocol-footgun warnings. Use this to keep the SDK's
    # protocol warnings AND add merchant-specific notes. Ignored when ``warnings`` is set.
    extra_warnings: list[str] | None = None,
    recommended: str | None = None,
    # Per-rail list of client names the merchant has verified work end-to-end.
    # Vendors set this from their own smoke matrix — defaults to None, in which case
    # the field is not emitted (avoids vouching for clients the merchant has not tested).
    # Keys are rail identifiers (e.g. "x402_base", "tempo_mpp"); values are display labels.
    compatible_clients: dict[str, list[str]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the agent_instructions block — combines how_to_pay with tools, warnings, compat note, timeout.

    Defaults adapt to the rails declared in ``how_to_pay``: only tempo-relevant warnings/tools
    appear if ``how_to_pay["tempo"]`` is set, only x402-relevant ones if ``x402_base``/
    ``solana_mpp`` are set. Vendors override ``warnings``/``recommended_tools`` for full control.
    """
    resolved_tools = recommended_tools if recommended_tools is not None else _default_recommended_tools(how_to_pay)
    resolved_warnings = warnings if warnings is not None else [*_default_warnings(how_to_pay), *(extra_warnings or [])]
    resolved_clients = compatible_clients if compatible_clients is not None else _default_compatible_clients(how_to_pay)
    out: dict[str, Any] = {
        "how_to_pay": how_to_pay,
        "recommended_tools": resolved_tools,
        "wallet_compatibility": wallet_compatibility or DEFAULT_WALLET_COMPATIBILITY,
        "timeout_seconds": timeout_seconds,
        "warnings": resolved_warnings,
    }
    if recommended:
        out["recommended"] = recommended
    if resolved_clients:
        out["compatible_clients"] = resolved_clients
    if extra:
        out.update(extra)
    return out
