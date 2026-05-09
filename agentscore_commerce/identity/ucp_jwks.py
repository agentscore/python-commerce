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

import contextlib
import hmac
import json
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

_JOSE_INSTALL_HINT = (
    "Install the optional dependency: `pip install agentscore-commerce[ucp]` (or `uv pip install joserfc`)."
)

_ALLOWED_ALGS = ("EdDSA", "ES256")
_UCP_TYP = "ucp-profile+jws"

_MAX_SAFE_INT = 2**53 - 1


@contextlib.contextmanager
def _suppress_joserfc_eddsa_warning() -> Iterator[None]:
    """Suppress joserfc's RFC-9864-deprecation SecurityWarning around JWS sign/verify.

    joserfc emits this on every JWS operation that uses EdDSA, despite EdDSA
    being the actively-recommended-by-IETF algorithm for new deployments. The
    filter is pinned to the exact message + class
    (``joserfc.errors.SecurityWarning``: ``"EdDSA is deprecated via RFC 9864"``)
    so any other SecurityWarning still surfaces normally. Key generation does
    not emit this warning, so suppression has no effect there.
    """
    from joserfc.errors import SecurityWarning  # type: ignore[import-not-found]

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"^EdDSA is deprecated via RFC 9864$",
            category=SecurityWarning,
        )
        yield


class UCPVerificationError(ValueError):
    """Discriminated error for UCP signature verification failures.

    Subclasses ``ValueError`` so existing ``except ValueError`` blocks keep working.
    Inspect ``code`` to branch on failure mode without parsing the message string
    or importing joserfc internals.
    """

    def __init__(
        self,
        code: Literal[
            "no_signature",
            "missing_kid",
            "kid_not_found",
            "duplicate_kid",
            "unsupported_alg",
            "wrong_typ",
            "signature_invalid",
            "body_mismatch",
            "malformed_jws",
            "malformed_jwks",
            "unrecognized_critical_header",
            "unusable_key",
        ],
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


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
    _load_joserfc()

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

    return GeneratedUCPKey(private_key=priv, public_jwk=public_jwk)


def _reject_unsafe_numbers(value: Any) -> None:
    """Walk ``value`` and raise on any number that won't survive cross-language parity.

    Two failure modes are rejected:

    * Non-integer ``float`` values. Cross-language float canonicalization (RFC 8785
      §3.2.2.3) diverges between Python's ``json.dumps`` and Node's ``JSON.stringify``
      (e.g. ``1.0`` vs ``1``, ``1e-7`` vs ``1e-07``). Use decimal strings (``"9.99"``)
      for monetary or fractional fields.
    * ``int`` values whose magnitude exceeds ``Number.MAX_SAFE_INTEGER`` (2^53 - 1).
      Python ints are arbitrary-width, but JS verifiers parse the canonical body via
      ``JSON.parse`` which silently loses precision past 2^53. Use a decimal string
      for any integer that may exceed the safe range.

    Catching the drift at sign-time prevents silent verifier-side failures in
    production.
    """
    if isinstance(value, bool):
        return  # bool subclasses int; allow.
    if isinstance(value, float):
        msg = (
            f"UCP profile canonicalization rejects float value {value!r}. "
            "Use a decimal string (e.g. '9.99') for monetary or fractional fields "
            "to preserve cross-language byte-parity."
        )
        raise ValueError(msg)
    if isinstance(value, int) and abs(value) > _MAX_SAFE_INT:
        msg = (
            f"UCP profile canonicalization rejects integer {value} that exceeds "
            "Number.MAX_SAFE_INTEGER (2^53 - 1). JS verifiers cannot losslessly "
            "parse this; use a decimal string to preserve cross-language byte-parity."
        )
        raise ValueError(msg)
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_unsafe_numbers(k)
            _reject_unsafe_numbers(v)
    elif isinstance(value, list | tuple | set | frozenset):
        for v in value:
            _reject_unsafe_numbers(v)


def _canonicalize_profile(profile: dict[str, Any]) -> bytes:
    """Canonicalize a UCP profile body for signing.

    Removes the ``signature`` field (if present), sorts keys lexicographically at every
    nesting level, returns UTF-8 JSON bytes. Cross-language byte-identical with the
    Node ``stableStringify`` output.

    Throws ``ValueError`` on float input or oversized int (see
    :func:`_reject_unsafe_numbers`).

    UCP §6.2: "the JSON-serialized profile body, with ``signature`` removed and keys
    ordered lexicographically at every nesting level."
    """
    stripped = {k: v for k, v in profile.items() if k != "signature"}
    _reject_unsafe_numbers(stripped)
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
    _load_joserfc()
    from joserfc import jws  # type: ignore[import-not-found]
    from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

    if not isinstance(kid, str) or not kid:
        msg = "sign_ucp_profile: `kid` must be a non-empty string."
        raise ValueError(msg)

    # Sign-time kid sanity check: the profile's `signing_keys[]` MUST contain
    # a JWK with the matching kid; otherwise verifiers can't resolve the
    # public key and the profile is dead-on-arrival.
    declared_kids = [
        k.get("kid") if isinstance(k, dict) else getattr(k, "kid", None) for k in profile.get("signing_keys", [])
    ]
    if kid not in declared_kids:
        msg = (
            f"sign_ucp_profile: kid {kid!r} is not present in profile.signing_keys[] "
            f"(declared kids: {declared_kids!r}). Verifiers will not find the key."
        )
        raise ValueError(msg)

    canonical_body = _canonicalize_profile(profile)
    header = {"alg": alg, "kid": kid, "typ": _UCP_TYP}
    # joserfc treats EdDSA as "not recommended" by default; UCP §6 explicitly accepts
    # both EdDSA and ES256, so allow both.
    registry = JWSRegistry(algorithms=list(_ALLOWED_ALGS))
    with _suppress_joserfc_eddsa_warning():
        signature = jws.serialize_compact(header, canonical_body, signing_key, registry=registry)

    return {**profile, "signature": signature}


def _peek_jws_header(jws_compact: str) -> dict[str, Any]:
    """Decode the JWS protected header (first segment) without verifying.

    Used to enforce kid/typ/alg requirements before handing the JWS to joserfc's
    deserialize_compact (which would skip these checks for kid-less JWSs).
    """
    import base64

    try:
        header_b64 = jws_compact.split(".")[0]
        padding = "=" * (-len(header_b64) % 4)
        header_bytes = base64.urlsafe_b64decode(header_b64 + padding)
        decoded = json.loads(header_bytes)
    except (ValueError, IndexError, json.JSONDecodeError) as exc:
        raise UCPVerificationError("malformed_jws", f"Could not decode JWS protected header: {exc}") from exc
    if not isinstance(decoded, dict):
        raise UCPVerificationError(
            "malformed_jws",
            f"JWS protected header must decode to a JSON object; got {type(decoded).__name__}.",
        )
    return decoded


def verify_ucp_profile(
    signed_profile: dict[str, Any],
    jwks: dict[str, Any],
) -> bool:
    """Verify a signed UCP profile against a JWKS.

    Returns ``True`` when:
      * the JWS protected header carries ``kid`` + ``typ='ucp-profile+jws'`` + a
        registered ``alg`` (EdDSA or ES256),
      * the JWKS contains exactly one key with the matching ``kid``,
      * the JWS signature validates against that key,
      * the signed payload byte-equals the canonical body of the presented profile.

    Raises :class:`UCPVerificationError` (a ``ValueError`` subclass) with a
    discriminated ``code`` attribute on every failure mode.

    Example::

        ok = verify_ucp_profile(signed, build_jwks_response([key.public_jwk]))
    """
    _load_joserfc()
    from joserfc import jws  # type: ignore[import-not-found]
    from joserfc.jwk import KeySet  # type: ignore[import-not-found]
    from joserfc.jws import JWSRegistry  # type: ignore[import-not-found]

    if not isinstance(signed_profile, dict):
        raise UCPVerificationError(
            "no_signature",
            f"UCP verifier expected a profile dict; got {type(signed_profile).__name__}.",
        )

    # JWKS shape guard so a malformed argument emits a typed UCPVerificationError
    # rather than a confusing kid_not_found / AttributeError.
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise UCPVerificationError(
            "malformed_jwks",
            f"UCP verifier expected JWKS shape {{'keys': [...]}}; got {type(jwks).__name__}.",
        )

    sig = signed_profile.get("signature")
    if not sig:
        raise UCPVerificationError(
            "no_signature",
            "UCP profile has no `signature` field; expected JWS Compact Serialization.",
        )
    if not isinstance(sig, str):
        raise UCPVerificationError(
            "no_signature",
            f"UCP `signature` must be a string; got {type(sig).__name__}.",
        )

    # Pre-deserialize header checks — joserfc's deserialize_compact accepts kid-less
    # JWSs (it iterates the KeySet) so we enforce kid/typ/alg ourselves.
    header = _peek_jws_header(sig)
    if header.get("typ") != _UCP_TYP:
        raise UCPVerificationError(
            "wrong_typ",
            f"UCP signature typ must be {_UCP_TYP!r}; got {header.get('typ')!r}.",
        )
    if header.get("alg") not in _ALLOWED_ALGS:
        raise UCPVerificationError(
            "unsupported_alg",
            f"UCP signing alg must be one of {_ALLOWED_ALGS}; got {header.get('alg')!r}.",
        )
    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise UCPVerificationError("missing_kid", "UCP signature header missing `kid`.")

    keys_list = jwks.get("keys", []) if isinstance(jwks, dict) else []
    matches = [k for k in keys_list if isinstance(k, dict) and k.get("kid") == kid]
    if not matches:
        raise UCPVerificationError("kid_not_found", f"No JWK in JWKS matching kid={kid!r}.")
    if len(matches) > 1:
        raise UCPVerificationError(
            "duplicate_kid",
            f"JWKS contains {len(matches)} keys with kid={kid!r}; expected exactly one.",
        )
    matched = matches[0]
    # RFC 7517 §4.2: reject keys not intended for signature verification.
    matched_use = matched.get("use")
    if matched_use is not None and matched_use != "sig":
        raise UCPVerificationError(
            "unusable_key",
            f"JWK with kid={kid!r} has use={matched_use!r}; expected 'sig'.",
        )
    # RFC 7517 §4.4: a JWK with declared `alg` constrains its use to that algorithm.
    header_alg = header.get("alg")
    matched_alg = matched.get("alg")
    if matched_alg is not None and matched_alg != header_alg:
        raise UCPVerificationError(
            "unusable_key",
            f"JWK alg {matched_alg!r} does not match JWS header alg {header_alg!r}.",
        )

    stripped = {k: v for k, v in signed_profile.items() if k != "signature"}
    try:
        expected_payload = _canonicalize_profile(stripped)
    except (ValueError, TypeError) as exc:
        raise UCPVerificationError(
            "body_mismatch",
            f"Failed to canonicalize received profile for verification: {exc}",
        ) from exc

    key_set = KeySet.import_key_set(cast("Any", {"keys": matches}))
    registry = JWSRegistry(algorithms=list(_ALLOWED_ALGS))
    try:
        with _suppress_joserfc_eddsa_warning():
            obj = jws.deserialize_compact(sig, key_set, registry=registry)
    except Exception as exc:
        # joserfc raises various subclasses. Wrap in our own type so callers
        # don't need to import joserfc internals.
        from joserfc.errors import (  # type: ignore[import-not-found]
            BadSignatureError,
            DecodeError,
            UnsupportedHeaderError,
        )

        if isinstance(exc, BadSignatureError):
            raise UCPVerificationError("signature_invalid", f"UCP signature verification failed: {exc}") from exc
        if isinstance(exc, DecodeError):
            raise UCPVerificationError("malformed_jws", f"Malformed JWS: {exc}") from exc
        # RFC 7515 §4.1.11 / RFC 8725 §3.10: a verifier MUST reject any JWS
        # whose `crit` header carries an extension the implementation doesn't
        # understand.
        if isinstance(exc, UnsupportedHeaderError):
            raise UCPVerificationError(
                "unrecognized_critical_header",
                f"UCP signing rejected unrecognized critical header: {exc}",
            ) from exc
        raise

    # Compare the bytes that were actually signed against the canonical body of the
    # profile we received. ``deserialize_compact`` validates the JWS against the bytes
    # embedded in the JWS payload segment — but the profile body could have been
    # swapped after signing while the JWS stayed unchanged.
    if not hmac.compare_digest(obj.payload, expected_payload):
        raise UCPVerificationError(
            "body_mismatch",
            "UCP profile body does not match the signed payload (tampered or non-canonical).",
        )

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
    "UCPVerificationError",
    "build_jwks_response",
    "generate_ucp_signing_key",
    "sign_ucp_profile",
    "verify_ucp_profile",
]
