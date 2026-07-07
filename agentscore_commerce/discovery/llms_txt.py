"""llms.txt builders — identity section + payment section + full document assembler."""

import re
from typing import Any, TypedDict


class LlmsTxtSection(TypedDict):
    """One ``## Heading`` block in :func:`build_llms_txt`."""

    heading: str
    content: str


def llms_txt_identity_section(
    *,
    agentscore: bool = False,
    aip: bool = False,
    compliance: dict[str, Any] | None = None,
) -> str:
    """Generate the standard "## Identity" section for an AgentScore-gated merchant's llms.txt.

    When ``aip`` is true, also advertise the AIP Agent Identity Token path. AgentScore's own
    issuer is always trusted, so set this whenever the merchant has an AIP gate (even with no
    external issuers).
    """
    if not agentscore:
        return ""
    aip_bullet = ""
    if aip:
        aip_bullet = (
            "\n- **`Agent-Identity: <JWT>` + RFC 9421 signature** — present an Agent Identity Token (AIP) "
            "from a trusted issuer (AgentScore is always trusted). Short-lived and bound to your key; sign "
            "the request (`Signature-Input` + `Signature` over `@method @authority @path agent-identity`, "
            "tag `agent-identity`) to prove possession. No long-lived token on the wire. Mint one with "
            "`agentscore-pay identity-mint` or let `agentscore-pay pay --identity aip` attach it automatically."
        )
    compliance_note = ""
    if compliance:
        c = compliance
        parts: list[str] = []
        if c.get("require_kyc"):
            parts.append("KYC required")
        if c.get("min_age"):
            parts.append(f"age {c['min_age']}+")
        if c.get("allowed_jurisdictions"):
            parts.append(f"{'/'.join(c['allowed_jurisdictions'])} only")
        if c.get("require_sanctions_clear"):
            parts.append("sanctions clear")
        if parts:
            compliance_note = f"\n\nCompliance: {', '.join(parts)}."
    return (
        "## Identity\n\n"
        "AgentScore identity is reusable across every AgentScore-gated merchant — one KYC, "
        "no re-verification per site. Pick a header:\n\n"
        "- **`X-Wallet-Address: 0x...` or base58** — works on signing rails (Tempo, x402, "
        "Solana MPP). The wallet you claim must sign the payment.\n"
        "- **`X-Operator-Token: opc_...`** — works on every rail, including Stripe SPT. "
        f"Reusable across AgentScore merchants until expiry.{aip_bullet}\n"
        "- **Neither** — you get a 403 with `verify_url`. Complete the session flow once and "
        f"reuse the resulting `opc_...` everywhere.{compliance_note}"
    )


def llms_txt_payment_section(
    *,
    rails: list[str],
    app_url: str,
    verbose: bool = False,
    tempo_network_name: str = "tempo-mainnet",
    tempo_chain_id: int = 4217,
) -> str:
    """Generate the standard "## Payment" section.

    Pass ``verbose=True`` for the rich variant — multi-step setup + full command examples +
    exact-amount warnings. Default is the compact one-bullet-per-rail form.

    ``tempo_network_name`` / ``tempo_chain_id`` are surfaced in the verbose-mode prerequisites;
    ignored in compact mode.
    """
    if verbose:
        return _llms_txt_payment_section_verbose(
            rails=rails,
            app_url=app_url,
            tempo_network_name=tempo_network_name,
            tempo_chain_id=tempo_chain_id,
        )
    return _llms_txt_payment_section_compact(rails=rails, app_url=app_url)


def _has_rail_family(rails: list[str], prefix: str) -> bool:
    return any(r.startswith(prefix) for r in rails)


_TESTNET_MARKER = re.compile(r"(sepolia|devnet|moderato|testnet)")


def _is_testnet_rail(rails: list[str], prefix: str) -> bool:
    return any(r.startswith(prefix) and _TESTNET_MARKER.search(r) for r in rails)


def _llms_txt_payment_section_compact(*, rails: list[str], app_url: str) -> str:
    lines: list[str] = ["## Payment", ""]
    rails_list = list(rails)
    if _has_rail_family(rails_list, "tempo-"):
        lines.append(
            "- **Tempo USDC via MPP** — "
            f"`tempo request -X POST -H \"X-Operator-Token: opc_...\" --json '{{...}}' --max-spend N {app_url}`"
        )
    if _has_rail_family(rails_list, "x402-base-"):
        lines.append(
            f"- **x402 USDC on Base** (EIP-3009) — `agentscore-pay pay POST {app_url} --chain base "
            "-H \"X-Operator-Token: opc_...\" -d '{...}'`"
        )
    if _has_rail_family(rails_list, "mpp-solana-"):
        lines.append(
            f"- **USDC on Solana** — `agentscore-pay pay POST {app_url} --chain solana "
            "-H \"X-Operator-Token: opc_...\" -d '{...}'`"
        )
    if "stripe-spt" in rails_list:
        lines.append(
            "- **Stripe Shared Payment Token** — agent mints SPT (own Stripe account scoped to networkId, "
            "OR `link-cli spend-request create --credential-type shared_payment_token --network-id "
            "<profileId> ...`)"
        )
    lines.append("")
    lines.append(
        "IMPORTANT: Do NOT use raw on-chain transfers. Use the CLI commands above so the payment credential "
        "is signed and submitted via the protocol handshake."
    )
    lines.append("")
    return "\n".join(lines)


def _llms_txt_payment_section_verbose(
    *,
    rails: list[str],
    app_url: str,
    tempo_network_name: str,
    tempo_chain_id: int,
) -> str:
    rails_list = list(rails)
    has_tempo = _has_rail_family(rails_list, "tempo-")
    has_base = _has_rail_family(rails_list, "x402-base-")
    has_solana = _has_rail_family(rails_list, "mpp-solana-")
    has_stripe = "stripe-spt" in rails_list
    base_network_name = "Base Sepolia" if _is_testnet_rail(rails_list, "x402-base-") else "Base"
    solana_network_name = "Solana devnet" if _is_testnet_rail(rails_list, "mpp-solana-") else "Solana"

    lines: list[str] = ["## Payment", ""]
    lines.append("Accepted rails:")
    lines.append("")
    if has_tempo:
        lines.append("- **USDC on Tempo**")
    if has_base:
        lines.append(f"- **USDC on {base_network_name}**")
    if has_solana:
        lines.append(f"- **USDC on {solana_network_name}**")
    if has_stripe:
        lines.append("- **Stripe Shared Payment Token**")
    lines.append("")

    if has_tempo:
        lines.append("### Pay with Tempo")
        lines.append("")
        lines.append("```bash")
        lines.append("curl -fsSL https://tempo.xyz/install | bash")
        lines.append("tempo wallet login")
        lines.append(f"tempo wallet whoami     # need USDC.e on {tempo_network_name} (chain {tempo_chain_id})")
        lines.append("tempo wallet fund       # if zero")
        lines.append("")
        lines.append("tempo request -X POST \\")
        lines.append('  -H "X-Operator-Token: opc_..." \\')
        lines.append("  --json '{...}' \\")
        lines.append("  --max-spend N \\")
        lines.append(f"  {app_url}")
        lines.append("```")
        lines.append("")

    if has_base or has_solana:
        chains_label = " or ".join(
            x for x in [base_network_name if has_base else None, solana_network_name if has_solana else None] if x
        )
        flags = " or ".join(
            x for x in ["`--chain base`" if has_base else None, "`--chain solana`" if has_solana else None] if x
        )
        lines.append(f"### Pay with {chains_label}")
        lines.append("")
        lines.append("```bash")
        lines.append("npm install -g @agent-score/pay")
        lines.append(f"agentscore-pay wallet create {flags}")
        lines.append(f"agentscore-pay balance {flags}   # fund the printed address with USDC")
        lines.append("")
        lines.append(f"agentscore-pay pay POST {app_url} \\")
        lines.append(f"  {'--chain base' if has_base else '--chain solana'} \\")
        lines.append('  -H "X-Operator-Token: opc_..." \\')
        lines.append("  -d '{...}' \\")
        lines.append("  --max-spend N")
        lines.append("```")
        lines.append("")

    if has_stripe:
        lines.append("### Pay with Stripe SPT")
        lines.append("")
        lines.append(
            "Mint a SharedPaymentToken scoped to the `profile_id` from the 402 body, then submit via "
            "`Authorization: Payment` with `method=stripe/charge`. Either your own Stripe account or "
            "`link-cli spend-request create --credential-type shared_payment_token --network-id <profileId> ...` "
            "for Stripe Link wallets."
        )
        lines.append("")

    lines.append(
        "IMPORTANT: Use the CLIs above. Raw on-chain transfers (e.g. `tempo wallet transfer`, sending USDC "
        "manually to deposit addresses) bypass the protocol handshake and the request will not complete."
    )
    if has_base or has_solana:
        lines.append(
            "IMPORTANT: Pay the exact amount in the 402 challenge. Overpayments and underpayments cannot be matched."
        )
    lines.append("")
    return "\n".join(lines)


def build_llms_txt(
    *,
    merchant_name: str,
    sections: list[LlmsTxtSection] | None = None,
    tagline: str | None = None,
    agentscore_identity: dict[str, Any] | None = None,
    payment: dict[str, Any] | None = None,
) -> str:
    """Assemble a complete llms.txt document with optional AgentScore identity + payment boilerplate.

    ``agentscore_identity`` is a dict forwarded to :func:`llms_txt_identity_section`
    (keys: ``agentscore``, ``aip``, ``compliance``). ``payment`` is a dict forwarded to
    :func:`llms_txt_payment_section` (keys: ``rails``, ``app_url``, ``verbose``,
    ``tempo_network_name``, ``tempo_chain_id``).
    """
    parts: list[str] = [f"# {merchant_name}"]
    if tagline:
        parts.append(f"> {tagline}")
    parts.append("")
    for s in sections or []:
        parts.append(f"## {s['heading']}")
        parts.append("")
        parts.append(s["content"])
        parts.append("")
    if agentscore_identity:
        parts.append(llms_txt_identity_section(**agentscore_identity))
        parts.append("")
    if payment:
        parts.append(llms_txt_payment_section(**payment))
    return "\n".join(parts)
