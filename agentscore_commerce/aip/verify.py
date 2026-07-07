"""AIP Agent Identity Token (AIT) verification pipeline — the verifier orchestrator.

This is the function a merchant gate calls. It executes the spec's verification steps over
a presented request, composing the three foundation modules:

* :mod:`~agentscore_commerce.aip.jwks` — trusted-issuer enforcement + key discovery
* :mod:`~agentscore_commerce.aip.http_signature` — RFC 9421 proof-of-possession over the request
* :mod:`~agentscore_commerce.aip.types` — AIT structural contract

Steps (per spec):

1. read the ``Agent-Identity`` header (one or more)
2. decode the JWT header (``kid``) + payload; confirm AIT shape (``cnf`` + ``agent``)
3. resolve the IdP's signing key from its JWKS (trusted-issuer + HTTPS enforced)
4. verify the IdP signature on the JWT (reject ``alg:none``; key is Ed25519)
5. check ``exp`` / ``iat`` with skew
6. extract ``cnf.jwk``
7. verify the RFC 9421 HTTP Message Signature with ``cnf.jwk``
8. confirm the signature ``keyid`` == JWK thumbprint of ``cnf.jwk``  (done inside step 7)

On success it returns the validated, signature-checked claims. On failure it returns a
typed reason that maps onto AIP's wire error codes (the gate turns these into 401/403).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentscore_commerce.aip.http_signature import verify_message_signature
from agentscore_commerce.aip.types import AitPayload, is_ait_shape, validate_ait_payload

if TYPE_CHECKING:
    from agentscore_commerce.aip.jwks import JwksCache

# Header that carries the AIT JWT.
AGENT_IDENTITY_HEADER = "agent-identity"

# Allowed AIT JWT signature algorithms (RFC 8725 §3.1 allowlist). EdDSA is AIP's default; ES256
# is permitted for parity with the server-side verifier. Anything else (RS*, HS*, none) is
# rejected at JWT verification, regardless of what alg the token header claims or the resolved JWK
# supports.
AIT_SIGNING_ALGS = ("EdDSA", "ES256")


# Failure reasons, aligned with AIP wire error codes. The gate maps:
#   no_token / malformed_token / invalid_signature / expired_token  -> 401
#   untrusted_issuer / weak_auth / invalid_claims                   -> 403
VerifyAitFailure = Literal[
    "no_token",
    "malformed_token",
    "untrusted_issuer",
    "key_unavailable",
    "idp_signature_invalid",
    "expired_token",
    "invalid_claims",
    "pop_signature_missing",
    "pop_signature_invalid",
]


@dataclass
class VerifyRequestContext:
    """Request fields the verifier needs. Framework-agnostic; adapters map their req onto this."""

    method: str
    authority: str
    path: str
    # All ``Agent-Identity`` header values present on the request (one per IdP).
    agent_identity_headers: list[str]
    signature_input: str | None
    signature: str | None


@dataclass
class SignatureMaterial:
    """RFC 9421 signature material forwarded to ``/v1/assess`` as ``aip_signature``.

    The API re-verifies proof-of-possession authoritatively (the edge check here is only a
    fail-fast filter; the API is the source of truth).
    """

    method: str
    authority: str
    path: str
    signature_input: str
    signature: str


@dataclass
class VerifiedAit:
    """A verified, signature-checked AIT plus the material a gate forwards to ``/v1/assess``."""

    payload: AitPayload
    # The issuer (canonical, as presented).
    iss: str
    # The agent's bound public key (``cnf.jwk``).
    cnf_jwk: dict[str, Any]
    # The raw JWT string that verified (the winning ``Agent-Identity`` header value, Bearer
    # prefix stripped). Lets a gate forward the exact token to ``/v1/assess`` as ``aip_token``.
    token: str
    signature_material: SignatureMaterial


@dataclass
class VerifyAitSuccess:
    """Successful AIT verification — ``ait`` holds the verified, key-bound token."""

    ait: VerifiedAit
    ok: Literal[True] = True


@dataclass
class VerifyAitFailureResult:
    """Failed AIT verification — ``reason`` names the typed verify-failure (-> wire error code)."""

    reason: VerifyAitFailure
    ok: Literal[False] = False


# Mirrors the reference discriminated union (``{ ok: true, ait } | { ok: false, reason }``): ``ok``
# discriminates, so a ``not result.ok`` guard narrows ``ait`` / ``reason`` to non-``None``.
VerifyAitResult = VerifyAitSuccess | VerifyAitFailureResult


async def verify_ait(
    ctx: VerifyRequestContext,
    *,
    jwks: JwksCache,
    now: float | None = None,
    max_skew_seconds: float | None = None,
    max_lifetime_seconds: float | None = None,
) -> VerifyAitResult:
    """Verify the AIP credential on a request.

    When multiple ``Agent-Identity`` headers are present, each is tried; the first that fully
    verifies AND whose ``cnf.jwk`` matches the request's RFC 9421 signature wins (all AITs on
    one request must share the same ``cnf`` key, so the PoP signature is checked once against
    the winning key).

    ``max_lifetime_seconds`` caps the accepted AIT lifetime (``exp - iat``). Defense-in-depth
    vs an external issuer minting a long-lived bearer credential (the spec recommends 60-300s).
    Default 300 (matching the authoritative API verifier, lowered from 3600) so a stolen AIT's
    usable window stays short even at the edge (standalone ``aip_gate``, no ``/v1/assess``).
    """
    if len(ctx.agent_identity_headers) == 0:
        return VerifyAitFailureResult(reason="no_token")
    if not ctx.signature_input or not ctx.signature:
        return VerifyAitFailureResult(reason="pop_signature_missing")
    # Captured post-guard (str, not str | None) — reused for the local fail-fast PoP check and
    # forwarded to /v1/assess so the API can re-verify the same proof-of-possession authoritatively.
    signature_input = ctx.signature_input
    signature = ctx.signature

    last_failure: VerifyAitFailure = "malformed_token"

    for raw in ctx.agent_identity_headers:
        token = _strip_bearer(raw)

        # Step 2: decode header + payload, confirm AIT shape.
        try:
            header = _decode_protected_header(token)
            payload = _decode_jwt(token)
        except (ValueError, binascii.Error, json.JSONDecodeError):
            last_failure = "malformed_token"
            continue
        alg = header.get("alg")
        if not isinstance(alg, str) or alg.lower() == "none":
            last_failure = "malformed_token"
            continue
        if not is_ait_shape(payload):
            last_failure = "malformed_token"
            continue

        # Structural contract (incl. human_confirmed -> amr).
        validated = validate_ait_payload(payload)
        if not validated.ok or validated.payload is None:
            last_failure = "invalid_claims"
            continue
        claims = validated.payload

        # Step 3: resolve IdP key (trusted-issuer + HTTPS enforced inside).
        key_lookup = await jwks.get_key(claims["iss"], header.get("kid"))
        if not key_lookup.ok:
            # `untrusted_issuer` (not on the allowlist) and `insecure_issuer` (http:// issuer) are both
            # PERMANENT trust failures -> 403, not the retryable 503 of `key_unavailable` (a transient
            # JWKS-fetch problem). Don't tell an agent to retry a config error it can't fix.
            last_failure = (
                "untrusted_issuer"
                if key_lookup.reason in ("untrusted_issuer", "insecure_issuer")
                else "key_unavailable"
            )
            continue
        idp_key = key_lookup.key
        if idp_key is None:
            # `ok=True` guarantees a key per the JwksCache contract; this guard only narrows the
            # `Jwk | None` type (and defends a sibling regression) — treat a missing key as unavailable.
            last_failure = "key_unavailable"
            continue

        # Step 4 + 5: verify IdP signature; enforce alg match + expiry/skew. The JWT iat/exp tolerance
        # and the RFC 9421 PoP `created`/`expires` window are both 60s (the AIP spec's recommended
        # window). An explicit `max_skew_seconds` override, when set, applies to both.
        jwt_clock_tolerance = max_skew_seconds if max_skew_seconds is not None else 60
        try:
            _verify_idp_signature(
                token,
                idp_key,
                # Pin the signature algorithm allowlist (RFC 8725 §3.1) — also rejects `alg:none`.
                # Without this, a trusted IdP publishing a non-Ed25519 (e.g. RSA/EC) `use:sig` key
                # would let an attacker present an RS256/ES256 token that verifies. Matches the
                # server-side allowlist in the AgentScore API verifier.
                algorithms=list(AIT_SIGNING_ALGS),
                clock_tolerance=jwt_clock_tolerance,
                now=now,
            )
        except _JwtExpiredError:
            last_failure = "expired_token"
            continue
        except _JwtVerifyError:
            last_failure = "idp_signature_invalid"
            continue

        # Spec step 5 also requires `iat` not be in the future. The JWT verify above validates
        # `exp`/`nbf` but does not reject a future `iat`, so check it explicitly (same skew tolerance).
        now_sec = now if now is not None else math.floor(time.time())
        if claims["iat"] > now_sec + jwt_clock_tolerance:
            last_failure = "expired_token"
            continue

        # Defense-in-depth: reject a long-lived AIT (spec recommends 60-300s). Own mint is exactly
        # 300s, so first-party tokens pass at the ceiling; this bites a trusted EXTERNAL issuer
        # minting a longer-lived bearer credential. Lowered 3600 -> 300 (matching the authoritative
        # API verifier) to keep a stolen token's window short, complementing the mandatory PoP bound.
        if claims["exp"] - claims["iat"] > (max_lifetime_seconds if max_lifetime_seconds is not None else 300):
            last_failure = "expired_token"
            continue

        # Step 6 + 7 + 8: PoP — verify the RFC 9421 signature against cnf.jwk. `verify_message_signature`
        # is synchronous (joserfc crypto is sync, unlike node's async WebCrypto), so no await. Its
        # `now`/`max_skew_seconds` are integer seconds (compared against integer `created`/`expires`);
        # floor a float clock to int seconds, identical to the JWT-path flooring above.
        pop_result = verify_message_signature(
            method=ctx.method,
            authority=ctx.authority,
            path=ctx.path,
            # The agent-identity covered component is the BARE AIT (a Bearer prefix, if present, is
            # transport that `_strip_bearer` removed above). Verify over `token`, not `raw`, so the edge
            # and the API — which verifies over the forwarded bare aip_token — reconstruct the identical
            # base.
            agent_identity=token,
            signature_input=signature_input,
            signature=signature,
            cnf_jwk=claims["cnf"]["jwk"],
            now=math.floor(now) if now is not None else None,
            max_skew_seconds=math.floor(max_skew_seconds) if max_skew_seconds is not None else None,
        )
        if not pop_result.ok:
            last_failure = "pop_signature_invalid"
            continue

        return VerifyAitSuccess(
            ait=VerifiedAit(
                payload=claims,
                iss=claims["iss"],
                cnf_jwk=claims["cnf"]["jwk"],
                token=token,
                signature_material=SignatureMaterial(
                    method=ctx.method,
                    authority=ctx.authority,
                    path=ctx.path,
                    signature_input=signature_input,
                    signature=signature,
                ),
            ),
        )

    return VerifyAitFailureResult(reason=last_failure)


# ── JWT decode + IdP signature verification (joserfc) ──


class _JwtVerifyError(Exception):
    """IdP JWT signature/alg verification failed (maps to ``idp_signature_invalid``)."""


class _JwtExpiredError(_JwtVerifyError):
    """IdP JWT failed the ``exp`` check (maps to ``expired_token``)."""


def _b64url_json(segment: str) -> Any:
    """Decode one base64url JWT segment into the JSON value it carries.

    Mirrors jose's ``decodeProtectedHeader`` / ``decodeJwt``: parse a segment WITHOUT verifying
    the signature (the IdP key isn't known until ``iss``/``kid`` are read). Raises on a malformed
    segment so the caller maps it to ``malformed_token``.
    """
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def _decode_protected_header(token: str) -> dict[str, Any]:
    """Decode the JWT protected header (first segment) without verifying. Mirrors jose's helper."""
    parts = token.split(".")
    if len(parts) < 2:
        msg = "not a JWT (expected header.payload.signature)"
        raise ValueError(msg)
    decoded = _b64url_json(parts[0])
    if not isinstance(decoded, dict):
        msg = "JWT header is not a JSON object"
        raise ValueError(msg)
    return decoded


def _decode_jwt(token: str) -> Any:
    """Decode the JWT payload (second segment) without verifying. Mirrors jose's ``decodeJwt``."""
    parts = token.split(".")
    if len(parts) < 2:
        msg = "not a JWT (expected header.payload.signature)"
        raise ValueError(msg)
    return _b64url_json(parts[1])


def _verify_idp_signature(
    token: str,
    key_jwk: dict[str, Any],
    *,
    algorithms: list[str],
    clock_tolerance: float,
    now: float | None,
) -> None:
    """Verify the IdP signature on the AIT JWT and enforce ``exp``/``nbf`` with skew.

    Pins the algorithm allowlist (rejecting ``alg:none`` and any non-allowlisted alg the
    resolved JWK might support) and validates ``exp``/``nbf`` with ``clock_tolerance`` leeway,
    matching jose's ``jwtVerify({ algorithms, clockTolerance, currentDate })``.

    ``iat`` is deliberately NOT validated here: jose's ``jwtVerify`` does not reject a future
    ``iat``, but joserfc's ``JWTClaimsRegistry.validate_iat`` does (raising a generic
    ``InvalidClaimError``). To stay behavior-exact with node — which checks the future-``iat``
    case itself and maps it to ``expired_token`` (NOT ``idp_signature_invalid``) — drop ``iat``
    from the validated claims so joserfc never sees it; the caller does the future-``iat`` check.

    Raises :class:`_JwtExpiredError` on expiry and :class:`_JwtVerifyError` on any other
    signature/alg/decode failure.
    """
    from joserfc import jwt  # type: ignore[import-not-found]
    from joserfc.errors import ExpiredTokenError, JoseError  # type: ignore[import-not-found]
    from joserfc.jwk import import_key  # type: ignore[import-not-found]

    try:
        key = import_key(key_jwk)
        # `algorithms` is the RFC 8725 allowlist; joserfc rejects a token whose header alg is
        # outside it (and `alg:none` is never in the allowlist) before checking the signature.
        decoded = jwt.decode(token, key, algorithms=algorithms)
    except JoseError as exc:
        raise _JwtVerifyError(str(exc)) from exc

    # joserfc's `decode` verifies the signature but does NOT validate temporal claims — run the
    # claims registry separately (mirrors jose's `jwtVerify`, which checks `exp`/`nbf` after the
    # signature). `leeway` == the clock tolerance; an integer `now` pins the comparison clock for
    # tests. `exp`/`nbf` are integer seconds per spec; floor a float clock to integer seconds.
    # `validate()` only runs `validate_<claim>` for keys PRESENT in the dict, so dropping `iat`
    # skips joserfc's future-`iat` rejection (see docstring) while keeping `exp`/`nbf`.
    now_int = math.floor(now) if now is not None else None
    leeway = math.floor(clock_tolerance) if clock_tolerance >= 0 else 0
    temporal_claims = {k: v for k, v in decoded.claims.items() if k != "iat"}
    registry = jwt.JWTClaimsRegistry(now=now_int, leeway=leeway)
    try:
        registry.validate(temporal_claims)
    except ExpiredTokenError as exc:
        raise _JwtExpiredError(str(exc)) from exc
    except JoseError as exc:
        # A future `nbf` (or other temporal-claim failure) is not an expiry; treat as a generic
        # signature/validity failure, matching node where only ERR_JWT_EXPIRED maps to expired_token.
        raise _JwtVerifyError(str(exc)) from exc


# `Bearer ` prefix: a literal `bearer` (case-insensitive) followed by >=1 whitespace char.
# Mirrors the reference `/^bearer\s+/i`.
_BEARER_PREFIX = re.compile(r"^bearer\s+", re.IGNORECASE)


def _strip_bearer(value: str) -> str:
    """Strip an optional ``Bearer `` prefix from a header value (case-insensitive)."""
    trimmed = value.strip()
    return _BEARER_PREFIX.sub("", trimmed, count=1) if _BEARER_PREFIX.match(trimmed) else trimmed


__all__ = [
    "AGENT_IDENTITY_HEADER",
    "AIT_SIGNING_ALGS",
    "SignatureMaterial",
    "VerifiedAit",
    "VerifyAitFailure",
    "VerifyAitFailureResult",
    "VerifyAitResult",
    "VerifyAitSuccess",
    "VerifyRequestContext",
    "verify_ait",
]
