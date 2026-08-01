"""RFC 9421 HTTP Message Signatures — the AIP-constrained subset.

AIP (Agentic Identity Protocol) binds an Agent Identity Token (AIT) to the
agent that presents it: the agent signs each HTTP request with the private key whose
public half is carried in the AIT's ``cnf.jwk`` (RFC 7800). A verifier reconstructs the
RFC 9421 signature base, verifies it against ``cnf.jwk``, and confirms the signature's
``keyid`` equals the JWK thumbprint (RFC 7638) of that key. A stolen AIT is then useless
without the bound private key.

This module implements ONLY the shape AIP uses, not the full RFC 9421 grammar:

* Covered components: the derived components ``@method``, ``@authority``, ``@path``, plus
  the ``agent-identity`` header field. (The AIP "minimum required" set.) Extra components in
  a presented signature are accepted and covered if the caller supplies their values.
* One labeled signature per request, tagged ``tag="agent-identity"``. Web Bot Auth
  signatures (``tag="web-bot-auth"``) may coexist on the same request under a different
  label; we select ours by tag, ignoring the rest.
* Algorithm: Ed25519 (EdDSA over OKP/Ed25519). AIP's default and only signing curve.

The structured-field parsing here is deliberately narrow: it parses the AIP member of the
``Signature-Input`` / ``Signature`` dictionaries (a parenthesized inner list + integer/string
params, and a single byte-sequence value). It is not a general RFC 8941 parser.

Byte-compatibility: this is a behavior-exact port of the reference ``aip/http-signature.ts``.
A node/pay-signed proof-of-possession MUST verify here and vice versa. The signature base,
component ordering, canonicalization, the standard (not base64url) base64 of the signature
value, and the RFC 7638 thumbprint are all reproduced byte-for-byte.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentscore_commerce.aip.types import Jwk

# The AIP "minimum required" covered components, in canonical order.
AIP_COVERED_COMPONENTS: tuple[str, ...] = ("@method", "@authority", "@path", "agent-identity")

# Tag that identifies the AIP signature among coexisting RFC 9421 signatures.
AIP_SIGNATURE_TAG = "agent-identity"

# Default clock-skew tolerance (seconds) for ``created`` / ``expires``. Aligned to the AIP
# spec's recommended 60s window (and to the JWT iat/exp tolerance) so the whole AIP check
# uses one value.
_DEFAULT_MAX_SKEW_SECONDS = 60

# Hard ceiling on the PoP signature's own declared lifetime (``expires - created``), in seconds.
# Requiring ``created``+``expires`` bounds replay to the declared window — but with no ceiling a
# malicious trusted-issuer agent could set ``expires = created + (AIT lifetime)`` and replay for the
# full window. Cap it tightly so every accepted PoP is short-lived. First-party ``pay`` signs a 60s
# window, so it passes; this only bites a signer that declares an over-long PoP. Matches the
# authoritative API verifier's ``MAX_POP_WINDOW_SECONDS`` (the AgentScore API verifier) so the
# edge (standalone ``aip_gate``) and the API can't drift. (Distinct from the AIT JWT's ``exp - iat``
# ceiling in verify.py — this is the HTTP-signature layer.)
MAX_POP_WINDOW_SECONDS = 120

# Verification failure reasons. Mirrors the reference ``VerifyFailureReason`` union exactly.
VerifyFailureReason = Literal[
    "no_aip_signature",
    "malformed_signature_input",
    "malformed_signature",
    "unsupported_alg",
    "missing_keyid",
    "keyid_mismatch",
    "missing_covered_component",
    "created_missing",
    "expires_missing",
    "pop_window_too_long",
    "created_in_future",
    "expired",
    "unsupported_cnf_key",
    "signature_invalid",
]


@dataclass
class SignatureParams:
    """Parameters parsed from (or used to build) a ``Signature-Input`` member."""

    components: list[str] = field(default_factory=list)
    created: int | None = None
    expires: int | None = None
    keyid: str | None = None
    tag: str | None = None
    alg: str | None = None


@dataclass(frozen=True)
class VerifyMessageSignatureResult:
    """Result of :func:`verify_message_signature`.

    Mirrors the reference discriminated union (``{ ok: true, params } | { ok: false, reason }``).
    On success ``ok`` is ``True`` and ``params`` holds the selected signature params; on
    failure ``ok`` is ``False`` and ``reason`` names the failure mode.
    """

    ok: bool
    params: SignatureParams | None = None
    reason: VerifyFailureReason | None = None


@dataclass(frozen=True)
class _DictMember:
    """A single ``label=value`` member of a structured-field dictionary."""

    label: str
    value: str


class _MissingComponentError(Exception):
    """Raised by :func:`build_signature_base` when a covered component has no value."""

    def __init__(self, component: str) -> None:
        super().__init__(f"signature base missing covered component: {component}")
        self.component = component


def normalize_authority(authority: str) -> str:
    """Normalize an authority for ``@authority`` per RFC 9421 §2.2.3.

    Lowercase, drop the default port for the scheme. We don't know the scheme here, so we
    drop the common defaults (80/443).
    """
    lower = authority.strip().lower()
    colon = lower.rfind(":")
    if colon == -1:
        return lower
    # Guard against IPv6 literals like ``[::1]`` with no port.
    if "]" in lower and colon < lower.index("]"):
        return lower
    host = lower[:colon]
    port = lower[colon + 1 :]
    if port in ("80", "443"):
        return host
    return lower


def _component_value(
    name: str,
    *,
    method: str,
    authority: str,
    path: str,
    agent_identity: str,
    extra: Mapping[str, str] | None,
) -> str | None:
    """Serialize one derived/header component value into its signature-base line value."""
    if name == "@method":
        return method.upper()
    if name == "@authority":
        return normalize_authority(authority)
    if name == "@path":
        return path
    if name == "agent-identity":
        return agent_identity.strip()
    return extra.get(name) if extra is not None else None


def _serialize_component_list(components: list[str]) -> str:
    """Serialize the inner-list of covered components: ``("@method" "@authority" ...)``."""
    return "(" + " ".join(f'"{c}"' for c in components) + ")"


def _serialize_params(p: SignatureParams) -> str:
    """Serialize the ``;k=v`` params suffix in canonical order."""
    parts: list[str] = []
    if p.created is not None:
        parts.append(f"created={p.created}")
    if p.expires is not None:
        parts.append(f"expires={p.expires}")
    if p.keyid is not None:
        parts.append(f'keyid="{p.keyid}"')
    if p.alg is not None:
        parts.append(f'alg="{p.alg}"')
    if p.tag is not None:
        parts.append(f'tag="{p.tag}"')
    return "".join(f";{s}" for s in parts)


def build_signature_base(
    params: SignatureParams,
    *,
    method: str,
    authority: str,
    path: str,
    agent_identity: str,
    extra: Mapping[str, str] | None = None,
    raw_params: str | None = None,
) -> str:
    r"""Build the RFC 9421 signature base.

    One line per covered component, then the ``@signature-params`` line. Components are
    joined by ``\n`` with no trailing newline. Raises :class:`_MissingComponentError` if a
    covered component has no available value.

    When ``raw_params`` is given (the verify path), it is used VERBATIM as the
    ``@signature-params`` value — the signer signed over its own serialization, so re-serializing
    parsed params in a fixed order would break a spec-legal signer that emitted them in a
    different order. The sign path omits it and serializes canonically.
    """
    lines: list[str] = []
    for name in params.components:
        value = _component_value(
            name,
            method=method,
            authority=authority,
            path=path,
            agent_identity=agent_identity,
            extra=extra,
        )
        if value is None:
            raise _MissingComponentError(name)
        lines.append(f'"{name}": {value}')
    if raw_params is not None:
        params_value = raw_params
    else:
        params_value = _serialize_component_list(params.components) + _serialize_params(params)
    lines.append(f'"@signature-params": {params_value}')
    return "\n".join(lines)


def _push_member(out: list[_DictMember], raw: str) -> None:
    trimmed = raw.strip()
    if not trimmed:
        return
    eq = trimmed.find("=")
    if eq == -1:
        return
    label = trimmed[:eq].strip()
    value = trimmed[eq + 1 :].strip()
    if label:
        out.append(_DictMember(label=label, value=value))


def _split_dictionary(header: str) -> list[_DictMember]:
    """Split a dictionary header into ``label=value`` members at top-level commas.

    Respects parentheses (inner lists) and colon-delimited byte sequences so commas inside
    them don't split. Narrow but correct for the AIP shapes we emit/consume.
    """
    out: list[_DictMember] = []
    depth = 0
    in_bytes = False
    in_string = False
    current = ""
    for i, ch in enumerate(header):
        if in_string:
            current += ch
            if ch == '"' and (i == 0 or header[i - 1] != "\\"):
                in_string = False
            continue
        if ch == '"':
            in_string = True
            current += ch
            continue
        if ch == ":":
            in_bytes = not in_bytes
            current += ch
            continue
        if not in_bytes and ch == "(":
            depth += 1
            current += ch
            continue
        if not in_bytes and ch == ")":
            depth = max(0, depth - 1)
            current += ch
            continue
        if not in_bytes and depth == 0 and ch == ",":
            _push_member(out, current)
            current = ""
            continue
        current += ch
    _push_member(out, current)
    return out


# Match ;key=value where value is an integer or a quoted string.
_PARAM_RE = re.compile(r';\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*("(?:[^"\\]|\\.)*"|-?\d+)')
# Match each quoted component name inside the inner list.
_COMPONENT_RE = re.compile(r'"[^"]*"')


def _parse_inner_list_member(value: str) -> SignatureParams | None:
    """Parse an inner-list member value: ``("@method" ...);created=...;keyid="...";tag="..."``."""
    open_idx = value.find("(")
    close_idx = value.find(")", open_idx + 1)
    if open_idx == -1 or close_idx == -1:
        return None
    list_body = value[open_idx + 1 : close_idx].strip()
    components = [] if len(list_body) == 0 else [s[1:-1] for s in _COMPONENT_RE.findall(list_body)]

    params = SignatureParams(components=components)
    param_str = value[close_idx + 1 :]
    for match in _PARAM_RE.finditer(param_str):
        key = match.group(1)
        raw = match.group(2)
        # The regex captures either a quoted string or a (signed) integer. created/expires
        # are integer params; keyid/tag/alg are string params. Real signers always emit the
        # matching shape, so we coerce per-key: integer params -> ``int``, string params ->
        # the unquoted (or bare) token. Mirrors the reference per-key assignment of
        # ``raw.startsWith('"') ? raw.slice(1,-1) : Number(raw)``.
        is_quoted = raw.startswith('"')
        if key == "created":
            params.created = int(raw) if not is_quoted else None
        elif key == "expires":
            params.expires = int(raw) if not is_quoted else None
        elif key == "keyid":
            params.keyid = raw[1:-1] if is_quoted else raw
        elif key == "tag":
            params.tag = raw[1:-1] if is_quoted else raw
        elif key == "alg":
            params.alg = raw[1:-1] if is_quoted else raw
    return params


@dataclass(frozen=True)
class ParsedSignatureInput:
    """A selected ``Signature-Input`` member.

    Carries the dictionary ``label``, parsed ``params``, and the ``raw`` member value
    (everything after ``label=``, exactly as received, OWS-trimmed) — the verify path
    rebuilds the base over the RAW serialization, not a re-serialization.
    """

    label: str
    params: SignatureParams
    raw: str


def parse_signature_input(header: str, tag: str = AIP_SIGNATURE_TAG) -> ParsedSignatureInput | None:
    """Parse a ``Signature-Input`` dictionary and return the member tagged ``tag``.

    The spec requires ``tag="agent-identity"``: an untagged member is skipped like any
    wrong-tagged member. Returns ``None`` if no tagged member is found or the member is malformed.
    """
    members = _split_dictionary(header)
    if len(members) == 0:
        return None

    parsed: list[ParsedSignatureInput] = []
    for m in members:
        params = _parse_inner_list_member(m.value)
        if params is not None:
            parsed.append(ParsedSignatureInput(label=m.label, params=params, raw=m.value))

    if len(parsed) == 0:
        return None

    return next((p for p in parsed if p.params.tag == tag), None)


def parse_signature_value(header: str, label: str) -> bytes | None:
    """Parse a ``Signature`` dictionary and return the byte-sequence value for ``label``.

    RFC 8941 byte sequences are ``:<base64>:`` (standard base64, not base64url).
    """
    members = _split_dictionary(header)
    member = next((m for m in members if m.label == label), None)
    if member is None:
        return None
    v = member.value.strip()
    if not v.startswith(":") or not v.endswith(":") or len(v) < 2:
        return None
    b64 = v[1:-1]
    try:
        # ``validate=True`` rejects non-base64 alphabet bytes so a malformed value returns
        # ``None`` rather than silently decoding garbage (matching node's ``atob`` throwing).
        return base64.b64decode(b64, validate=True)
    except Exception:  # any decode error → malformed, return None
        return None


def _calculate_jwk_thumbprint(jwk: Jwk) -> str:
    """RFC 7638 SHA-256 JWK thumbprint, byte-identical to jose's ``calculateJwkThumbprint``.

    joserfc's ``OKPKey.thumbprint()`` canonicalizes ``{crv, kty, x}`` with sorted keys and
    no whitespace, SHA-256s it, and base64url-no-pad encodes — exactly the RFC 7638
    construction jose uses. Verified byte-equal cross-language. Raises on a malformed JWK;
    the caller catches.
    """
    from joserfc.jwk import OKPKey  # type: ignore[import-not-found]

    return OKPKey.import_key(jwk).thumbprint()


def verify_message_signature(
    *,
    method: str,
    authority: str,
    path: str,
    agent_identity: str,
    signature_input: str,
    signature: str,
    cnf_jwk: Jwk,
    now: int | None = None,
    max_skew_seconds: int | None = None,
    extra_components: Mapping[str, str] | None = None,
) -> VerifyMessageSignatureResult:
    """Verify an AIP HTTP Message Signature. Performs the full check.

    1. select the AIP-tagged member of ``Signature-Input``
    2. confirm the AIP minimum covered components are present
    3. REQUIRE both ``created`` and ``expires``, reject an over-long declared window
       (``expires - created`` > MAX_POP_WINDOW_SECONDS -> ``pop_window_too_long``), then enforce
       them against ``now`` with skew tolerance. Both are mandatory: an optional time bound is no
       time bound — without ``expires`` a captured ``(token, Signature-Input, Signature)`` triple is
       replayable for the whole AIT lifetime. A signature omitting either is rejected
       (``created_missing`` / ``expires_missing``). This matches the authoritative API verifier
       (the AgentScore API verifier) so a merchant running ``aip_gate`` STANDALONE (the
       crypto-identity-only deployment with no ``/v1/assess``) gets the same replay defense.
    4. confirm ``keyid`` equals the RFC 7638 thumbprint of ``cnf.jwk``
    5. reconstruct the signature base and verify Ed25519 over it

    Note:
        This is a STATELESS verifier — it bounds the replay WINDOW but does not dedupe within it.
        A captured triple can still be replayed until ``expires`` (<= MAX_POP_WINDOW_SECONDS + skew
        from ``created``). A stateful seen-signature cache (as in the authoritative API) is out of
        scope for the SDK edge; the tight window bound is the meaningful mitigation here.

    Args:
        method: HTTP method, e.g. ``POST``. Case-insensitive; normalized to upper.
        authority: Authority (host[:port]). Lowercased; default ports dropped.
        path: Request path (no query), e.g. ``/checkout``.
        agent_identity: Raw value of the ``Agent-Identity`` header the signature covers.
        signature_input: Raw ``Signature-Input`` header value.
        signature: Raw ``Signature`` header value.
        cnf_jwk: The agent's public key from the AIT's ``cnf.jwk``.
        now: Wall-clock seconds; defaults to now. Injectable for tests.
        max_skew_seconds: Skew tolerance for created/expires. Defaults to 60.
        extra_components: Extra covered-component values, keyed by component name (for
            components beyond the AIP minimum).
    """
    selected = parse_signature_input(signature_input)
    if selected is None:
        return VerifyMessageSignatureResult(ok=False, reason="no_aip_signature")
    label = selected.label
    params = selected.params

    # The ``alg`` param is optional in RFC 9421 (the verifier derives the algorithm from the
    # key); when a signer does include it, the registered HTTP-sig label is ``ed25519``.
    # Accept that plus the JWS spelling ``EdDSA``, case-insensitively, so a spec-loose
    # external signer isn't wrongly rejected — the actual key type is still pinned to
    # OKP/Ed25519 below, so this only affects the label.
    if params.alg is not None and params.alg.lower() not in ("ed25519", "eddsa"):
        return VerifyMessageSignatureResult(ok=False, reason="unsupported_alg")

    # All AIP-minimum components must be covered.
    for required in AIP_COVERED_COMPONENTS:
        if required not in params.components:
            return VerifyMessageSignatureResult(ok=False, reason="missing_covered_component")

    # REQUIRE both ``created`` and ``expires``. Treating them as optional leaves an unbounded replay
    # window — a captured signature with no ``expires`` is valid for the AIT's full lifetime. Reject
    # when either is absent so every accepted PoP carries an explicit, enforceable time bound. (Our
    # pay signer always emits both with a 60s window; this only rejects spec-loose external signers.)
    if params.created is None:
        return VerifyMessageSignatureResult(ok=False, reason="created_missing")
    if params.expires is None:
        return VerifyMessageSignatureResult(ok=False, reason="expires_missing")

    # Bound the PoP's own declared lifetime. created+expires alone only bound replay to whatever
    # window the SIGNER chose — a malicious trusted-issuer agent could declare a window as wide as
    # the AIT lifetime and replay for all of it. Reject an over-long window so every accepted PoP is
    # short-lived. (pay signs 60s; this only bites a signer declaring > MAX_POP_WINDOW_SECONDS.)
    # A NEGATIVE window (expires before created) is nonsense and would otherwise slip under the
    # cap; reject it with the same window-violation reason.
    if params.expires < params.created:
        return VerifyMessageSignatureResult(ok=False, reason="pop_window_too_long")
    if params.expires - params.created > MAX_POP_WINDOW_SECONDS:
        return VerifyMessageSignatureResult(ok=False, reason="pop_window_too_long")

    now_s = now if now is not None else int(time.time())
    skew = max_skew_seconds if max_skew_seconds is not None else _DEFAULT_MAX_SKEW_SECONDS
    if params.created > now_s + skew:
        return VerifyMessageSignatureResult(ok=False, reason="created_in_future")
    if params.expires < now_s - skew:
        return VerifyMessageSignatureResult(ok=False, reason="expired")

    if not params.keyid:
        return VerifyMessageSignatureResult(ok=False, reason="missing_keyid")

    # The RFC 9421 proof-of-possession is verified with the agent's ``cnf`` key, which AIP
    # binds as Ed25519 (OKP). Validate the key shape BEFORE thumbprinting / importing: a
    # malformed JWK (missing or non-string ``x``) makes the thumbprint throw, and a non-OKP
    # key (e.g. a P-256 EC cnf) makes the Ed25519 import throw. Neither call site below
    # catches an unguarded throw into a crash — reject with a typed failure instead.
    # (Note: the JWT alg allowlist permits ES256 for the IDP *issuer* signing key, a
    # different key.)
    cnf: dict[str, Any] = cnf_jwk if isinstance(cnf_jwk, dict) else {}
    if (
        cnf.get("kty") != "OKP"
        or cnf.get("crv") != "Ed25519"
        or not isinstance(cnf.get("x"), str)
        or len(cnf.get("x", "")) == 0
    ):
        return VerifyMessageSignatureResult(ok=False, reason="unsupported_cnf_key")

    try:
        thumbprint = _calculate_jwk_thumbprint(cnf_jwk)
    except Exception:  # any thumbprint failure → unusable cnf key
        return VerifyMessageSignatureResult(ok=False, reason="unsupported_cnf_key")
    if params.keyid != thumbprint:
        return VerifyMessageSignatureResult(ok=False, reason="keyid_mismatch")

    sig = parse_signature_value(signature, label)
    if not sig:
        return VerifyMessageSignatureResult(ok=False, reason="malformed_signature")

    try:
        base = build_signature_base(
            params,
            method=method,
            authority=authority,
            path=path,
            agent_identity=agent_identity,
            extra=extra_components,
            # Verify over the RAW received member serialization — param order is the signer's.
            raw_params=selected.raw,
        )
    except _MissingComponentError:
        return VerifyMessageSignatureResult(ok=False, reason="missing_covered_component")

    # cnf key shape (OKP/Ed25519 with a string ``x``) was validated above, before
    # thumbprinting. Any crypto/import/verify failure is a verification failure, never an
    # uncaught throw.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from joserfc.jwk import OKPKey  # type: ignore[import-not-found]

    try:
        raw = OKPKey.import_key(cnf_jwk).raw_value
        # ``cnf.jwk`` is a public key, but derive the public half defensively so a private
        # JWK (which carries ``d``) also verifies — mirrors jose ``importJWK`` + ``subtle``,
        # which verify against the public part regardless.
        public_key = raw.public_key() if isinstance(raw, Ed25519PrivateKey) else raw
        # crv=Ed25519 was enforced above, so raw_value is an Ed25519 key; the isinstance
        # narrows joserfc's broad OKP union to the concrete Ed25519PublicKey for verify().
        if not isinstance(public_key, Ed25519PublicKey):
            return VerifyMessageSignatureResult(ok=False, reason="unsupported_cnf_key")
        public_key.verify(sig, base.encode("utf-8"))
    except InvalidSignature:
        return VerifyMessageSignatureResult(ok=False, reason="signature_invalid")
    except Exception:  # malformed key / wrong curve / etc. → invalid
        return VerifyMessageSignatureResult(ok=False, reason="signature_invalid")
    return VerifyMessageSignatureResult(ok=True, params=params)


@dataclass(frozen=True)
class SignedMessage:
    """The ``Signature-Input`` and ``Signature`` header values for an AIP request."""

    signature_input: str
    signature: str


def sign_message(
    *,
    method: str,
    authority: str,
    path: str,
    agent_identity: str,
    private_jwk: Jwk,
    public_jwk: Jwk,
    created: int | None = None,
    expires: int | None = None,
    label: str = "ait",
    components: list[str] | None = None,
    extra_components: Mapping[str, str] | None = None,
) -> SignedMessage:
    """Build the ``Signature-Input`` and ``Signature`` header values for an AIP request.

    Args:
        method: HTTP method, e.g. ``POST``.
        authority: Authority (host[:port]).
        path: Request path (no query).
        agent_identity: Raw ``Agent-Identity`` header value the signature covers.
        private_jwk: Agent private key (Ed25519 JWK with ``d``).
        public_jwk: Agent public key; used to derive ``keyid`` (thumbprint).
        created: ``created`` param; defaults to now.
        expires: ``expires`` param; omitted when ``None``.
        label: Signature dictionary label. Defaults to ``ait``.
        components: Covered components; defaults to the AIP minimum.
        extra_components: Extra covered-component values, keyed by component name.

    Raises:
        TypeError: if ``private_jwk`` is not an Ed25519 private key.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from joserfc.jwk import OKPKey  # type: ignore[import-not-found]

    comps = components if components is not None else list(AIP_COVERED_COMPONENTS)
    created_v = created if created is not None else int(time.time())
    keyid = _calculate_jwk_thumbprint(public_jwk)

    params = SignatureParams(
        components=comps,
        created=created_v,
        expires=expires,
        keyid=keyid,
        tag=AIP_SIGNATURE_TAG,
    )

    base = build_signature_base(
        params,
        method=method,
        authority=authority,
        path=path,
        agent_identity=agent_identity,
        extra=extra_components,
    )

    # Raw Ed25519 over the signature base bytes — NOT a JWS. Matches node's
    # ``subtle.sign('Ed25519', key, bytes)`` which emits the 64-byte raw signature, then
    # standard-base64 (not base64url) encodes it into the ``:...:`` byte-sequence value.
    raw_private = OKPKey.import_key(private_jwk).raw_value
    # Narrow joserfc's broad OKP union to the concrete Ed25519PrivateKey for sign(); mirrors
    # node throwing when the imported key is not an Ed25519 private CryptoKey.
    if not isinstance(raw_private, Ed25519PrivateKey):
        msg = "sign_message: expected an Ed25519 private key"
        raise TypeError(msg)
    sig_bytes = raw_private.sign(base.encode("utf-8"))
    b64 = base64.b64encode(sig_bytes).decode("ascii")

    signature_input = f"{label}={_serialize_component_list(comps)}{_serialize_params(params)}"
    signature = f"{label}=:{b64}:"
    return SignedMessage(signature_input=signature_input, signature=signature)


__all__ = [
    "AIP_COVERED_COMPONENTS",
    "AIP_SIGNATURE_TAG",
    "MAX_POP_WINDOW_SECONDS",
    "ParsedSignatureInput",
    "SignatureParams",
    "SignedMessage",
    "VerifyFailureReason",
    "VerifyMessageSignatureResult",
    "build_signature_base",
    "normalize_authority",
    "parse_signature_input",
    "parse_signature_value",
    "sign_message",
    "verify_message_signature",
]
