"""how_to_pay block builder — per-rail setup/command/what_it_does for 402 agent_instructions."""

import math
from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class TempoRailConfig:
    recipient: str
    network_name: str = "tempo-mainnet"
    chain_id: int = 4217
    recommend: Literal["tempo", "agentscore-pay", "both"] = "both"


@dataclass
class X402BaseRailConfig:
    recipient: str
    network: str = "eip155:8453"


@dataclass
class X402SolanaRailConfig:
    recipient: str
    network: str = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


@dataclass
class StripeRailConfig:
    profile_id: str | None = None
    product_name: str | None = None


@dataclass
class HowToPayRails:
    tempo: TempoRailConfig | None = None
    x402_base: X402BaseRailConfig | None = None
    solana_mpp: X402SolanaRailConfig | None = None
    stripe: StripeRailConfig | None = None


@dataclass
class BuildHowToPayInput:
    url: str
    retry_body_json: str
    total_usd: float | str
    rails: HowToPayRails
    op_token_placeholder: str = "<your_opc_token>"
    max_spend: float | str | None = None


TEMPO_SETUP = [
    "curl -fsSL https://tempo.xyz/install | bash",
    "tempo wallet login",
    "tempo wallet whoami",
    "tempo wallet fund   # if balance is zero",
]
PAY_SETUP_BASE = [
    "npm install -g @agent-score/pay   # or: brew install agentscore/tap/agentscore-pay",
    "agentscore-pay wallet create --chain base",
    "agentscore-pay balance --chain base   # fund the printed address with USDC on Base",
]
PAY_SETUP_SOLANA = [
    "npm install -g @agent-score/pay   # or: brew install agentscore/tap/agentscore-pay",
    "agentscore-pay wallet create --chain solana",
    "agentscore-pay balance --chain solana   # fund the printed address with USDC on Solana",
]


def build_how_to_pay(input: BuildHowToPayInput) -> dict[str, Any]:
    """Build the agent_instructions.how_to_pay block.

    Generates per-rail setup/command/what_it_does so agents see concrete commands per rail in the 402 body.
    """
    total_num = float(input.total_usd) if isinstance(input.total_usd, str) else input.total_usd
    max_spend = str(input.max_spend) if input.max_spend is not None else f"{math.ceil(total_num) + 1:.2f}"
    op_token = input.op_token_placeholder
    block: dict[str, Any] = {}

    if input.rails.tempo:
        t = input.rails.tempo
        tempo_command = (
            f"tempo request -X POST -H 'X-Operator-Token: {op_token}' -H 'Content-Type: application/json' "
            f"--json '{input.retry_body_json}' --max-spend {max_spend} {input.url}"
        )
        pay_command = (
            f"agentscore-pay pay POST {input.url} --chain tempo -H 'X-Operator-Token: {op_token}' "
            f"-H 'Content-Type: application/json' -d '{input.retry_body_json}' --max-spend {max_spend}"
        )
        entry: dict[str, Any] = {
            "setup": TEMPO_SETUP,
            "prerequisite": (
                f"Run `tempo wallet whoami` and confirm USDC.e balance on {t.network_name} (chain {t.chain_id}) "
                f"is at least ${max_spend}. If the tempo CLI is not installed, run the setup commands above first."
            ),
            "command": pay_command if t.recommend == "agentscore-pay" else tempo_command,
            "what_it_does": (
                f"Hits this endpoint, receives this same 402, signs the MPP challenge on {t.network_name}, and "
                "submits the credential back via Authorization: Payment. Either client (tempo request or "
                "agentscore-pay pay --chain tempo) works — both run the full MPP handshake."
            ),
        }
        if t.recommend == "both":
            entry["alternative_command"] = pay_command
        elif t.recommend == "agentscore-pay":
            entry["alternative_command"] = tempo_command
        block["tempo"] = entry

    if input.rails.x402_base:
        b = input.rails.x402_base
        block["x402_base"] = {
            "setup": PAY_SETUP_BASE,
            "prerequisite": (
                f"Run `agentscore-pay balance --chain base` and confirm USDC balance on Base ({b.network}) is at "
                f"least ${max_spend}. If the CLI is not installed, run the setup commands above first."
            ),
            "command": (
                f"agentscore-pay pay POST {input.url} --chain base -H 'X-Operator-Token: {op_token}' "
                f"-H 'Content-Type: application/json' -d '{input.retry_body_json}' --max-spend {max_spend}"
            ),
            "what_it_does": (
                "Hits this endpoint, receives this same 402, signs an EIP-3009 USDC TransferWithAuthorization "
                "on Base, submits via X-Payment header. Server verifies + settles via the Coinbase facilitator + "
                "returns 200 with the completed order."
            ),
        }

    if input.rails.solana_mpp:
        s = input.rails.solana_mpp
        block["solana_mpp"] = {
            "setup": PAY_SETUP_SOLANA,
            "prerequisite": (
                f"Run `agentscore-pay balance --chain solana` and confirm USDC balance on Solana ({s.network}) "
                f"is at least ${max_spend}. If the CLI is not installed, run the setup commands above first."
            ),
            "command": (
                f"agentscore-pay pay POST {input.url} --chain solana -H 'X-Operator-Token: {op_token}' "
                f"-H 'Content-Type: application/json' -d '{input.retry_body_json}' --max-spend {max_spend}"
            ),
            "what_it_does": (
                "Hits this endpoint, receives this same 402, signs an SPL Token TransferChecked transaction on "
                "Solana, submits via X-Payment header. Server verifies + settles via the Coinbase facilitator + "
                "returns 200 with the completed order."
            ),
        }

    if input.rails.stripe:
        cfg = input.rails.stripe
        amount_cents = round(total_num * 100)
        link_cli_blocked = amount_cents > 50000
        product_name = cfg.product_name or "this purchase"
        spt_context = (
            f'Purchasing "{product_name}" via the agent commerce API. The user authorized this purchase '
            f"through their AI agent for ${total_num}; charge to be settled via shared payment token over the "
            "Machine Payments Protocol."
        )
        stripe_block: dict[str, Any] = {
            "prerequisite": (
                "Either your own Stripe account with Shared Payment Token acceptance, OR a Stripe Link wallet "
                "(any user with link.com)."
            ),
            "instructions": (
                "Mint a SharedPaymentToken scoped to the profile_id advertised in accepted_methods, then submit "
                "via Authorization: Payment MPP header with method=stripe/charge."
            ),
        }
        if cfg.profile_id and not link_cli_blocked:
            stripe_block["setup_link_cli"] = [
                "npm install -g @stripe/link-cli   # or use npx -y @stripe/link-cli for one-shot",
                "link-cli auth login   # one-time, opens your Link wallet",
                "link-cli payment-methods list --output-json   # copy a csmrpd_... id",
            ]
            stripe_block["command_link_cli"] = [
                (
                    "SPEND_ID=$(link-cli spend-request create "
                    "--payment-method-id <csmrpd_id_from_payment_methods_list> "
                    f"--credential-type shared_payment_token --network-id {cfg.profile_id} "
                    f"--amount {amount_cents} "
                    f'--context "{spt_context}" --request-approval --output-json | jq -r .id)'
                ),
                (
                    f"link-cli mpp pay {input.url} --spend-request-id $SPEND_ID --method POST "
                    f"--data '{input.retry_body_json}' --header 'X-Operator-Token: {op_token}' --output-json"
                ),
            ]
            stripe_block["what_it_does_link_cli"] = (
                "For users who have a Stripe Link wallet: step 1 mints a one-time-use SharedPaymentToken scoped to "
                "this purchase and pushes a notification to the user for approval (blocks until approved); step 2 "
                "submits the SPT via the MPP handshake along with your AgentScore operator credential."
            )
        elif link_cli_blocked:
            stripe_block["note"] = (
                "link-cli SPT path not available for this purchase — Stripe link-cli caps spend requests at $500.00 "
                f"($50000 cents); your total is ${total_num}. Use your own Stripe account with the SharedPaymentToken "
                "API instead."
            )
        block["stripe"] = stripe_block

    return block
