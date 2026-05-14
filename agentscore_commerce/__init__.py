"""Agent commerce SDK — identity middleware + payment helpers + 402 builders + discovery + Stripe multichain.

Submodules:
    agentscore_commerce.identity   - per-framework gate adapters
    agentscore_commerce.payment    - networks/usdc/rails registries, directives, x402/mpp helpers
    agentscore_commerce.discovery  - probe + .well-known/mpp.json + llms.txt + OpenAPI snippets
    agentscore_commerce.challenge  - 402-body builders
    agentscore_commerce.stripe_multichain - multichain PaymentIntent helpers
    agentscore_commerce.checkout   - high-level Checkout orchestrator
    agentscore_commerce.api        - AgentScore SDK re-export
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from agentscore_commerce.checkout import (
    Checkout,
    CheckoutContext,
    CheckoutRailSpec,
    CheckoutRequest,
    CheckoutResult,
    MppxComposeOutcome,
    PricingResult,
    SettleOutcome,
)

try:
    __version__ = _pkg_version("agentscore-commerce")
except PackageNotFoundError:
    # Editable install or pre-build state — fall back to a sentinel so consumers
    # don't crash on a missing dist-info dir. Real version always comes from
    # pyproject.toml at install time.
    __version__ = "0.0.0+local"

__all__ = [
    "Checkout",
    "CheckoutContext",
    "CheckoutRailSpec",
    "CheckoutRequest",
    "CheckoutResult",
    "MppxComposeOutcome",
    "PricingResult",
    "SettleOutcome",
    "__version__",
]
