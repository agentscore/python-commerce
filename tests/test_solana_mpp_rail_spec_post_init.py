"""Behavior contract for `SolanaMppRailSpec.__post_init__` mint derivation.

When the merchant flips ``network`` to devnet (CAIP-2 or the raw ``'devnet'``
form ``@solana/mpp`` accepts) without explicitly pinning ``token``, the
dataclass picks the devnet USDC mint, mirroring ``X402BaseRailSpec``'s pattern
for Sepolia. Explicit ``token`` overrides always win.
"""

from agentscore_commerce.payment.rail_spec import SolanaMppRailSpec
from agentscore_commerce.payment.usdc import USDC


def test_default_mainnet_keeps_mainnet_mint() -> None:
    spec = SolanaMppRailSpec(recipient="13QbUqJeu3VMLxn4Jypt63zqCrzKeZoaYA5k1GaWQpmS")
    assert spec.token == USDC.solana.mainnet.mint


def test_devnet_caip2_flips_to_devnet_mint() -> None:
    spec = SolanaMppRailSpec(
        recipient="13QbUqJeu3VMLxn4Jypt63zqCrzKeZoaYA5k1GaWQpmS",
        network="solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    )
    assert spec.token == USDC.solana.devnet.mint


def test_raw_devnet_string_flips_to_devnet_mint() -> None:
    spec = SolanaMppRailSpec(
        recipient="13QbUqJeu3VMLxn4Jypt63zqCrzKeZoaYA5k1GaWQpmS",
        network="devnet",
    )
    assert spec.token == USDC.solana.devnet.mint


def test_explicit_token_override_wins_over_network_derived_default() -> None:
    spec = SolanaMppRailSpec(
        recipient="13QbUqJeu3VMLxn4Jypt63zqCrzKeZoaYA5k1GaWQpmS",
        network="devnet",
        token="custom_mint_pubkey",
    )
    assert spec.token == "custom_mint_pubkey"
