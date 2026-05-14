"""x402 Settlement-Overrides header helpers — used with the `upto` scheme to specify the actual amount.

The header is JSON-encoded and lives on the merchant's response; the facilitator settles for that
amount instead of the advertised maximum. Per the x402 docs, the amount field accepts:
- raw atomic units, e.g., '1000' for $0.001 USDC at 6 decimals
- percentage, e.g., '50%' of the authorized maximum
- dollar price, e.g., '$0.05' (converted to atomic via the network's default token)
"""

import json

SETTLEMENT_OVERRIDES_HEADER = "Settlement-Overrides"


def settlement_override_header(*, amount: str) -> tuple[str, str]:
    """Build a (name, value) pair for the x402 Settlement-Overrides response header.

    ``amount`` is the override value: raw atomic units, ``"<n>%"`` percentage, or ``"$X.YZ"``
    dollar price.
    """
    return SETTLEMENT_OVERRIDES_HEADER, json.dumps({"amount": amount}, separators=(",", ":"))
