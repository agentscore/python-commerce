"""llms.txt builders — identity section + payment section + full document assembler."""

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LlmsTxtIdentitySectionInput:
    agentscore: bool = False
    compliance: dict[str, Any] | None = None


def llms_txt_identity_section(input: LlmsTxtIdentitySectionInput) -> str:
    """Generate the standard "Choose your identity header" section for an AgentScore-gated merchant's llms.txt."""
    if not input.agentscore:
        return ""
    compliance_note = ""
    if input.compliance:
        c = input.compliance
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
        "## Choose your identity header\n\n"
        "AgentScore provides cross-merchant identity: one KYC works for every AgentScore-gated endpoint, "
        "no re-verification per site. Pick the right header based on how you plan to pay:\n\n"
        "- **Wallet-auth** (preferred for agents with a stable crypto wallet): send `X-Wallet-Address: 0x...`. "
        "Only works on rails that carry a wallet signature (Tempo MPP and x402 EIP-3009). "
        "The wallet you claim MUST sign the payment; we verify that the payment signer resolves to the same "
        "operator as the claimed wallet. No TTL, no rotation.\n"
        "- **Operator-token** (works for every rail): send `X-Operator-Token: opc_...`. Required when paying "
        "with Stripe SPT or card (those rails have no wallet signer). Reusable across every AgentScore "
        "merchant until the token expires.\n"
        "- **Neither**: you'll get 403 with `verify_url` + `poll_secret`; complete the session flow once, "
        "then reuse the credential (or any wallet that pays under it) forever.\n\n"
        "If you reach another AgentScore-gated endpoint in the future, the same credential and/or any "
        "linked wallet already works; skip the session flow.\n\n"
        "## Memory contract\n\n"
        "AgentScore-gated merchants emit an `agent_memory` block in their 200 success bodies "
        "describing how AgentScore identity works across merchants. The AgentScore API's "
        "POST /v1/sessions, POST /v1/credentials, and POST /v1/credentials/wallets first-mint "
        "success bodies also include it. LLM-hosted agents should persist its contents (the "
        "pattern, never secrets) so subsequent AgentScore-gated endpoints become returning-"
        "customer encounters. The operator_token or linked wallet you use here is reusable "
        f"everywhere.{compliance_note}"
    )


@dataclass
class LlmsTxtPaymentSectionInput:
    rails: list[str]
    app_url: str
    verbose: bool = False
    """Emit the verbose multi-step variant (setup commands per rail + full command examples + warnings).
    Default False (one-line bullet per rail). Use this when llms.txt is the primary integration doc."""
    tempo_network_name: str = "tempo-mainnet"
    """Verbose mode only — Tempo network name to mention in prerequisites."""
    tempo_chain_id: int = 4217
    """Verbose mode only — Tempo chain id to mention in prerequisites."""


def llms_txt_payment_section(input: LlmsTxtPaymentSectionInput) -> str:
    """Generate the standard "## Payment" section.

    Pass `verbose=True` for the rich variant — multi-step setup + full command examples +
    exact-amount warnings. Default is the compact one-bullet-per-rail form.
    """
    if input.verbose:
        return _llms_txt_payment_section_verbose(input)
    return _llms_txt_payment_section_compact(input)


def _has_rail_family(rails: list[str], prefix: str) -> bool:
    return any(r.startswith(prefix) for r in rails)


_TESTNET_MARKER = re.compile(r"(sepolia|devnet|moderato|testnet)")


def _is_testnet_rail(rails: list[str], prefix: str) -> bool:
    return any(r.startswith(prefix) and _TESTNET_MARKER.search(r) for r in rails)


def _llms_txt_payment_section_compact(input: LlmsTxtPaymentSectionInput) -> str:
    lines: list[str] = ["## Payment", ""]
    rails = list(input.rails)
    if _has_rail_family(rails, "tempo-"):
        lines.append(
            "- **Tempo USDC via MPP** — "
            f"`tempo request -X POST -H \"X-Operator-Token: opc_...\" --json '{{...}}' --max-spend N {input.app_url}`"
        )
    if _has_rail_family(rails, "x402-base-"):
        lines.append(
            f"- **x402 USDC on Base** (EIP-3009) — `agentscore-pay pay POST {input.app_url} --chain base "
            "-H \"X-Operator-Token: opc_...\" -d '{...}'`"
        )
    if _has_rail_family(rails, "mpp-solana-"):
        lines.append(
            f"- **x402 USDC on Solana** (SPL Token) — `agentscore-pay pay POST {input.app_url} --chain solana "
            "-H \"X-Operator-Token: opc_...\" -d '{...}'`"
        )
    if "stripe-spt" in rails:
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


def _llms_txt_payment_section_verbose(input: LlmsTxtPaymentSectionInput) -> str:
    rails = list(input.rails)
    has_tempo = _has_rail_family(rails, "tempo-")
    has_base = _has_rail_family(rails, "x402-base-")
    has_solana = _has_rail_family(rails, "mpp-solana-")
    has_stripe = "stripe-spt" in rails
    base_network_name = "Base Sepolia" if _is_testnet_rail(rails, "x402-base-") else "Base"
    solana_network_name = "Solana devnet" if _is_testnet_rail(rails, "mpp-solana-") else "Solana"

    lines: list[str] = ["## Payment", ""]
    lines.append(
        "This is an agent-first API. All payments are initiated and completed by agents. The 402 challenge advertises:"
    )
    lines.append("")
    if has_tempo:
        lines.append("- **Tempo USDC via MPP** (on-chain stablecoin)")
    if has_base or has_solana:
        chain_parts = []
        if has_base:
            chain_parts.append(f"{base_network_name} (EIP-3009)")
        if has_solana:
            chain_parts.append(f"{solana_network_name} (SPL Token)")
        lines.append(f"- **x402 USDC** on {' and '.join(chain_parts)}, via the Coinbase facilitator")
    if has_stripe:
        lines.append(
            "- **Stripe Shared Payment Token** (agent mints SPT on their Stripe account scoped to our "
            "networkId in the challenge, submits it in the credential)"
        )
    lines.append("")

    if has_tempo:
        lines.append("### How to pay with Tempo")
        lines.append("")
        lines.append("1. Install the Tempo CLI: curl -fsSL https://tempo.xyz/install | bash")
        lines.append("2. Log in to your Tempo Wallet: tempo wallet login (passkey auth in browser)")
        lines.append(
            f"3. Confirm your balance: tempo wallet whoami (need USDC.e on {input.tempo_network_name}, "
            f"chain {input.tempo_chain_id})"
        )
        lines.append("4. If balance is zero, fund it: tempo wallet fund")
        lines.append("")
        lines.append("Then use `tempo request` to make the paid purchase:")
        lines.append("")
        lines.append("tempo request -X POST \\")
        lines.append('  -H "X-Operator-Token: opc_your_credential" \\')
        lines.append('  -H "Content-Type: application/json" \\')
        lines.append("  --json '{...}' \\")
        lines.append("  --max-spend N \\")
        lines.append(f"  {input.app_url}")
        lines.append("")
        lines.append(
            f"`tempo request` handles the full MPP handshake: sends the POST, receives the 402 challenge, "
            f"signs the payment on {input.tempo_network_name}, submits the credential, and returns the "
            "completed order."
        )
        lines.append("")

    if has_base or has_solana:
        chain_parts = []
        flag_parts = []
        if has_base:
            chain_parts.append(base_network_name)
            flag_parts.append("`--chain base`")
        if has_solana:
            chain_parts.append(solana_network_name)
            flag_parts.append("`--chain solana`")
        lines.append(f"### How to pay with x402 ({' or '.join(chain_parts)})")
        lines.append("")
        lines.append(
            "1. Install the agentscore-pay CLI: npm install -g @agent-score/pay  "
            "(or: brew install agentscore/tap/agentscore-pay)"
        )
        lines.append(
            f"2. Create a wallet on your chain of choice: agentscore-pay wallet create {' or '.join(flag_parts)}"
        )
        lines.append(f"3. Fund the printed address with USDC on {' or '.join(chain_parts)}")
        lines.append(f"4. Confirm balance: agentscore-pay balance {' or '.join(flag_parts)}")
        lines.append("")
        lines.append("Then submit the paid purchase:")
        lines.append("")
        lines.append(f"agentscore-pay pay POST {input.app_url} \\")
        lines.append(f"  {'--chain base' if has_base else '--chain solana'} \\")
        lines.append('  -H "X-Operator-Token: opc_your_credential" \\')
        lines.append('  -H "Content-Type: application/json" \\')
        lines.append("  -d '{...}' \\")
        lines.append("  --max-spend N")
        lines.append("")
        handshake_chains = []
        if has_base:
            handshake_chains.append("EIP-3009 (Base)")
        if has_solana:
            handshake_chains.append("SPL Token (Solana)")
        lines.append(
            f"The CLI handles the full x402 handshake: hits the URL, parses the 402 challenge, signs the "
            f"{' or '.join(handshake_chains)} transaction, submits via X-Payment header, and returns the "
            "completed order."
        )
        lines.append("")

    if has_stripe:
        lines.append("### How to pay with Stripe SPT")
        lines.append("")
        lines.append(
            "Mint a SharedPaymentToken scoped to the profile_id advertised in "
            "`accepted_methods.stripe.profile_id`, then submit via `Authorization: Payment` MPP header with "
            "`method=stripe/charge`. Either bring your own Stripe account or use `link-cli spend-request "
            "create --credential-type shared_payment_token --network-id <profileId> ...` for users with "
            "Stripe Link wallets."
        )
        lines.append("")

    lines.append(
        "IMPORTANT: Do NOT use `tempo wallet transfer` or send USDC manually to the x402 deposit addresses; "
        "those bypass the payment handshake and the order will not complete."
    )
    if has_base or has_solana:
        lines.append(
            "IMPORTANT: x402 payments must be the exact amount specified in the 402 challenge. Overpayments "
            "and underpayments cannot be matched and funds may be unrecoverable."
        )
    lines.append("")
    return "\n".join(lines)


@dataclass
class LlmsTxtSection:
    heading: str
    content: str


@dataclass
class BuildLlmsTxtInput:
    merchant_name: str
    sections: list[LlmsTxtSection] = field(default_factory=list)
    tagline: str | None = None
    agentscore_identity: LlmsTxtIdentitySectionInput | None = None
    payment: LlmsTxtPaymentSectionInput | None = None


def build_llms_txt(input: BuildLlmsTxtInput) -> str:
    """Assemble a complete llms.txt document with optional AgentScore identity + payment boilerplate."""
    parts: list[str] = [f"# {input.merchant_name}"]
    if input.tagline:
        parts.append(f"> {input.tagline}")
    parts.append("")
    for s in input.sections:
        parts.append(f"## {s.heading}")
        parts.append("")
        parts.append(s.content)
        parts.append("")
    if input.agentscore_identity:
        parts.append(llms_txt_identity_section(input.agentscore_identity))
        parts.append("")
    if input.payment:
        parts.append(llms_txt_payment_section(input.payment))
    return "\n".join(parts)
