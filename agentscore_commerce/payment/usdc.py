"""USDC token registry per network. Used by payment_directive and rail definitions."""

from typing import Final


class _BaseMainnet:
    address: Final[str] = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    decimals: Final[int] = 6


class _BaseSepolia:
    address: Final[str] = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    decimals: Final[int] = 6


class _SolanaMainnet:
    mint: Final[str] = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    decimals: Final[int] = 6


class _SolanaDevnet:
    mint: Final[str] = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
    decimals: Final[int] = 6


class _TempoMainnet:
    address: Final[str] = "0x20C000000000000000000000b9537d11c60E8b50"
    decimals: Final[int] = 6


class _TempoTestnet:
    address: Final[str] = "0x20c0000000000000000000000000000000000000"
    decimals: Final[int] = 6


class _USDCBase:
    mainnet = _BaseMainnet()
    sepolia = _BaseSepolia()


class _USDCSolana:
    mainnet = _SolanaMainnet()
    devnet = _SolanaDevnet()


class _USDCTempo:
    mainnet = _TempoMainnet()
    testnet = _TempoTestnet()


class _USDC:
    base = _USDCBase()
    solana = _USDCSolana()
    tempo = _USDCTempo()


USDC = _USDC()
