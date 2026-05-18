"""JWS round-trip for A2A Agent Card signatures (RFC 7515).

Per A2A spec §4.4.7, the card body is signed without ``signatures``, the
signature is computed over the canonical serialization, then attached back as
one of ``card["signatures"][]``. Verifiers reconstruct the body without
``signatures`` and verify each entry against the merchant's published JWKS.

This test proves we can sign and verify an unsigned card produced by
``build_a2a_agent_card`` end-to-end.
"""

from __future__ import annotations

import json
import warnings

import pytest

from agentscore_commerce.identity.a2a import (
    A2AAgentCardSignature,
    A2AAgentSkill,
    build_a2a_agent_card,
    ucp_a2a_extension,
)

# joserfc emits a SecurityWarning for EdDSA per RFC 9864; suppress at sign/verify
# time. Mirrors agentscore_commerce.identity.ucp_jwks pattern.
warnings.filterwarnings("ignore", message="EdDSA is deprecated")


def _sign_card(card: dict, private_jwk: dict, kid: str) -> A2AAgentCardSignature:
    """Sign the card body MINUS `signatures` and return one AgentCardSignature."""
    from joserfc import jws
    from joserfc.jwk import OKPKey
    from joserfc.jws import JWSRegistry

    body_without_sigs = {k: v for k, v in card.items() if k != "signatures"}
    payload = json.dumps(body_without_sigs).encode("utf-8")
    key = OKPKey.import_key(private_jwk)
    header = {"alg": "EdDSA", "kid": kid}
    registry = JWSRegistry(algorithms=["EdDSA"])
    compact = jws.serialize_compact(header, payload, key, registry=registry)
    protected_b64, _payload_b64, signature_b64 = compact.split(".")
    return A2AAgentCardSignature(protected=protected_b64, signature=signature_b64)


def _verify_card(card: dict, public_jwk: dict) -> bool:
    import base64

    from joserfc import jws
    from joserfc.jwk import OKPKey
    from joserfc.jws import JWSRegistry

    sigs = card.get("signatures") or []
    if not sigs:
        msg = "card has no signatures"
        raise ValueError(msg)
    sig = sigs[0]
    body_without_sigs = {k: v for k, v in card.items() if k != "signatures"}
    payload = json.dumps(body_without_sigs).encode("utf-8")

    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    compact_jws = f"{sig['protected']}.{payload_b64}.{sig['signature']}"
    key = OKPKey.import_key(public_jwk)
    registry = JWSRegistry(algorithms=["EdDSA"])
    jws.deserialize_compact(compact_jws, key, registry=registry)
    return True


@pytest.fixture
def jws_keypair() -> tuple[dict, dict]:
    from joserfc.jwk import OKPKey

    key = OKPKey.generate_key("Ed25519", private=True)
    private = key.as_dict(private=True)
    public = key.as_dict(private=False)
    return private, public


def test_signs_and_verifies_an_unsigned_card(jws_keypair: tuple[dict, dict]) -> None:
    private_jwk, public_jwk = jws_keypair
    card_obj = build_a2a_agent_card(
        name="Example Merchant",
        description="Buy products via agent payments.",
        url="https://agents.example.com",
        version="1.0.0",
        skills=[
            A2AAgentSkill(
                id="purchase",
                name="Purchase",
                description="Buy products via agent payments.",
                tags=["commerce", "payment"],
            ),
        ],
        extensions=[ucp_a2a_extension()],
    )
    unsigned = card_obj.to_dict()
    signature = _sign_card(unsigned, private_jwk, "merchant-key-1")
    signed = {**unsigned, "signatures": [signature.to_dict()]}
    assert _verify_card(signed, public_jwk) is True


def test_verification_fails_when_body_is_tampered(jws_keypair: tuple[dict, dict]) -> None:
    private_jwk, public_jwk = jws_keypair
    card_obj = build_a2a_agent_card(
        name="Example",
        description="d",
        url="https://x.example",
        skills=[A2AAgentSkill(id="p", name="P", description="d", tags=["t"])],
    )
    unsigned = card_obj.to_dict()
    signature = _sign_card(unsigned, private_jwk, "k1")
    tampered = {**unsigned, "description": "tampered", "signatures": [signature.to_dict()]}

    from joserfc.errors import BadSignatureError

    with pytest.raises(BadSignatureError):
        _verify_card(tampered, public_jwk)
