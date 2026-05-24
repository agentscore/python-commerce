"""Live-API tests for create_x402_server + create_mppx_server factories.

The prior mock-based suite tracked x402 2.8 / pympp pre-release internal layout
(``x402.servers``, ``HTTPFacilitatorClient``, ``Mppx``, ``charge(currency=...)``).
x402 2.9 + pympp 0.6 shipped breaking refactors so the mocks no longer reflect
reality. We now run against the actually-installed peer deps — the tests skip
when the deps aren't present so the suite still runs in minimal envs.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest

from agentscore_commerce.payment import (
    StripeRailSpec,
    TempoRailSpec,
    TempoSessionRailSpec,
    create_mppx_server,
    create_x402_server,
)

_X402_INSTALLED = importlib.util.find_spec("x402") is not None
_MPPX_INSTALLED = importlib.util.find_spec("mpp.server") is not None
_TEMPO_INSTALLED = importlib.util.find_spec("mpp.methods.tempo") is not None


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_returns_resource_server() -> None:
    """create_x402_server with no rails returns a bare resource server."""
    server = await create_x402_server(facilitator="http", initialize=False)
    assert type(server).__name__ == "x402ResourceServer"
    assert hasattr(server, "verify_payment")
    assert hasattr(server, "settle_payment")
    assert hasattr(server, "register")


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_registers_base_sepolia_scheme() -> None:
    """Base Sepolia rail registers the ExactEvmScheme on the right network."""
    server = await create_x402_server(
        facilitator="http",
        rails=["x402-base-sepolia"],
        initialize=False,
    )
    # x402 2.9 keeps registrations in `_schemes: {network: {scheme_name: instance}}`.
    assert "eip155:84532" in server._schemes
    assert "exact" in server._schemes["eip155:84532"]


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_coinbase_facilitator_wires_cdp_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    """``facilitator="coinbase"`` builds an HTTPFacilitatorClient pointed at the
    CDP URL with a per-endpoint JWT auth provider — not a bare in-process facilitator.

    This is the regression that 1.3.2 fixes: 1.3.0 + 1.3.1 both passed an empty
    ``x402Facilitator()`` instance and silently failed downstream when
    ``build_payment_requirements`` looked up the supported map.
    """
    cdp_sdk_installed = importlib.util.find_spec("cdp.auth.utils.jwt") is not None
    if not cdp_sdk_installed:
        pytest.skip("cdp-sdk not installed (install via the `coinbase` extra)")

    monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-key-secret")

    server = await create_x402_server(
        facilitator="coinbase",
        rails=["x402-base-mainnet"],
        initialize=False,
    )
    facilitator_clients = server._facilitator_clients
    assert len(facilitator_clients) == 1
    facilitator = facilitator_clients[0]
    assert type(facilitator).__name__ == "HTTPFacilitatorClient"
    assert facilitator.url == "https://api.cdp.coinbase.com/platform/v2/x402"
    assert facilitator._auth_provider is not None


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_coinbase_without_creds_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``facilitator="coinbase"`` with no CDP creds raises a clear ValueError."""
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)

    with pytest.raises(ValueError, match="CDP_API_KEY_ID and CDP_API_KEY_SECRET"):
        await create_x402_server(facilitator="coinbase", rails=["x402-base-mainnet"], initialize=False)


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
def test_build_x402_accepts_for_402_returns_dicts_from_typed_requirements() -> None:
    """``build_x402_accepts_for_402`` wraps ``server.build_payment_requirements`` and
    returns the requirements as wire-shape dicts (via ``model_dump(by_alias=True, mode="json")``)
    so merchants can drop them straight into the 402 response body without importing
    Pydantic types or remembering to serialize.
    """
    from x402.schemas import PaymentRequirements

    from agentscore_commerce.payment import build_x402_accepts_for_402

    captured: dict = {}

    class _CapturingServer:
        def build_payment_requirements(self, config, _ext=None):
            captured["config"] = config
            return [
                PaymentRequirements(
                    scheme="exact",
                    network="eip155:8453",
                    amount="100000",
                    asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    pay_to="0x000000000000000000000000000000000000dEaD",
                    max_timeout_seconds=300,
                    extra={"name": "USD Coin", "version": "2"},
                )
            ]

    accepts = build_x402_accepts_for_402(
        _CapturingServer(),
        network="eip155:8453",
        price="$0.10",
        pay_to="0x000000000000000000000000000000000000dEaD",
    )
    # Caller-side: a typed ResourceConfig is passed to build_payment_requirements.
    cfg = captured["config"]
    assert cfg.network == "eip155:8453"
    assert cfg.pay_to == "0x000000000000000000000000000000000000dEaD"
    assert cfg.max_timeout_seconds == 300
    # Returned shape: wire-form dicts, not Pydantic models.
    assert isinstance(accepts, list)
    assert len(accepts) == 1
    assert isinstance(accepts[0], dict)
    assert accepts[0]["network"] == "eip155:8453"
    # Camel-case keys (by_alias=True) — facilitator + clients expect this shape.
    assert accepts[0]["payTo"] == "0x000000000000000000000000000000000000dEaD"
    assert accepts[0]["maxTimeoutSeconds"] == 300
    assert accepts[0]["extra"] == {"name": "USD Coin", "version": "2"}


@pytest.mark.skipif(not _MPPX_INSTALLED or not _TEMPO_INSTALLED, reason="pympp[tempo] not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_tempo_returns_mpp_instance() -> None:
    """create_mppx_server with a Tempo charge rail returns a configured Mpp."""
    server = await create_mppx_server(
        secret_key="X" * 32,
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 20, testnet=True)},
    )
    assert type(server).__name__ == "Mpp"
    # pympp 0.6 exposes intent-named methods directly (charge, pay, …) on the Mpp instance.
    assert callable(getattr(server, "charge", None))


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_no_method_or_rails_raises() -> None:
    with pytest.raises(ValueError, match="no method or rails"):
        await create_mppx_server(secret_key="X" * 32)


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_tempo_session_raises_until_pympp_supports_it() -> None:
    with pytest.raises(ImportError, match="SessionIntent"):
        await create_mppx_server(
            secret_key="X" * 32,
            rails={
                "tempo_session": TempoSessionRailSpec(
                    recipient="0x" + "00" * 20,
                    escrow_contract="0x" + "11" * 20,
                    store=object(),
                ),
            },
        )


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_stripe_requires_secret_key() -> None:
    with pytest.raises(ValueError, match="profile_id and secret_key"):
        await create_mppx_server(
            secret_key="X" * 32,
            rails={"stripe": StripeRailSpec(profile_id="profile_x")},
        )


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_unknown_rail_spec_raises() -> None:
    with pytest.raises(TypeError, match="unsupported rail spec"):
        await create_mppx_server(
            secret_key="X" * 32,
            rails={"weird": "not-a-spec"},  # type: ignore[dict-item]
        )


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_auto_promotes_to_coinbase_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When facilitator='http' but CDP env vars are present, auto-promote to Coinbase."""
    cdp_installed = importlib.util.find_spec("cdp.auth.utils.jwt") is not None
    if not cdp_installed:
        pytest.skip("cdp-sdk not installed")
    monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-key-secret")

    server = await create_x402_server(facilitator="http", initialize=False)
    facilitator = server._facilitator_clients[0]
    assert facilitator.url == "https://api.cdp.coinbase.com/platform/v2/x402"


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_passthrough_prebuilt_facilitator() -> None:
    """A non-string facilitator argument is passed through verbatim."""
    sentinel = object()
    server = await create_x402_server(facilitator=sentinel, initialize=False)
    assert server._facilitator_clients[0] is sentinel


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_upto_rail_registers_upto_scheme() -> None:
    server = await create_x402_server(
        facilitator="http",
        rails=["x402-base-sepolia-upto"],
        initialize=False,
    )
    assert "eip155:84532" in server._schemes
    # upto schemes register under their canonical scheme name in pympp 0.6+.
    sepolia_schemes = server._schemes["eip155:84532"]
    assert len(sepolia_schemes) >= 1


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_custom_scheme_registered() -> None:
    """A CustomScheme entry is registered alongside symbolic rails."""
    from x402.mechanisms.evm.exact.server import ExactEvmScheme

    from agentscore_commerce.payment import CustomScheme

    custom = CustomScheme(network="eip155:8453", scheme=ExactEvmScheme())
    server = await create_x402_server(
        facilitator="http",
        schemes=[custom],
        initialize=False,
    )
    assert "eip155:8453" in server._schemes


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_bazaar_registers_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bazaar=True` looks up the extension and calls server.register_extension."""
    # `find_spec` triggers a real import via x402's __init__, so guard against
    # transitive ImportError (jsonschema / idna are part of `x402[extensions]`).
    try:
        import x402.extensions.bazaar
    except ImportError:
        pytest.skip("x402[extensions] not installed (jsonschema/idna unavailable)")

    registered: list[Any] = []

    # Patch register_extension on the class BEFORE creating the server so the
    # bazaar branch records its extension call.
    import x402

    orig_init = x402.x402ResourceServer.__init__

    def patched_init(self: Any, **kw: Any) -> None:
        orig_init(self, **kw)
        self.register_extension = registered.append  # type: ignore[attr-defined]

    monkeypatch.setattr(x402.x402ResourceServer, "__init__", patched_init)
    await create_x402_server(
        facilitator=object(),  # opaque facilitator; never called with initialize=False
        bazaar=True,
        initialize=False,
    )
    assert len(registered) == 1


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
def test_build_x402_accepts_for_402_accepts_dict_requirements() -> None:
    """`build_payment_requirements` may return plain dicts (older versions / stubs)."""
    from agentscore_commerce.payment import build_x402_accepts_for_402

    class _DictServer:
        def build_payment_requirements(self, _config: Any, _ext: Any = None) -> list[dict[str, Any]]:
            return [{"scheme": "exact", "network": "eip155:8453", "payTo": "0xDEAD"}]

    accepts = build_x402_accepts_for_402(
        _DictServer(),
        network="eip155:8453",
        price="$0.10",
        pay_to="0x000000000000000000000000000000000000dEaD",
    )
    assert accepts == [{"scheme": "exact", "network": "eip155:8453", "payTo": "0xDEAD"}]


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
def test_build_x402_accepts_for_402_rejects_unknown_requirement_type() -> None:
    """Anything other than a Pydantic model or dict raises TypeError."""
    from agentscore_commerce.payment import build_x402_accepts_for_402

    class _BadServer:
        def build_payment_requirements(self, _config: Any, _ext: Any = None) -> list[object]:
            return [object()]

    with pytest.raises(TypeError, match="expected a Pydantic PaymentRequirements or a dict"):
        build_x402_accepts_for_402(
            _BadServer(),
            network="eip155:8453",
            price="$0.10",
            pay_to="0x000000000000000000000000000000000000dEaD",
        )


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_coinbase_facilitator_emits_per_endpoint_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Coinbase auth provider mints a per-endpoint JWT via cdp-sdk's generate_jwt.

    Exercises `_mint_bearer` + `_create_headers` so they show up in coverage.
    Mocks the real cdp JWT signer so the test doesn't need a valid EC private key.
    """
    cdp_installed = importlib.util.find_spec("cdp.auth.utils.jwt") is not None
    if not cdp_installed:
        pytest.skip("cdp-sdk not installed")
    monkeypatch.setenv("CDP_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "test-secret")
    import cdp.auth.utils.jwt as cdp_jwt

    captured: list[tuple[str, str]] = []

    def _fake_generate_jwt(options: Any) -> str:
        captured.append((options.request_method, options.request_path))
        return "fake-jwt-token"

    monkeypatch.setattr(cdp_jwt, "generate_jwt", _fake_generate_jwt)

    server = await create_x402_server(
        facilitator="coinbase",
        rails=["x402-base-mainnet"],
        initialize=False,
    )
    facilitator = server._facilitator_clients[0]
    auth_provider = facilitator._auth_provider
    headers = auth_provider.get_auth_headers()
    assert headers.verify["Authorization"] == "Bearer fake-jwt-token"
    assert headers.settle["Authorization"] == "Bearer fake-jwt-token"
    assert headers.supported["Authorization"] == "Bearer fake-jwt-token"
    # All three endpoints were minted distinct JWTs.
    assert len(captured) == 3
    methods = [c[0] for c in captured]
    assert "POST" in methods  # verify + settle
    assert "GET" in methods  # supported


# ---------------------------------------------------------------------------
# Peer-dep ImportError paths + stripe rail + realm (mppx_server branch gaps)
# ---------------------------------------------------------------------------


def _block_import(monkeypatch: pytest.MonkeyPatch, *blocked_prefixes: str) -> None:
    """Make importlib.import_module raise ImportError for the given module prefixes."""
    import importlib

    real = importlib.import_module

    def _fake(name: str, *args: Any, **kwargs: Any) -> Any:
        if any(name == p or name.startswith(p) for p in blocked_prefixes):
            msg = f"blocked {name}"
            raise ImportError(msg)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _fake)


@pytest.mark.asyncio
async def test_create_mppx_server_missing_pympp_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When `mpp.server` is unavailable, a guiding ImportError names the install command."""
    _block_import(monkeypatch, "mpp.server")
    with pytest.raises(ImportError, match=r"pympp not installed"):
        await create_mppx_server(secret_key="X" * 32, rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 20)})


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_missing_tempo_factory_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When `mpp.methods.tempo` is unavailable, the tempo rail raises a guiding ImportError."""
    _block_import(monkeypatch, "mpp.methods.tempo")
    with pytest.raises(ImportError, match=r"pympp\[tempo\] not installed"):
        await create_mppx_server(
            secret_key="X" * 32,
            rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 20, testnet=True)},
        )


@pytest.mark.skipif(not _MPPX_INSTALLED or not _TEMPO_INSTALLED, reason="pympp[tempo] not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_missing_charge_intent_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tempo module present but missing ChargeIntent raises the upgrade hint."""
    import mpp.methods.tempo as tempo_mod

    # Make getattr(module, "ChargeIntent", None) return None without removing `tempo`.
    monkeypatch.setattr(tempo_mod, "ChargeIntent", None, raising=False)
    with pytest.raises(ImportError, match=r"missing ChargeIntent"):
        await create_mppx_server(
            secret_key="X" * 32,
            rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 20, testnet=True)},
        )


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_passes_realm_through() -> None:
    """A non-None `realm` is threaded into Mpp.create."""
    server = await create_mppx_server(
        secret_key="X" * 32,
        rails={"tempo": TempoRailSpec(recipient="0x" + "00" * 20, testnet=True)},
        realm="my-realm",
    )
    assert type(server).__name__ == "Mpp"


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_stripe_rail_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fully-specified StripeRailSpec resolves a stripe method and constructs an Mpp.

    ``create_mppx_stripe`` is stubbed because the installed pympp build doesn't ship a
    working ``stripe/charge`` factory; this exercises the stripe branch (resolve + break)
    in ``create_mppx_server`` without depending on that peer-dep detail.
    """

    async def _fake_create_stripe(**_kwargs: Any) -> Any:
        return object()

    monkeypatch.setattr("agentscore_commerce.stripe_multichain.mppx_stripe.create_mppx_stripe", _fake_create_stripe)
    server = await create_mppx_server(
        secret_key="X" * 32,
        rails={"stripe": StripeRailSpec(profile_id="profile_x", secret_key="sk_test_x")},
    )
    assert type(server).__name__ == "Mpp"


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_with_prebuilt_method_skips_rail_resolution() -> None:
    """Passing `method=` directly skips the rail-resolution loop entirely (127->145)."""
    import mpp.methods.tempo as tempo_mod

    method = tempo_mod.tempo(
        intents={"charge": tempo_mod.ChargeIntent()},
        currency="0x" + "11" * 20,
        recipient="0x" + "00" * 20,
        chain_id=42431,
    )
    server = await create_mppx_server(secret_key="X" * 32, method=method)
    assert type(server).__name__ == "Mpp"


# ---------------------------------------------------------------------------
# x402_server peer-dep + error-path branch gaps
# ---------------------------------------------------------------------------


def test__import_optional_returns_none_for_missing_module() -> None:
    from agentscore_commerce.payment.x402_server import _import_optional

    assert _import_optional("totally_nonexistent_module_xyz") is None
    assert _import_optional("os") is not None


@pytest.mark.asyncio
async def test_create_x402_server_missing_x402_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the top-level `x402` package is unavailable, a guiding ImportError fires."""
    import agentscore_commerce.payment.x402_server as mod

    monkeypatch.setattr(mod, "_import_optional", lambda _name: None)
    with pytest.raises(ImportError, match=r"x402 not installed"):
        await create_x402_server(facilitator="http", initialize=False)


@pytest.mark.asyncio
async def test_create_x402_server_http_facilitator_missing_client_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`facilitator='http'` but x402.http lacks HTTPFacilitatorClient → ImportError."""
    import agentscore_commerce.payment.x402_server as mod

    x402_top = type("X", (), {"x402ResourceServer": object})()

    def _fake_import(name: str) -> Any:
        if name == "x402":
            return x402_top
        if name == "x402.http":
            return type("H", (), {})()  # no HTTPFacilitatorClient
        return None

    monkeypatch.setattr(mod, "_import_optional", _fake_import)
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)
    with pytest.raises(ImportError, match=r"x402.http missing HTTPFacilitatorClient"):
        await create_x402_server(facilitator="http", initialize=False)


@pytest.mark.asyncio
async def test_create_x402_server_coinbase_missing_cdp_sdk_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`facilitator='coinbase'` with creds but no cdp-sdk → guiding ImportError."""
    import agentscore_commerce.payment.x402_server as mod

    x402_top = type("X", (), {"x402ResourceServer": object})()

    def _fake_import(name: str) -> Any:
        if name == "x402":
            return x402_top
        return None  # cdp.auth.utils.jwt missing

    monkeypatch.setattr(mod, "_import_optional", _fake_import)
    monkeypatch.setenv("CDP_API_KEY_ID", "id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "secret")
    with pytest.raises(ImportError, match=r"cdp-sdk not installed"):
        await create_x402_server(facilitator="coinbase", initialize=False)


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_upto_scheme_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An upto rail whose scheme module/class is missing raises an x402[evm] hint."""
    import agentscore_commerce.payment.x402_server as mod

    real = mod._import_optional

    def _fake_import(name: str) -> Any:
        if name == "x402.mechanisms.evm.upto.server":
            return None
        return real(name)

    monkeypatch.setattr(mod, "_import_optional", _fake_import)
    with pytest.raises(ImportError, match=r"x402\[evm\] not installed"):
        await create_x402_server(facilitator="http", rails=["x402-base-sepolia-upto"], initialize=False)


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_exact_scheme_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exact rail whose scheme module/class is missing raises an x402[evm] hint."""
    import agentscore_commerce.payment.x402_server as mod

    real = mod._import_optional

    def _fake_import(name: str) -> Any:
        if name == "x402.mechanisms.evm.exact.server":
            return None
        return real(name)

    monkeypatch.setattr(mod, "_import_optional", _fake_import)
    with pytest.raises(ImportError, match=r"x402\[evm\] not installed"):
        await create_x402_server(facilitator="http", rails=["x402-base-mainnet"], initialize=False)


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_bazaar_missing_extension_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bazaar=True` but the bazaar extension is unavailable → guiding ImportError."""
    import agentscore_commerce.payment.x402_server as mod

    real = mod._import_optional

    def _fake_import(name: str) -> Any:
        if name == "x402.extensions.bazaar":
            return None
        return real(name)

    monkeypatch.setattr(mod, "_import_optional", _fake_import)
    with pytest.raises(ImportError, match=r"x402\[extensions\] not installed"):
        await create_x402_server(facilitator=object(), bazaar=True, initialize=False)


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_bazaar_server_missing_register_extension_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server without register_extension raises a clear RuntimeError when bazaar=True."""
    import x402

    import agentscore_commerce.payment.x402_server as mod

    real = mod._import_optional

    def _fake_import(name: str) -> Any:
        if name == "x402.extensions.bazaar":
            return type("B", (), {"bazaar_resource_server_extension": object()})()
        return real(name)

    orig_init = x402.x402ResourceServer.__init__

    def patched_init(self: Any, **kw: Any) -> None:
        orig_init(self, **kw)
        # Drop register_extension so the callable check fails.
        self.register_extension = "not-callable"

    monkeypatch.setattr(mod, "_import_optional", _fake_import)
    monkeypatch.setattr(x402.x402ResourceServer, "__init__", patched_init)
    with pytest.raises(RuntimeError, match=r"does not expose register_extension"):
        await create_x402_server(facilitator=object(), bazaar=True, initialize=False)


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_awaits_async_initialize(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the server's initialize() returns an awaitable, create_x402_server awaits it."""
    import x402

    awaited: list[bool] = []

    async def _async_init(self: Any) -> None:
        awaited.append(True)

    monkeypatch.setattr(x402.x402ResourceServer, "initialize", _async_init, raising=False)
    server = await create_x402_server(facilitator="http", initialize=True)
    assert awaited == [True]
    assert type(server).__name__ == "x402ResourceServer"


def test_build_x402_accepts_for_402_missing_x402_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_x402_accepts_for_402 raises a guiding ImportError when x402 is absent."""
    import agentscore_commerce.payment.x402_server as mod
    from agentscore_commerce.payment import build_x402_accepts_for_402

    monkeypatch.setattr(mod, "_import_optional", lambda _name: None)
    with pytest.raises(ImportError, match=r"x402 not installed"):
        build_x402_accepts_for_402(object(), network="eip155:8453", price="$0.10", pay_to="0xDEAD")


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_sync_initialize_not_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sync initialize() (x402 2.9 shape) is called but not awaited."""
    import x402

    calls: list[bool] = []

    def _sync_init(self: Any) -> None:
        calls.append(True)

    monkeypatch.setattr(x402.x402ResourceServer, "initialize", _sync_init, raising=False)
    server = await create_x402_server(facilitator="http", initialize=True)
    assert calls == [True]
    assert type(server).__name__ == "x402ResourceServer"


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_initialize_noncallable_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server whose `initialize` attr isn't callable is skipped without error."""
    import x402

    monkeypatch.setattr(x402.x402ResourceServer, "initialize", "not-callable", raising=False)
    server = await create_x402_server(facilitator="http", initialize=True)
    assert type(server).__name__ == "x402ResourceServer"


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_non_base_rail_is_ignored() -> None:
    """A rail string that doesn't start with `x402-base` is silently skipped (no scheme)."""
    server = await create_x402_server(
        facilitator="http",
        rails=["x402-unknown-network"],  # type: ignore[list-item]
        initialize=False,
    )
    # No EVM scheme registered for the unrecognized rail.
    assert "eip155:8453" not in getattr(server, "_schemes", {})


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_two_exact_rails_reuse_module() -> None:
    """Two exact rails exercise the `evm_exact_module is None` cache reuse branch."""
    server = await create_x402_server(
        facilitator="http",
        rails=["x402-base-mainnet", "x402-base-sepolia"],
        initialize=False,
    )
    assert "eip155:8453" in server._schemes
    assert "eip155:84532" in server._schemes


@pytest.mark.skipif(not _X402_INSTALLED, reason="x402 peer dep not installed")
@pytest.mark.asyncio
async def test_create_x402_server_two_upto_rails_reuse_module() -> None:
    """Two upto rails exercise the `evm_upto_module is None` cache reuse branch."""
    server = await create_x402_server(
        facilitator="http",
        rails=["x402-base-mainnet-upto", "x402-base-sepolia-upto"],
        initialize=False,
    )
    assert "eip155:8453" in server._schemes
    assert "eip155:84532" in server._schemes


@pytest.mark.asyncio
async def test_coinbase_facilitator_missing_facilitator_config_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coinbase path with cdp present but x402.http lacking FacilitatorConfig → ImportError."""
    import agentscore_commerce.payment.x402_server as mod

    x402_top = type("X", (), {"x402ResourceServer": object})()
    fake_cdp = type(
        "J",
        (),
        {"JwtOptions": object, "generate_jwt": staticmethod(lambda _o: "jwt")},
    )()

    def _fake_import(name: str) -> Any:
        if name == "x402":
            return x402_top
        if name == "cdp.auth.utils.jwt":
            return fake_cdp
        if name == "x402.http":
            return type("H", (), {})()  # no FacilitatorConfig / HTTPFacilitatorClient
        return None

    monkeypatch.setattr(mod, "_import_optional", _fake_import)
    monkeypatch.setenv("CDP_API_KEY_ID", "id")
    monkeypatch.setenv("CDP_API_KEY_SECRET", "secret")
    with pytest.raises(ImportError, match=r"FacilitatorConfig / HTTPFacilitatorClient"):
        await create_x402_server(facilitator="coinbase", initialize=False)
