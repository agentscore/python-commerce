"""Named network registry. Vendors reference symbolic names instead of magic strings."""

from typing import Final, Literal

NetworkFamily = Literal["base", "solana", "tempo"]


class _Base:
    class _Mainnet:
        caip2: Final[str] = "eip155:8453"
        chain_id: Final[int] = 8453

    class _Sepolia:
        caip2: Final[str] = "eip155:84532"
        chain_id: Final[int] = 84532

    mainnet = _Mainnet()
    sepolia = _Sepolia()


class _Solana:
    class _Mainnet:
        caip2: Final[str] = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

    class _Devnet:
        caip2: Final[str] = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"

    mainnet = _Mainnet()
    devnet = _Devnet()


class _Tempo:
    class _Mainnet:
        caip2: Final[str] = "eip155:4217"
        chain_id: Final[int] = 4217

    class _Testnet:
        caip2: Final[str] = "eip155:42431"
        chain_id: Final[int] = 42431

    mainnet = _Mainnet()
    testnet = _Testnet()


class _Networks:
    base = _Base()
    solana = _Solana()
    tempo = _Tempo()


networks = _Networks()


def network_family(caip2: str) -> NetworkFamily | None:
    """Return the family name (base/solana/tempo) for a CAIP-2 string, or None."""
    if caip2 in (networks.base.mainnet.caip2, networks.base.sepolia.caip2):
        return "base"
    if caip2 in (networks.solana.mainnet.caip2, networks.solana.devnet.caip2):
        return "solana"
    if caip2 in (networks.tempo.mainnet.caip2, networks.tempo.testnet.caip2):
        return "tempo"
    if caip2.startswith("solana:"):
        return "solana"
    return None
