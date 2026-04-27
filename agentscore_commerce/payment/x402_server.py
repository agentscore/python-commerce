"""One-call x402 server setup wrapping the official `x402` Python package.

Resolves the facilitator, constructs the server, registers schemes per network
with v1+v2 dual-register, and optionally adds the Bazaar discovery extension.

Replaces ~15 lines of boilerplate with a single config call::

    from agentscore_commerce.payment import create_x402_server

    server = await create_x402_server(
        facilitator="coinbase",
        rails=["x402-base-mainnet", "x402-solana-mainnet"],
        bazaar=True,
    )

`x402` is an OPTIONAL peer dependency — install only the schemes you use::

    pip install 'x402[evm,svm,fastapi]>=2.8,<3'   # plus 'coinbase-x402' for the Coinbase facilitator
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from agentscore_commerce.payment.networks import networks
from agentscore_commerce.payment.x402 import register_x402_schemes_v1_v2

if TYPE_CHECKING:
    from collections.abc import Iterable

X402SymbolicRail = Literal[
    "x402-base-mainnet",
    "x402-base-sepolia",
    "x402-solana-mainnet",
    "x402-solana-devnet",
    "x402-base-mainnet-upto",
    "x402-base-sepolia-upto",
]

X402FacilitatorChoice = Literal["coinbase", "http"]


@dataclass(frozen=True)
class CustomScheme:
    """Custom (network, scheme-instance) pair to register beyond the symbolic rails."""

    network: str
    scheme: Any


@dataclass
class CreateX402ServerOptions:
    """Configuration for :func:`create_x402_server`."""

    facilitator: X402FacilitatorChoice | Any = "http"
    """Facilitator selection — ``"coinbase"`` (requires ``coinbase-x402``), ``"http"``
    (public testnet facilitator), or any pre-built facilitator instance."""

    rails: list[X402SymbolicRail] = field(default_factory=list)
    """Symbolic rail names to register schemes for. Each gets v1+v2 dual-register
    applied. Requires the corresponding peer dep installed (``x402[evm]`` for base,
    ``x402[svm]`` for solana)."""

    schemes: list[CustomScheme] = field(default_factory=list)
    """Advanced: register custom (network, scheme) pairs in addition to ``rails``."""

    bazaar: bool = False
    """Register the Bazaar discovery extension. Requires the extension peer dep installed."""

    initialize: bool = True
    """Initialize the server immediately (calls facilitator). Default ``True``."""


def _import_optional(module_name: str) -> Any | None:
    """Try to import a module; return ``None`` if not installed."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


async def create_x402_server(
    facilitator: X402FacilitatorChoice | Any = "http",
    rails: Iterable[X402SymbolicRail] | None = None,
    schemes: Iterable[CustomScheme] | None = None,
    bazaar: bool = False,
    initialize: bool = True,
) -> Any:
    """One-call x402 server setup.

    Returns a configured ``x402ResourceServer`` instance. Raises ``ImportError``
    with a guiding install command when a required peer dep is missing.

    Async because the underlying ``initialize()`` call is async (talks to the
    facilitator).
    """
    rails_list = list(rails or [])
    schemes_list = list(schemes or [])

    # Eager validation — surface bad rail combinations before paying for peer-dep resolution.
    for rail in rails_list:
        if rail.startswith("x402-solana") and rail.endswith("-upto"):
            msg = f'Rail "{rail}" not supported — the Solana x402 scheme does not ship an upto variant yet (EVM-only).'
            raise ValueError(msg)

    # Core x402 package. The Python `x402` package re-exports the server primitives
    # under top-level submodules; we import them defensively to surface a clear
    # peer-dep error instead of an opaque AttributeError.
    x402_servers = _import_optional("x402.servers")
    x402_facilitator = _import_optional("x402.facilitator")
    if x402_servers is None or x402_facilitator is None:
        msg = "x402 not installed — run `pip install 'x402[evm,svm,fastapi]>=2.8,<3'` to use create_x402_server."
        raise ImportError(msg)

    facilitator_instance: Any
    if facilitator == "coinbase":
        # The Coinbase facilitator is reachable through the main `x402` package
        # — no separate Python peer dep. Prefer the official preset on `x402.facilitator`
        # if the SDK ships one; otherwise fall back to the published Coinbase URL.
        coinbase_preset = getattr(x402_facilitator, "coinbase_facilitator", None) or getattr(
            x402_facilitator, "COINBASE_FACILITATOR", None
        )
        if coinbase_preset is not None:
            facilitator_instance = x402_facilitator.HTTPFacilitatorClient(coinbase_preset)
        else:
            facilitator_instance = x402_facilitator.HTTPFacilitatorClient(
                "https://api.cdp.coinbase.com/platform/v2/x402",
            )
    elif facilitator == "http":
        facilitator_instance = x402_facilitator.HTTPFacilitatorClient()
    else:
        # Pre-built facilitator instance passed directly.
        facilitator_instance = facilitator

    server = x402_servers.x402ResourceServer(facilitator_instance)

    # Lazy-load scheme modules so vendors only need the peer deps for rails they use.
    evm_exact_module: Any | None = None
    evm_upto_module: Any | None = None
    svm_module: Any | None = None

    for rail in rails_list:
        is_upto = rail.endswith("-upto")
        if rail.startswith("x402-base"):
            base_rail = rail[:-5] if is_upto else rail
            network = networks.base.mainnet.caip2 if base_rail == "x402-base-mainnet" else networks.base.sepolia.caip2
            if is_upto:
                if evm_upto_module is None:
                    evm_upto_module = _import_optional("x402.schemes.upto.evm")
                if evm_upto_module is None or not getattr(evm_upto_module, "UptoEvmServerScheme", None):
                    msg = "x402[evm] not installed — run `pip install 'x402[evm]'` for x402 base upto rails."
                    raise ImportError(msg)
                register_x402_schemes_v1_v2(server, network, evm_upto_module.UptoEvmServerScheme())
            else:
                if evm_exact_module is None:
                    evm_exact_module = _import_optional("x402.schemes.exact.evm")
                if evm_exact_module is None or not getattr(evm_exact_module, "ExactEvmServerScheme", None):
                    msg = "x402[evm] not installed — run `pip install 'x402[evm]'` for x402 base rails."
                    raise ImportError(msg)
                register_x402_schemes_v1_v2(server, network, evm_exact_module.ExactEvmServerScheme())
        elif rail.startswith("x402-solana"):
            if svm_module is None:
                svm_module = _import_optional("x402.schemes.exact.svm")
            if svm_module is None or not getattr(svm_module, "ExactSvmServerScheme", None):
                msg = "x402[svm] not installed — run `pip install 'x402[svm]'` for x402 solana rails."
                raise ImportError(msg)
            network = networks.solana.mainnet.caip2 if rail == "x402-solana-mainnet" else networks.solana.devnet.caip2
            register_x402_schemes_v1_v2(server, network, svm_module.ExactSvmServerScheme())

    for custom in schemes_list:
        register_x402_schemes_v1_v2(server, custom.network, custom.scheme)

    if bazaar:
        bazaar_module = _import_optional("x402.extensions.bazaar")
        bazaar_ext = getattr(bazaar_module, "bazaar_resource_server_extension", None) if bazaar_module else None
        if bazaar_ext is None:
            msg = "x402[extensions] not installed — run `pip install 'x402[extensions]'` for bazaar discovery."
            raise ImportError(msg)
        register_extension = getattr(server, "register_extension", None)
        if not callable(register_extension):
            msg = "x402 server does not expose register_extension — bazaar registration unavailable."
            raise RuntimeError(msg)
        register_extension(bazaar_ext)

    if initialize:
        init_fn = getattr(server, "initialize", None)
        if callable(init_fn):
            await init_fn()

    return server


__all__ = [
    "CreateX402ServerOptions",
    "CustomScheme",
    "X402FacilitatorChoice",
    "X402SymbolicRail",
    "create_x402_server",
]
