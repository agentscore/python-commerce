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
