"""UCP profile signing helpers (JWKS + JWS) — Python sibling of node-commerce.

UCP §6 (https://ucp.dev/latest/specification/signatures/) requires that profiles
published at ``/.well-known/ucp`` carry a JWKS-backed signature for trust-mode clients
(Google AI Mode, Gemini commerce, future ChatGPT app shells). Without a signature,
trust-mode clients reject the profile.

This module provides:

* :func:`generate_ucp_signing_key` — generate an Ed25519 (or ES256) keypair
* :func:`sign_ucp_profile` — sign a profile, returning a JWS-attached envelope
* :func:`verify_ucp_profile` — verify a signed profile against a JWKS
* :func:`build_jwks_response` — assemble a JWKS document for ``/.well-known/jwks.json``

Implementation rides on ``joserfc`` (optional extra). Install via
``pip install agentscore-commerce[ucp]``. Merchants who don't sign their profile
(development) skip this module entirely; the unsigned :func:`build_ucp_profile`
path still works.

Cross-language API parity with ``@agent-score/commerce`` Node SDK — same canonical
body, same JWS Compact Serialization, same key-resolution semantics. Profiles
signed by Node verify in Python and vice versa.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

_JOSE_INSTALL_HINT = (
    "Install the optional dependency: `pip install agentscore-commerce[ucp]` (or `uv pip install joserfc`)."
)


def _load_joserfc() -> Any:
    """Lazy-import joserfc so the optional dep isn't required for non-signing flows."""
    try:
        import joserfc  # type: ignore[import-not-found]

        return joserfc
    except ImportError as exc:
        msg = f"UCP signing requires the `joserfc` library, an optional dependency. {_JOSE_INSTALL_HINT}"
        raise ImportError(msg) from exc


@dataclass
class GeneratedUCPKey:
    """Output of :func:`generate_ucp_signing_key`.

    * ``private_key`` is the joserfc Key object — pass to :func:`sign_ucp_profile`.
      Never publish.
    * ``public_jwk`` is the JWK dict — publish at ``/.well-known/jwks.json`` and
      inline in the UCP profile's ``signing_keys[]``.
    """

    private_key: Any
    public_jwk: dict[str, Any]


def generate_ucp_signing_key(*, kid: str, alg: Literal["EdDSA", "ES256"] = "EdDSA") -> GeneratedUCPKey:
    """Generate an Ed25519 (default) or ES256 keypair for signing UCP profiles.

    The ``private_key`` is a joserfc ``Key`` — store it securely (env var, KMS, secret
    manager) and pass to :func:`sign_ucp_profile`.

    The ``public_jwk`` is a dict you publish at ``/.well-known/jwks.json`` and inline
    in the UCP profile's ``signing_keys[]`` array.

    Example::

        from agentscore_commerce.identity.ucp_jwks import generate_ucp_signing_key

        key = generate_ucp_signing_key(kid='merchant-2026-05')
        # key.private_key — persist securely
        # key.public_jwk  — publish at /.well-known/jwks.json
    """
    joserfc = _load_joserfc()

    if alg == "EdDSA":
        from joserfc.jwk import OKPKey  # type: ignore[import-not-found]

        priv = OKPKey.generate_key(crv="Ed25519", parameters={"kid": kid, "alg": alg, "use": "sig"})
    elif alg == "ES256":
        from joserfc.jwk import ECKey  # type: ignore[import-not-found]

        priv = ECKey.generate_key(crv="P-256", parameters={"kid": kid, "alg": alg, "use": "sig"})
    else:
        msg = f"Unsupported UCP signing algorithm: {alg!r}. Use 'EdDSA' or 'ES256'."
        raise ValueError(msg)

    public_jwk = priv.as_dict(private=False)
    # Ensure kid/alg/use are present in the exported dict (joserfc preserves params).
    public_jwk.setdefault("kid", kid)
    public_jwk.setdefault("alg", alg)
    public_jwk.setdefault("use", "sig")

    # Quiet unused-import warning when only one branch executes.
    _ = joserfc

    return GeneratedUCPKey(private_key=priv, public_jwk=public_jwk)


def _canonicalize_profile(profile: dict[str, Any]) -> bytes:
    """Canonicalize a UCP profile body for signing.

    Removes the ``signature`` field (if present), sorts keys lexicographically at every
    nesting level, returns UTF-8 JSON bytes. Cross-language byte-identical with the
    Node ``stableStringify`` output.

    UCP §6.2: "the JSON-serialized profile body, with ``signature`` removed and keys
    ordered lexicographically at every nesting level."
    """
    stripped = {k: v for k, v in profile.items() if k != "signature"}
    # ``ensure_ascii=False`` so non-ASCII characters travel as UTF-8 (matches Node's
    # JSON.stringify default). ``sort_keys=True`` sorts keys at every level. Compact
    # separators avoid whitespace drift.
    return json.dumps(stripped, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign_ucp_profile(
    profile: dict[str, Any],
    *,
    signing_key: Any,
    kid: str,
    alg: Literal["EdDSA", "ES256"] = "EdDSA",
) -> dict[str, Any]:
    """Sign a UCP profile, returning a new dict with the JWS attached as ``signature``.

    The signature covers the canonicalized profile body (everything except
    ``signature`` itself, with keys sorted at every level). Trust-mode UCP verifiers
    reconstruct the canonical body, look up the key referenced by the JWS header's
    ``kid``, and validate.

    The profile's ``signing_keys[]`` MUST already include a JWK with the matching
    ``kid`` — otherwise verifiers can't find the public key.

    Example::

        profile = build_ucp_profile(..., signing_keys=[UCPSigningKey(**key.public_jwk)])
        signed = sign_ucp_profile(profile.to_dict(), signing_key=key.private_key, kid='merchant-2026-05')
    """
    joserfc = _load_joserfc()
    from joserfc import jws  # type: ignore[import-not-found]
    from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

    canonical_body = _canonicalize_profile(profile)
    header = {"alg": alg, "kid": kid, "typ": "ucp-profile+jws"}
    # joserfc treats EdDSA as "not recommended" by default; UCP §6 explicitly accepts
    # both EdDSA and ES256, so allow both.
    registry = JWSRegistry(algorithms=["EdDSA", "ES256"])
    signature = jws.serialize_compact(header, canonical_body, signing_key, registry=registry)

    _ = joserfc

    return {**profile, "signature": signature}


def verify_ucp_profile(
    signed_profile: dict[str, Any],
    jwks: dict[str, Any],
) -> bool:
    """Verify a signed UCP profile against a JWKS.

    Returns ``True`` when the JWS validates against a matching key in ``jwks`` AND the
    signed payload matches the canonical body of the profile-as-presented. Raises on
    signature mismatch, missing key, or canonicalization drift.

    Example::

        ok = verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
    """
    joserfc = _load_joserfc()
    from joserfc import jws  # type: ignore[import-not-found]
    from joserfc.jwk import KeySet  # type: ignore[import-not-found]
    from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

    sig = signed_profile.get("signature")
    if not sig:
        msg = "UCP profile has no `signature` field; expected JWS Compact Serialization."
        raise ValueError(msg)

    stripped = {k: v for k, v in signed_profile.items() if k != "signature"}
    expected_payload = _canonicalize_profile(stripped)

    # joserfc's KeySetSerialization type is a precise TypedDict; in practice the helper
    # accepts a plain dict-of-keys at runtime, so cast at the boundary.
    key_set = KeySet.import_key_set(cast("Any", jwks))
    registry = JWSRegistry(algorithms=["EdDSA", "ES256"])
    obj = jws.deserialize_compact(sig, key_set, registry=registry)

    # Compare the bytes that were actually signed against the canonical body of the
    # profile we received. ``deserialize_compact`` validates the JWS against the bytes
    # embedded in the JWS payload segment — but the profile body could have been
    # swapped after signing while the JWS stayed unchanged.
    if obj.payload != expected_payload:
        msg = "UCP profile body does not match the signed payload (tampered or non-canonical)."
        raise ValueError(msg)

    _ = joserfc
    return True


def build_jwks_response(keys: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a JWKS document for ``/.well-known/jwks.json``.

    Example::

        from agentscore_commerce.identity.ucp_jwks import build_jwks_response

        @app.get('/.well-known/jwks.json')
        async def jwks():
            return build_jwks_response([key.public_jwk])
    """
    return {"keys": keys}


__all__ = [
    "GeneratedUCPKey",
    "build_jwks_response",
    "generate_ucp_signing_key",
    "sign_ucp_profile",
    "verify_ucp_profile",
]
