"""Live-API tests for create_x402_server + create_mppx_server factories.

The prior mock-based suite tracked x402 2.8 / pympp pre-release internal layout
(``x402.servers``, ``HTTPFacilitatorClient``, ``Mppx``, ``charge(currency=...)``).
x402 2.9 + pympp 0.6 shipped breaking refactors so the mocks no longer reflect
reality. We now run against the actually-installed peer deps — the tests skip
when the deps aren't present so the suite still runs in minimal envs.
"""

from __future__ import annotations

import importlib.util

import pytest

from agentscore_commerce.payment import (
    MppxRails,
    TempoChargeRail,
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


@pytest.mark.skipif(not _MPPX_INSTALLED or not _TEMPO_INSTALLED, reason="pympp[tempo] not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_tempo_returns_mpp_instance() -> None:
    """create_mppx_server with a Tempo charge rail returns a configured Mpp."""
    server = await create_mppx_server(
        secret_key="X" * 32,
        rails=MppxRails(tempo=TempoChargeRail(recipient="0x" + "00" * 20, testnet=True)),
    )
    assert type(server).__name__ == "Mpp"
    # pympp 0.6 exposes intent-named methods directly (charge, pay, …) on the Mpp instance.
    assert callable(getattr(server, "charge", None))


@pytest.mark.skipif(not _MPPX_INSTALLED, reason="pympp not installed")
@pytest.mark.asyncio
async def test_create_mppx_server_no_method_or_rails_raises() -> None:
    with pytest.raises(ValueError, match="no method or rails"):
        await create_mppx_server(secret_key="X" * 32)
