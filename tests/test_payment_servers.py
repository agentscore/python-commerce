"""Tests for create_x402_server + create_mppx_server factories.

Both peer deps (x402, pympp) are mocked here so the test suite has no real
network calls and no hard dependency on the official SDKs being installed.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentscore_commerce.payment import (
    CustomScheme,
    MppxRails,
    StripeRail,
    TempoChargeRail,
    TempoSessionRail,
    create_mppx_server,
    create_x402_server,
)

# ---------------------------------------------------------------------------
# Fake module helpers for peer-dep mocking
# ---------------------------------------------------------------------------


class _FakeServer:
    """Stand-in for x402ResourceServer + pympp Mppx instance."""

    def __init__(self, facilitator: Any | None = None) -> None:
        self.facilitator = facilitator
        self.registered: list[tuple[str, Any]] = []
        self.registered_v1: list[tuple[str, Any]] = []
        self.extensions: list[Any] = []
        self.initialized = False

    def register(self, network: str, scheme: Any) -> None:
        self.registered.append((network, scheme))

    def register_v1(self, network: str, scheme: Any) -> None:
        self.registered_v1.append((network, scheme))

    def register_extension(self, ext: Any) -> None:
        self.extensions.append(ext)

    async def initialize(self) -> None:
        self.initialized = True


def _install_x402_modules(monkeypatch: pytest.MonkeyPatch) -> dict[str, ModuleType]:
    """Install fake `x402.servers` + `x402.facilitator` + scheme modules into sys.modules."""
    fake_servers = ModuleType("x402.servers")
    fake_servers.x402ResourceServer = _FakeServer  # type: ignore[attr-defined]

    fake_facilitator = ModuleType("x402.facilitator")
    facilitator_calls: list[Any] = []

    class _FakeFacilitator:
        def __init__(self, upstream: Any | None = None) -> None:
            facilitator_calls.append(upstream)
            self.upstream = upstream

    fake_facilitator.HTTPFacilitatorClient = _FakeFacilitator  # type: ignore[attr-defined]

    fake_evm_exact = ModuleType("x402.schemes.exact.evm")
    fake_evm_exact.ExactEvmServerScheme = MagicMock(name="ExactEvmServerScheme")  # type: ignore[attr-defined]

    fake_evm_upto = ModuleType("x402.schemes.upto.evm")
    fake_evm_upto.UptoEvmServerScheme = MagicMock(name="UptoEvmServerScheme")  # type: ignore[attr-defined]

    fake_svm = ModuleType("x402.schemes.exact.svm")
    fake_svm.ExactSvmServerScheme = MagicMock(name="ExactSvmServerScheme")  # type: ignore[attr-defined]

    fake_bazaar = ModuleType("x402.extensions.bazaar")
    fake_bazaar.bazaar_resource_server_extension = SimpleNamespace(  # type: ignore[attr-defined]
        name="bazaar_extension",
    )

    fake_coinbase = ModuleType("coinbase_x402")
    fake_coinbase.facilitator = SimpleNamespace(name="coinbase_facilitator")  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "x402.servers", fake_servers)
    monkeypatch.setitem(sys.modules, "x402.facilitator", fake_facilitator)
    monkeypatch.setitem(sys.modules, "x402.schemes.exact.evm", fake_evm_exact)
    monkeypatch.setitem(sys.modules, "x402.schemes.upto.evm", fake_evm_upto)
    monkeypatch.setitem(sys.modules, "x402.schemes.exact.svm", fake_svm)
    monkeypatch.setitem(sys.modules, "x402.extensions.bazaar", fake_bazaar)
    monkeypatch.setitem(sys.modules, "coinbase_x402", fake_coinbase)

    return {
        "servers": fake_servers,
        "facilitator": fake_facilitator,
        "evm_exact": fake_evm_exact,
        "evm_upto": fake_evm_upto,
        "svm": fake_svm,
        "bazaar": fake_bazaar,
        "coinbase": fake_coinbase,
    }


def _install_pympp_modules(monkeypatch: pytest.MonkeyPatch) -> dict[str, ModuleType]:
    """Install fake `pympp.server` + `pympp.methods.tempo` + `pympp.methods.stripe`."""
    fake_pympp_server = ModuleType("pympp.server")

    class _FakeMppx:
        @staticmethod
        def create(methods: list[Any], secret_key: str) -> _FakeServer:
            srv = _FakeServer()
            srv.methods = list(methods)  # type: ignore[attr-defined]
            srv.secret_key = secret_key  # type: ignore[attr-defined]
            return srv

    fake_pympp_server.Mppx = _FakeMppx  # type: ignore[attr-defined]

    fake_tempo = ModuleType("pympp.methods.tempo")

    def _tempo_charge(**kwargs: Any) -> dict[str, Any]:
        return {"kind": "tempo.charge", **kwargs}

    def _tempo_session(**kwargs: Any) -> dict[str, Any]:
        return {"kind": "tempo.session", **kwargs}

    fake_tempo.charge = _tempo_charge  # type: ignore[attr-defined]
    fake_tempo.session = _tempo_session  # type: ignore[attr-defined]

    fake_stripe = ModuleType("pympp.methods.stripe")

    def _stripe_charge(**kwargs: Any) -> dict[str, Any]:
        return {"kind": "stripe.charge", **kwargs}

    fake_stripe.charge = _stripe_charge  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pympp.server", fake_pympp_server)
    monkeypatch.setitem(sys.modules, "pympp.methods.tempo", fake_tempo)
    monkeypatch.setitem(sys.modules, "pympp.methods.stripe", fake_stripe)

    return {
        "server": fake_pympp_server,
        "tempo": fake_tempo,
        "stripe": fake_stripe,
    }


# ---------------------------------------------------------------------------
# create_x402_server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_x402_server_no_peer_dep_raises_with_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "x402.servers", None)
    monkeypatch.setitem(sys.modules, "x402.facilitator", None)
    with pytest.raises(ImportError, match=r"pip install 'x402\[evm,svm,fastapi\]"):
        await create_x402_server()


@pytest.mark.asyncio
async def test_create_x402_server_default_facilitator_is_http(monkeypatch):
    _install_x402_modules(monkeypatch)
    server = await create_x402_server()
    assert isinstance(server, _FakeServer)
    assert server.initialized is True
    # No upstream → bare HTTPFacilitatorClient
    assert server.facilitator.upstream is None


@pytest.mark.asyncio
async def test_create_x402_server_coinbase_facilitator_uses_preset_when_available(monkeypatch):
    fakes = _install_x402_modules(monkeypatch)
    fakes["facilitator"].coinbase_facilitator = SimpleNamespace(name="coinbase_preset")
    server = await create_x402_server(facilitator="coinbase")
    assert server.facilitator.upstream is fakes["facilitator"].coinbase_facilitator


@pytest.mark.asyncio
async def test_create_x402_server_coinbase_facilitator_falls_back_to_public_url(monkeypatch):
    _install_x402_modules(monkeypatch)
    server = await create_x402_server(facilitator="coinbase")
    # Falls back to the published Coinbase facilitator URL when no preset constant is exported.
    assert isinstance(server.facilitator.upstream, str)
    assert "coinbase" in server.facilitator.upstream


@pytest.mark.asyncio
async def test_create_x402_server_registers_base_mainnet_exact_scheme_v1_and_v2(monkeypatch):
    fakes = _install_x402_modules(monkeypatch)
    server = await create_x402_server(rails=["x402-base-mainnet"])
    # eip155:8453 = Base mainnet
    assert any(net == "eip155:8453" for net, _ in server.registered)
    assert any(net == "eip155:8453" for net, _ in server.registered_v1)
    fakes["evm_exact"].ExactEvmServerScheme.assert_called()


@pytest.mark.asyncio
async def test_create_x402_server_registers_base_sepolia_upto_scheme(monkeypatch):
    fakes = _install_x402_modules(monkeypatch)
    server = await create_x402_server(rails=["x402-base-sepolia-upto"])
    assert any(net == "eip155:84532" for net, _ in server.registered)
    fakes["evm_upto"].UptoEvmServerScheme.assert_called()


@pytest.mark.asyncio
async def test_create_x402_server_registers_solana_mainnet(monkeypatch):
    fakes = _install_x402_modules(monkeypatch)
    server = await create_x402_server(rails=["x402-solana-mainnet"])
    assert any(net.startswith("solana:") for net, _ in server.registered)
    fakes["svm"].ExactSvmServerScheme.assert_called()


@pytest.mark.asyncio
async def test_create_x402_server_solana_upto_rejected_eagerly(monkeypatch):
    _install_x402_modules(monkeypatch)
    with pytest.raises(ValueError, match="upto"):
        # Type-ignore on the literal — we're testing runtime guard, not the type system.
        await create_x402_server(rails=["x402-solana-mainnet-upto"])  # type: ignore[list-item]


@pytest.mark.asyncio
async def test_create_x402_server_evm_peer_dep_missing_raises(monkeypatch):
    _install_x402_modules(monkeypatch)
    monkeypatch.setitem(sys.modules, "x402.schemes.exact.evm", None)
    with pytest.raises(ImportError, match=r"pip install 'x402\[evm\]"):
        await create_x402_server(rails=["x402-base-mainnet"])


@pytest.mark.asyncio
async def test_create_x402_server_svm_peer_dep_missing_raises(monkeypatch):
    _install_x402_modules(monkeypatch)
    monkeypatch.setitem(sys.modules, "x402.schemes.exact.svm", None)
    with pytest.raises(ImportError, match=r"pip install 'x402\[svm\]"):
        await create_x402_server(rails=["x402-solana-devnet"])


@pytest.mark.asyncio
async def test_create_x402_server_custom_scheme_registered(monkeypatch):
    _install_x402_modules(monkeypatch)
    sentinel = object()
    server = await create_x402_server(
        schemes=[CustomScheme(network="custom:1", scheme=sentinel)],
    )
    assert ("custom:1", sentinel) in server.registered


@pytest.mark.asyncio
async def test_create_x402_server_bazaar_extension_registered(monkeypatch):
    fakes = _install_x402_modules(monkeypatch)
    server = await create_x402_server(bazaar=True)
    assert fakes["bazaar"].bazaar_resource_server_extension in server.extensions


@pytest.mark.asyncio
async def test_create_x402_server_initialize_false_skips_init(monkeypatch):
    _install_x402_modules(monkeypatch)
    server = await create_x402_server(initialize=False)
    assert server.initialized is False


# ---------------------------------------------------------------------------
# create_mppx_server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_mppx_server_no_peer_dep_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "pympp.server", None)
    with pytest.raises(ImportError, match=r"pip install 'pympp\[server,tempo,stripe\]"):
        await create_mppx_server(secret_key="msk_test")


@pytest.mark.asyncio
async def test_create_mppx_server_tempo_charge_method_added(monkeypatch):
    _install_pympp_modules(monkeypatch)
    server = await create_mppx_server(
        secret_key="msk_test",
        rails=MppxRails(tempo=TempoChargeRail(recipient="0xrecipient")),
    )
    assert any(m["kind"] == "tempo.charge" for m in server.methods)
    assert server.secret_key == "msk_test"


@pytest.mark.asyncio
async def test_create_mppx_server_tempo_charge_uses_default_usdc_currency(monkeypatch):
    _install_pympp_modules(monkeypatch)
    server = await create_mppx_server(
        secret_key="msk_test",
        rails=MppxRails(tempo=TempoChargeRail(recipient="0xrecipient", testnet=False)),
    )
    tempo_method = next(m for m in server.methods if m["kind"] == "tempo.charge")
    # USDC mainnet on Tempo
    assert tempo_method["currency"] != ""
    assert tempo_method["recipient"] == "0xrecipient"


@pytest.mark.asyncio
async def test_create_mppx_server_tempo_session_method_added(monkeypatch):
    _install_pympp_modules(monkeypatch)
    fake_store = object()
    server = await create_mppx_server(
        secret_key="msk_test",
        rails=MppxRails(
            tempo_session=TempoSessionRail(
                recipient="0xrecipient",
                escrow_contract="0xescrow",
                store=fake_store,
            ),
        ),
    )
    sess = next(m for m in server.methods if m["kind"] == "tempo.session")
    assert sess["recipient"] == "0xrecipient"
    assert sess["escrow_contract"] == "0xescrow"
    assert sess["store"] is fake_store


@pytest.mark.asyncio
async def test_create_mppx_server_stripe_method_added(monkeypatch):
    _install_pympp_modules(monkeypatch)
    server = await create_mppx_server(
        secret_key="msk_test",
        rails=MppxRails(
            stripe=StripeRail(profile_id="profile_x", secret_key="sk_test"),
        ),
    )
    stripe_method = next(m for m in server.methods if m["kind"] == "stripe.charge")
    assert stripe_method["network_id"] == "profile_x"
    assert stripe_method["secret_key"] == "sk_test"
    assert stripe_method["payment_method_types"] == ["card", "link"]


@pytest.mark.asyncio
async def test_create_mppx_server_methods_passthrough(monkeypatch):
    _install_pympp_modules(monkeypatch)
    sentinel = {"kind": "custom"}
    server = await create_mppx_server(secret_key="msk_test", methods=[sentinel])
    assert sentinel in server.methods


@pytest.mark.asyncio
async def test_create_mppx_server_tempo_peer_missing_raises(monkeypatch):
    _install_pympp_modules(monkeypatch)
    monkeypatch.setitem(sys.modules, "pympp.methods.tempo", None)
    with pytest.raises(ImportError, match=r"pip install 'pympp\[tempo\]"):
        await create_mppx_server(
            secret_key="msk_test",
            rails=MppxRails(tempo=TempoChargeRail(recipient="0xr")),
        )


@pytest.mark.asyncio
async def test_create_mppx_server_stripe_peer_missing_raises(monkeypatch):
    _install_pympp_modules(monkeypatch)
    monkeypatch.setitem(sys.modules, "pympp.methods.stripe", None)
    with pytest.raises(ImportError, match=r"pip install 'pympp\[stripe\]"):
        await create_mppx_server(
            secret_key="msk_test",
            rails=MppxRails(stripe=StripeRail(profile_id="p", secret_key="sk")),
        )
