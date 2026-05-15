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
    CheckoutGateConfig,
    CheckoutRailSpec,
    CheckoutRequest,
    CheckoutResult,
    CheckoutValidationError,
    MppxComposeOutcome,
    PricingResult,
    SettleOutcome,
    format_pydantic_errors,
    validation_envelope,
    validation_response_aiohttp,
    validation_response_django,
    validation_response_fastapi,
    validation_response_flask,
    validation_response_sanic,
)
from agentscore_commerce.checkout_hooks import make_mppx_compose_hook

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
    "CheckoutGateConfig",
    "CheckoutRailSpec",
    "CheckoutRequest",
    "CheckoutResult",
    "CheckoutValidationError",
    "MppxComposeOutcome",
    "PricingResult",
    "SettleOutcome",
    "__version__",
    "format_pydantic_errors",
    "make_mppx_compose_hook",
    "validation_envelope",
    "validation_response_aiohttp",
    "validation_response_django",
    "validation_response_fastapi",
    "validation_response_flask",
    "validation_response_sanic",
]
