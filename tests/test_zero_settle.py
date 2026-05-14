"""Tests for ``agentscore_commerce.payment.zero_settle.zero_amount_carve_out``.

Locked cross-language fixtures shared with the Node sibling at
``node-commerce/tests/payment/zero_settle.test.ts``. Both files reference
identical payload dicts / Authorization header values + expected
``ZeroSettleResult``. Drift in either language (DID parsing, dict-shape
handling, address validation) fails that language's test against the
locked value.
"""

from __future__ import annotations

import pytest

from agentscore_commerce.payment import ZeroSettleResult, zero_amount_carve_out

# ─── x402-base rail: payload is the verified outer dict (already base64-decoded) ────────

_X402_EVM_PAYLOAD = {
    "payload": {"authorization": {"from": "0xABCDef1234567890123456789012345678901234"}},
}

_X402_FIXTURES = [
    (
        "x402_evm_signer_recovered",
        _X402_EVM_PAYLOAD,
        ZeroSettleResult(
            signer_address="0xabcdef1234567890123456789012345678901234",
            signer_network="evm",
        ),
    ),
    ("x402_payload_none", None, ZeroSettleResult(signer_address=None, signer_network=None)),
    (
        "x402_payload_not_dict",
        "not-a-dict",  # type: ignore[arg-type]
        ZeroSettleResult(signer_address=None, signer_network=None),
    ),
    (
        "x402_inner_payload_missing",
        {},
        ZeroSettleResult(signer_address=None, signer_network=None),
    ),
    (
        "x402_inner_payload_not_dict",
        {"payload": "oops"},
        ZeroSettleResult(signer_address=None, signer_network=None),
    ),
    (
        "x402_authorization_missing",
        {"payload": {}},
        ZeroSettleResult(signer_address=None, signer_network=None),
    ),
    (
        "x402_from_missing",
        {"payload": {"authorization": {}}},
        ZeroSettleResult(signer_address=None, signer_network=None),
    ),
    (
        "x402_from_not_evm_shape",
        {"payload": {"authorization": {"from": "not-an-address"}}},
        ZeroSettleResult(signer_address=None, signer_network=None),
    ),
]

# ─── tempo / solana MPP rails: authorization_header carries the credential ──────────────

_MPP_TEMPO_AUTH = (
    "Payment eyJzb3VyY2UiOiAiZGlkOnBraDplaXAxNTU6NDIxNzoweEFCQ0RlZjEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQifQ=="
)
_MPP_SOLANA_AUTH = (
    "Payment "
    "eyJjaGFsbGVuZ2UiOiB7InNvdXJjZSI6ICJkaWQ6cGtoOnNvbGFuYTo1ZXlrdDRVc0Z2OFA4TkpkVFJFcFkxdnpxS3FaS3ZkcFVrZkZw"
    "OjduUUVneHFFVzFiRHFhVDNrWldhOEtxVWs0V2ZoNFZiY3cifX0="
)

_MPP_FIXTURES = [
    (
        "mpp_tempo_signer_recovered",
        "tempo",
        _MPP_TEMPO_AUTH,
        ZeroSettleResult(
            signer_address="0xabcdef1234567890123456789012345678901234",
            signer_network="evm",
        ),
    ),
    (
        "mpp_solana_signer_recovered",
        "solana",
        _MPP_SOLANA_AUTH,
        ZeroSettleResult(
            signer_address="7nQEgxqEW1bDqaT3kZWa8KqUk4Wfh4Vbcw",
            signer_network="solana",
        ),
    ),
    ("mpp_auth_none", "tempo", None, ZeroSettleResult(signer_address=None, signer_network=None)),
    ("mpp_auth_empty", "tempo", "", ZeroSettleResult(signer_address=None, signer_network=None)),
    (
        "mpp_auth_not_payment_scheme",
        "tempo",
        "Bearer abc.def.ghi",
        ZeroSettleResult(signer_address=None, signer_network=None),
    ),
    (
        "mpp_credential_without_source",
        "tempo",
        "Payment eyJmb28iOiAiYmFyIn0=",  # {"foo": "bar"} — no source field
        ZeroSettleResult(signer_address=None, signer_network=None),
    ),
]


@pytest.mark.parametrize(
    ("label", "payload", "expected"),
    _X402_FIXTURES,
    ids=[label for label, _, _ in _X402_FIXTURES],
)
def test_x402_base_locked_fixture(label, payload, expected) -> None:
    del label
    assert zero_amount_carve_out(rail="x402-base", payload=payload) == expected


@pytest.mark.parametrize(
    ("label", "rail", "auth_header", "expected"),
    _MPP_FIXTURES,
    ids=[label for label, _, _, _ in _MPP_FIXTURES],
)
def test_mpp_locked_fixture(label, rail, auth_header, expected) -> None:
    del label
    assert zero_amount_carve_out(rail=rail, authorization_header=auth_header) == expected


def test_tx_hash_is_always_none() -> None:
    """The carve-out skips on-chain settle; tx_hash is fixed to None."""
    result = zero_amount_carve_out(rail="x402-base", payload=_X402_EVM_PAYLOAD)
    assert result.tx_hash is None


def test_x402_base_ignores_authorization_header() -> None:
    """``rail="x402-base"`` only reads ``payload``; an extra ``authorization_header`` arg is ignored."""
    result = zero_amount_carve_out(
        rail="x402-base",
        payload=_X402_EVM_PAYLOAD,
        authorization_header=_MPP_SOLANA_AUTH,
    )
    # Returns the x402 EVM signer, NOT the Solana signer from the MPP header
    assert result.signer_address == "0xabcdef1234567890123456789012345678901234"
    assert result.signer_network == "evm"


def test_mpp_rails_ignore_payload() -> None:
    """``rail="tempo"`` / ``"solana"`` only read ``authorization_header``; ``payload`` is ignored."""
    result = zero_amount_carve_out(
        rail="tempo",
        payload=_X402_EVM_PAYLOAD,
        authorization_header=_MPP_TEMPO_AUTH,
    )
    # Returns the tempo MPP signer from the authorization header, not the x402 payload's from
    assert result.signer_address == "0xabcdef1234567890123456789012345678901234"
    assert result.signer_network == "evm"


def test_no_credential_provided_returns_none() -> None:
    """Missing both ``payload`` and ``authorization_header`` returns a null result."""
    assert zero_amount_carve_out(rail="x402-base") == ZeroSettleResult(
        signer_address=None,
        signer_network=None,
    )
    assert zero_amount_carve_out(rail="tempo") == ZeroSettleResult(
        signer_address=None,
        signer_network=None,
    )
    assert zero_amount_carve_out(rail="solana") == ZeroSettleResult(
        signer_address=None,
        signer_network=None,
    )
