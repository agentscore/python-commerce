"""One-call x402 server setup wrapping the official `x402` Python package.

Resolves the facilitator, constructs the server, registers schemes per network
with v1+v2 dual-register, and optionally adds the Bazaar discovery extension.

Replaces ~15 lines of boilerplate with a single config call::

    from agentscore_commerce.payment import create_x402_server

    server = await create_x402_server(
        facilitator="coinbase",
        rails=["x402-base-mainnet"],
        bazaar=True,
    )

`x402` is an OPTIONAL peer dependency — install only the schemes you use::

    pip install 'x402[evm,fastapi]>=2.9,<3'   # for non-Coinbase facilitators
    pip install 'agentscore-commerce[x402,coinbase]'   # for the Coinbase facilitator (adds cdp-sdk)
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentscore_commerce.payment.networks import networks

if TYPE_CHECKING:
    from collections.abc import Iterable

COINBASE_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"

X402SymbolicRail = Literal[
    "x402-base-mainnet",
    "x402-base-sepolia",
    "x402-base-mainnet-upto",
    "x402-base-sepolia-upto",
]

X402FacilitatorChoice = Literal["coinbase", "http"]


@dataclass(frozen=True)
class CustomScheme:
    """Custom (network, scheme-instance) pair to register beyond the symbolic rails."""

    network: str
    scheme: Any


def _import_optional(module_name: str) -> Any | None:
    """Try to import a module; return ``None`` if not installed."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def _build_coinbase_facilitator(
    x402_top: Any,
    api_key_id: str | None,
    api_key_secret: str | None,
) -> Any:
    """Build a ``HTTPFacilitatorClient`` pointed at the Coinbase facilitator with CDP JWT auth.

    Uses ``cdp-sdk``'s ``generate_jwt`` to mint a per-endpoint Bearer token (CDP rotates
    JWTs every 120s by default). Mirrors the TS ``@coinbase/x402`` package's
    ``createCdpAuthHeaders`` shape exactly so the verify / settle / supported routes
    each get a JWT scoped to their HTTP method + path.
    """
    api_key_id = api_key_id or os.environ.get("CDP_API_KEY_ID")
    api_key_secret = api_key_secret or os.environ.get("CDP_API_KEY_SECRET")
    if not api_key_id or not api_key_secret:
        msg = (
            "facilitator='coinbase' requires CDP_API_KEY_ID and CDP_API_KEY_SECRET — "
            "set them as env vars or pass cdp_api_key_id / cdp_api_key_secret to "
            "create_x402_server."
        )
        raise ValueError(msg)

    cdp_jwt_module = _import_optional("cdp.auth.utils.jwt")
    if cdp_jwt_module is None:
        msg = (
            "cdp-sdk not installed — run `pip install 'agentscore-commerce[coinbase]'` "
            "(or `pip install cdp-sdk`) to use facilitator='coinbase'."
        )
        raise ImportError(msg)

    http_module = _import_optional("x402.http")
    facilitator_config_cls = getattr(http_module, "FacilitatorConfig", None) if http_module else None
    facilitator_client_cls = getattr(http_module, "HTTPFacilitatorClient", None) if http_module else None
    if facilitator_config_cls is None or facilitator_client_cls is None:
        msg = "x402.http missing FacilitatorConfig / HTTPFacilitatorClient — upgrade x402>=2.9."
        raise ImportError(msg)

    facilitator_url = COINBASE_FACILITATOR_URL
    request_host = facilitator_url.split("://", 1)[1].split("/", 1)[0]
    request_path = "/" + facilitator_url.split("://", 1)[1].split("/", 1)[1]
    jwt_options_cls = cdp_jwt_module.JwtOptions
    generate_jwt = cdp_jwt_module.generate_jwt

    def _mint_bearer(method: str, path: str) -> str:
        token = generate_jwt(
            jwt_options_cls(
                api_key_id=api_key_id,
                api_key_secret=api_key_secret,
                request_method=method,
                request_host=request_host,
                request_path=path,
            )
        )
        return f"Bearer {token}"

    def _create_headers() -> dict[str, dict[str, str]]:
        return {
            "verify": {"Authorization": _mint_bearer("POST", f"{request_path}/verify")},
            "settle": {"Authorization": _mint_bearer("POST", f"{request_path}/settle")},
            "supported": {"Authorization": _mint_bearer("GET", f"{request_path}/supported")},
        }

    create_headers_provider_cls = getattr(http_module, "CreateHeadersAuthProvider", None)
    config = facilitator_config_cls(
        url=facilitator_url,
        auth_provider=create_headers_provider_cls(_create_headers) if create_headers_provider_cls else None,
    )
    return facilitator_client_cls(config)


async def create_x402_server(
    facilitator: X402FacilitatorChoice | Any = "http",
    rails: Iterable[X402SymbolicRail] | None = None,
    schemes: Iterable[CustomScheme] | None = None,
    bazaar: bool = False,
    initialize: bool = True,
    cdp_api_key_id: str | None = None,
    cdp_api_key_secret: str | None = None,
) -> Any:
    """One-call x402 server setup.

    Returns a configured ``x402ResourceServer`` instance. Raises ``ImportError``
    with a guiding install command when a required peer dep is missing.

    Async because the underlying ``initialize()`` call is async (talks to the
    facilitator).
    """
    rails_list = list(rails or [])
    schemes_list = list(schemes or [])

    # x402 2.9 layout: top-level `x402` package (with `x402` re-exports of
    # `x402ResourceServer`, `x402Facilitator`); schemes under
    # `x402.mechanisms.evm.{exact,upto}.server`. The 2.8-era v1+v2 dual
    # register helper is obsolete — `register()` is v2 only and the resource
    # server handles v1 fallback internally via the facilitator.
    x402_top = _import_optional("x402")
    if x402_top is None or not hasattr(x402_top, "x402ResourceServer"):
        msg = "x402 not installed — run `pip install 'x402[evm,fastapi]>=2.9,<3'` to use create_x402_server."
        raise ImportError(msg)

    # Auto-select the Coinbase CDP facilitator when both env vars are present.
    # Lets merchants drop the `facilitator: 'coinbase' if env else 'http'` ternary.
    # Explicit `facilitator=` arg still wins.
    import os

    if facilitator == "http" and os.environ.get("CDP_API_KEY_ID") and os.environ.get("CDP_API_KEY_SECRET"):
        facilitator = "coinbase"

    facilitator_instance: Any
    if facilitator == "coinbase":
        # Coinbase's x402 facilitator at api.cdp.coinbase.com requires a JWT
        # bearer per endpoint signed with the CDP API key. A bare x402Facilitator()
        # does NOT auto-pick up CDP creds — the public docs implying otherwise
        # are wrong. Build an HTTPFacilitatorClient with a CreateHeadersAuthProvider
        # that mints per-endpoint JWTs via cdp-sdk.
        facilitator_instance = _build_coinbase_facilitator(x402_top, cdp_api_key_id, cdp_api_key_secret)
    elif facilitator == "http":
        # Public x402.org testnet facilitator. HTTPFacilitatorClient with no auth.
        http_module = _import_optional("x402.http")
        facilitator_client_cls = getattr(http_module, "HTTPFacilitatorClient", None) if http_module else None
        if facilitator_client_cls is None:
            msg = "x402.http missing HTTPFacilitatorClient — upgrade x402>=2.9."
            raise ImportError(msg)
        facilitator_instance = facilitator_client_cls()
    else:
        # Pre-built facilitator instance passed directly.
        facilitator_instance = facilitator

    server = x402_top.x402ResourceServer(facilitator_clients=facilitator_instance)

    # Lazy-load scheme modules so vendors only need the peer deps for rails they use.
    evm_exact_module: Any | None = None
    evm_upto_module: Any | None = None

    for rail in rails_list:
        is_upto = rail.endswith("-upto")
        if rail.startswith("x402-base"):
            base_rail = rail[:-5] if is_upto else rail
            network = networks.base.mainnet.caip2 if base_rail == "x402-base-mainnet" else networks.base.sepolia.caip2
            if is_upto:
                if evm_upto_module is None:
                    evm_upto_module = _import_optional("x402.mechanisms.evm.upto.server")
                scheme_cls = getattr(evm_upto_module, "UptoEvmScheme", None) if evm_upto_module else None
                if scheme_cls is None:
                    msg = "x402[evm] not installed — run `pip install 'x402[evm]'` for x402 base upto rails."
                    raise ImportError(msg)
                server.register(network, scheme_cls())
            else:
                if evm_exact_module is None:
                    evm_exact_module = _import_optional("x402.mechanisms.evm.exact.server")
                scheme_cls = getattr(evm_exact_module, "ExactEvmScheme", None) if evm_exact_module else None
                if scheme_cls is None:
                    msg = "x402[evm] not installed — run `pip install 'x402[evm]'` for x402 base rails."
                    raise ImportError(msg)
                server.register(network, scheme_cls())

    for custom in schemes_list:
        server.register(custom.network, custom.scheme)

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
            result = init_fn()
            # x402 2.9 made initialize() sync; older versions had it async.
            # Await only if the call returned an awaitable to stay compatible.
            if hasattr(result, "__await__"):
                await result

    return server


def build_x402_accepts_for_402(
    server: Any,
    *,
    network: str,
    price: str,
    pay_to: str,
    scheme: str = "exact",
    max_timeout_seconds: int = 300,
    extensions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build x402 ``accepts[]`` entries for a 402 challenge body.

    Wraps ``server.build_payment_requirements(...)`` so merchants don't have to:

    1. Import ``x402.schemas.config.ResourceConfig`` themselves
    2. Remember to call ``model_dump(by_alias=True, mode="json")`` on each Pydantic
       requirement so the surrounding JSON response can serialize it
    3. Hardcode ``extra`` (which differs by the actual on-chain contract: base mainnet
       USDC has ``name="USD Coin"``, base sepolia USDC has ``name="USDC"`` — EIP-712
       domain hashes differ, so getting this wrong silently breaks every signature
       verify at the facilitator)

    Returns a list of plain dicts in the shape that x402 expects on the wire — drop
    them straight into the ``accepts`` field of the 402 challenge body.

    Raises ``Exception`` if the underlying ``build_payment_requirements`` raises;
    callers should wrap with ``try/except`` and either omit x402 from the 402 or
    surface a 5xx (depending on whether other rails are advertised).
    """
    config_cls_module = _import_optional("x402.schemas.config")
    config_cls = getattr(config_cls_module, "ResourceConfig", None) if config_cls_module else None
    if config_cls is None:
        msg = "x402 not installed — run `pip install 'x402[evm,fastapi]>=2.9,<3'` to use build_x402_accepts_for_402."
        raise ImportError(msg)
    config = config_cls(
        scheme=scheme,
        network=network,
        price=price,
        pay_to=pay_to,
        max_timeout_seconds=max_timeout_seconds,
    )
    requirements = (
        server.build_payment_requirements(config, extensions)
        if extensions
        else server.build_payment_requirements(config)
    )
    # Pydantic ``PaymentRequirements`` is the live shape under x402 2.9+. Older
    # versions (and test stubs) return plain dicts that already match the wire form.
    out: list[dict[str, Any]] = []
    for req in requirements:
        model_dump = getattr(req, "model_dump", None)
        if callable(model_dump):
            out.append(model_dump(by_alias=True, mode="json"))
        elif isinstance(req, dict):
            out.append(dict(req))
        else:
            msg = (
                f"build_payment_requirements returned {type(req).__name__}; expected a "
                "Pydantic PaymentRequirements or a dict."
            )
            raise TypeError(msg)
    return out


__all__ = [
    "CustomScheme",
    "X402FacilitatorChoice",
    "X402SymbolicRail",
    "build_x402_accepts_for_402",
    "create_x402_server",
]
