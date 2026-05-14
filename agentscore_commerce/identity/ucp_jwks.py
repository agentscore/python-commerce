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
import logging
import os
import threading
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

_JOSE_INSTALL_HINT = (
    "Install the optional dependency: `pip install agentscore-commerce[ucp]` (or `uv pip install joserfc`)."
)

_ALLOWED_ALGS = ("EdDSA", "ES256")
# JWS protected header ``typ`` value. Vendor-namespaced because UCP §6 does not define
# a profile-as-JWS typ; the value advertises that this signed envelope follows the
# AgentScore extension semantics rather than a UCP-canonical signing convention.
_PROFILE_TYP = "agentscore-profile+jws"

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
    """Walk ``value`` and raise on anything that won't survive cross-language parity.

    Three failure modes are rejected:

    * Non-integer ``float`` values. Cross-language float canonicalization (RFC 8785
      §3.2.2.3) diverges between Python's ``json.dumps`` and Node's ``JSON.stringify``
      (e.g. ``1.0`` vs ``1``, ``1e-7`` vs ``1e-07``). Use decimal strings (``"9.99"``)
      for monetary or fractional fields.
    * ``int`` values whose magnitude exceeds ``Number.MAX_SAFE_INTEGER`` (2^53 - 1).
      Python ints are arbitrary-width, but JS verifiers parse the canonical body via
      ``JSON.parse`` which silently loses precision past 2^53. Use a decimal string
      for any integer that may exceed the safe range.
    * Strings containing U+2028 (LINE SEPARATOR) or U+2029 (PARAGRAPH SEPARATOR).
      Pre-ES2019 V8 (and any environment whose ``JSON.stringify`` still escapes
      these codepoints) emits the escaped sequences while
      ``json.dumps(ensure_ascii=False)`` emits them raw, so the canonical bytes
      would diverge across the Node and Python siblings. Mirror of the rejection
      in ``core/api/src/lib/canonicalize.ts``.

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
    if isinstance(value, str):
        if "\u2028" in value or "\u2029" in value:
            msg = (
                "UCP profile strings containing U+2028 (LINE SEPARATOR) or "
                "U+2029 (PARAGRAPH SEPARATOR) are not allowed; cross-language "
                "byte parity requires neither be present (Node JSON.stringify "
                "on older V8 escapes them; Python json.dumps with "
                "ensure_ascii=False does not)."
            )
            raise ValueError(msg)
        return
    # Reject set / frozenset with a typed message (mirrors the node sibling's
    # "Set values are not allowed" rejection in stableStringify). Without this,
    # an empty set or a set-of-valid-strings falls through `_reject_unsafe_numbers`
    # cleanly and surfaces a raw `TypeError` from `json.dumps` later. Sets aren't
    # representable in JSON; convert to a sorted list before passing.
    if isinstance(value, set | frozenset):
        msg = (
            f"{type(value).__name__} values are not allowed in canonicalized JSON. "
            "Convert to a sorted list before passing."
        )
        raise ValueError(msg)
    # Reject bytes / bytearray with a typed message (mirrors the node sibling's
    # "typed arrays are not allowed" rejection in stableStringify). Without this,
    # raw bytes fall through cleanly and surface a confusing
    # `TypeError: Object of type bytes is not JSON serializable` from
    # `json.dumps` later. Convert to a base64url string before passing.
    if isinstance(value, bytes | bytearray):
        msg = (
            f"{type(value).__name__} values are not allowed in canonicalized JSON. "
            "Convert to a base64url string before passing."
        )
        raise ValueError(msg)
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_unsafe_numbers(k)
            _reject_unsafe_numbers(v)
    elif isinstance(value, list | tuple):
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

        profile = build_ucp_profile(..., signing_keys=[UCPSigningKey.from_jwk(key.public_jwk)])
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
    header = {"alg": alg, "kid": kid, "typ": _PROFILE_TYP}
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
      * the JWS protected header carries ``kid`` + ``typ='agentscore-profile+jws'`` + a
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
    if header.get("typ") != _PROFILE_TYP:
        raise UCPVerificationError(
            "wrong_typ",
            f"UCP signature typ must be {_PROFILE_TYP!r}; got {header.get('typ')!r}.",
        )
    if header.get("alg") not in _ALLOWED_ALGS:
        raise UCPVerificationError(
            "unsupported_alg",
            f"UCP signing alg must be one of {_ALLOWED_ALGS}; got {header.get('alg')!r}.",
        )
    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise UCPVerificationError("missing_kid", "UCP signature header missing `kid`.")

    # UCP doesn't define any critical headers; any crit advertised is by definition
    # unrecognized. Reject before the JWKS kid lookup so a crit-violating JWS with a
    # missing/duplicate/unusable kid surfaces crit (not kid_not_found / duplicate_kid /
    # unusable_key), matching node-commerce's manual peek order:
    # typ -> alg -> kid -> crit -> kid_lookup. Cross-language ordering parity is
    # non-obvious because joserfc's deserialize_compact only enforces crit AFTER
    # the kid lookup, so we must check it here ourselves.
    # Gate on key-presence (not `is not None`) so that JSON `null` falls through to
    # the shape check and surfaces typed `malformed_jws`, not joserfc's raw TypeError
    # when it tries to iterate `None`. RFC 7515 §4.1.11 requires a non-empty array.
    if "crit" in header:
        crit = header["crit"]
        if not isinstance(crit, list) or len(crit) == 0 or not all(isinstance(c, str) for c in crit):
            raise UCPVerificationError(
                "malformed_jws",
                f"JWS protected header crit must be a non-empty array of strings; got {crit!r}.",
            )
        raise UCPVerificationError(
            "unrecognized_critical_header",
            f"JWS protected header advertises unrecognized crit headers: {crit!r}.",
        )

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
    # ``use`` and ``alg`` are optional per RFC 7517; an explicit JSON null is
    # out-of-spec but treat it as absent (skip-on-null) so a JWK with
    # ``"use": null`` matches the Node sibling's ``!= null`` semantics in
    # ucp-jwks.ts and the two languages stay symmetric.
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
    # joserfc's KeySet.import_key_set runs a stricter dict-key validation that
    # rejects ``use: None`` / ``alg: None`` outright. Strip explicit nulls for
    # those two fields before handing the JWK off so skip-on-null actually
    # propagates to the import step.
    matches = [{k: v for k, v in matched.items() if not (k in ("use", "alg") and v is None)}]

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


# ── env-driven loader (extracted from store + martin + signed_ucp_merchant) ──

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadUCPSigningKeyOptions:
    """Configuration for :func:`load_ucp_signing_key_from_env`.

    Env-var names are overridable so a merchant can run multiple distinct signing
    keys from different env namespaces (e.g. ``PROD_UCP_JWK`` vs ``STAGING_UCP_JWK``).
    ``default_kid`` and ``default_alg`` are used when the env JWK is absent or
    doesn't carry its own ``kid`` / can't dictate alg via kty+crv.
    """

    env_jwk_var: str = "UCP_SIGNING_KEY_JWK_PRIVATE"
    env_kid_var: str = "UCP_SIGNING_KEY_KID"
    env_alg_var: str = "UCP_SIGNING_KEY_ALG"
    default_kid: str = "merchant-default"
    default_alg: Literal["EdDSA", "ES256"] = "EdDSA"


_env_loader_cache: dict[tuple[str, str, str, str, str], GeneratedUCPKey] = {}
_env_loader_lock = threading.Lock()


def _read_env_trimmed(name: str) -> str | None:
    r"""Read ``name`` from env, strip whitespace, treat whitespace-only as unset.

    A secret-manager export piped through ``xargs`` appends ``\n``, which would otherwise
    make ``UCP_SIGNING_KEY_JWK_PRIVATE`` fail ``json.loads`` with a misleading error.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed or None


def _detect_alg_from_jwk(jwk: dict[str, Any]) -> Literal["EdDSA", "ES256"] | None:
    """Detect signing alg from JWK shape; returns ``None`` for unsupported kty/crv."""
    kty = jwk.get("kty")
    crv = jwk.get("crv")
    if kty == "OKP" and crv == "Ed25519":
        return "EdDSA"
    if kty == "EC" and crv == "P-256":
        return "ES256"
    return None


def _build_env_signing_key(opts: LoadUCPSigningKeyOptions) -> GeneratedUCPKey:
    """Load (or generate) one signing key per env state. No locking (caller wraps)."""
    kid_default = _read_env_trimmed(opts.env_kid_var) or opts.default_kid
    raw_alg = (_read_env_trimmed(opts.env_alg_var) or "").upper()
    # Case-insensitive env-alg comparison: secret configs commonly carry casing
    # drift (``"es256"``, ``" ES256 "``, ``"eS256"``). Strict exact-match would
    # silently downgrade to the default and operators would publish a JWKS
    # containing the wrong key family.
    alg_fallback: Literal["EdDSA", "ES256"] = "ES256" if raw_alg == "ES256" else opts.default_alg

    env_jwk = _read_env_trimmed(opts.env_jwk_var)
    if env_jwk:
        from joserfc.jwk import ECKey, OKPKey  # type: ignore[import-not-found]

        try:
            jwk_dict = json.loads(env_jwk)
        except json.JSONDecodeError as exc:
            msg = f"{opts.env_jwk_var} is not valid JSON: {exc}"
            raise ValueError(msg) from exc

        if not isinstance(jwk_dict, dict) or not jwk_dict:
            msg = f"{opts.env_jwk_var} must be a non-empty JWK object; got {type(jwk_dict).__name__}."
            raise ValueError(msg)

        detected_alg = _detect_alg_from_jwk(jwk_dict)
        if not detected_alg:
            msg = (
                f"{opts.env_jwk_var} has unsupported kty/crv "
                f"(got kty={jwk_dict.get('kty')!r} crv={jwk_dict.get('crv')!r}); "
                "expected OKP+Ed25519 or EC+P-256."
            )
            raise ValueError(msg)

        try:
            priv = OKPKey.import_key(jwk_dict) if detected_alg == "EdDSA" else ECKey.import_key(jwk_dict)
        except Exception as exc:
            # Do NOT interpolate the underlying exception message; some import paths echo
            # back fields of the input JWK including private key material. Surface only
            # the exception class so logs never carry key bytes through stderr / CloudWatch.
            msg = (
                f"{opts.env_jwk_var} has malformed key material ({type(exc).__name__}). "
                "Verify the JWK is well-formed and matches the declared kty/crv. "
                "Underlying details suppressed to avoid leaking key bytes."
            )
            raise ValueError(msg) from exc

        # Project to canonical public fields per kty so unknown env JWK fields
        # (key_ops, x5c, x5t, x5u, etc.) don't leak into the published JWKS.
        raw = priv.as_dict(private=False)
        if detected_alg == "EdDSA":
            public_jwk: dict[str, Any] = {
                "kty": raw["kty"],
                "crv": raw["crv"],
                "x": raw["x"],
            }
        else:
            public_jwk = {
                "kty": raw["kty"],
                "crv": raw["crv"],
                "x": raw["x"],
                "y": raw["y"],
            }
        # Empty-string kid in env JWK falls through to the configured default —
        # publishing `"kid": ""` would break every kid-pinning verifier.
        public_jwk["kid"] = jwk_dict.get("kid") or kid_default
        public_jwk["alg"] = detected_alg
        public_jwk["use"] = "sig"
        _logger.info(
            "Loaded persistent UCP signing key kid=%s alg=%s from %s",
            public_jwk["kid"],
            detected_alg,
            opts.env_jwk_var,
        )
        return GeneratedUCPKey(private_key=priv, public_jwk=public_jwk)

    _logger.error(
        "%s not set; generating ephemeral signing key. Verifier caches will break across restarts. "
        "NOT SAFE FOR PRODUCTION.",
        opts.env_jwk_var,
    )
    return generate_ucp_signing_key(kid=kid_default, alg=alg_fallback)


def load_ucp_signing_key_from_env(opts: LoadUCPSigningKeyOptions | None = None) -> GeneratedUCPKey:
    """Load the merchant's UCP signing key from env, with concurrent-safe caching.

    On first call (per ``opts``): reads ``opts.env_jwk_var``, parses it as a JWK,
    validates kty/crv (OKP+Ed25519 or EC+P-256), and projects to a canonical
    public JWK. Falls back to an ephemeral keypair when the env var is missing
    or whitespace-only (dev-friendly; logs a loud warning).

    Subsequent calls with the same ``opts`` return the cached key without
    re-reading env. Concurrent first-callers serialize on a lock so only one
    key generation runs; the rest receive the cached result.

    Different ``opts`` values get separate cache entries: a merchant running
    one signing key per env namespace (e.g. prod vs staging) does not collide.

    Env-driven precedence:

    * Embedded ``kid`` in the JWK wins over ``opts.env_kid_var`` env value;
      empty-string ``kid`` in the env JWK falls through to ``opts.default_kid``.
    * Structural ``kty``+``crv`` in the JWK wins over ``opts.env_alg_var`` env
      value (which is only consulted in the ephemeral fallback path).

    Raises ``ValueError`` with a sanitized message for malformed env JWKs;
    raw exception detail is intentionally suppressed so key bytes can never
    reach logs.
    """
    resolved = opts if opts is not None else LoadUCPSigningKeyOptions()
    cache_key = (
        resolved.env_jwk_var,
        resolved.env_kid_var,
        resolved.env_alg_var,
        resolved.default_kid,
        resolved.default_alg,
    )
    cached = _env_loader_cache.get(cache_key)
    if cached is not None:
        return cached
    with _env_loader_lock:
        cached = _env_loader_cache.get(cache_key)
        if cached is not None:
            return cached
        result = _build_env_signing_key(resolved)
        _env_loader_cache[cache_key] = result
        return result


def _reset_ucp_signing_key_cache() -> None:
    """Test-only: clear the env-loader cache.

    Use after ``monkeypatch.setenv(...)`` / ``monkeypatch.delenv(...)`` to force
    the next ``load_ucp_signing_key_from_env`` call to re-read the env state.
    """
    with _env_loader_lock:
        _env_loader_cache.clear()


__all__ = [
    "GeneratedUCPKey",
    "LoadUCPSigningKeyOptions",
    "UCPVerificationError",
    "build_jwks_response",
    "generate_ucp_signing_key",
    "load_ucp_signing_key_from_env",
    "sign_ucp_profile",
    "verify_ucp_profile",
]
