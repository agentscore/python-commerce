"""UCP (Universal Commerce Protocol) profile builder.

Compose the JSON payload published at ``/.well-known/ucp`` per the UCP spec, with
AgentScore identity claims attached as a capability. Returned object is the unsigned
profile body — the merchant signs it (or wraps it in their JWKS-backed envelope)
before publishing.

Why publish: UCP is the Google-led cross-vendor standard (announced Jan 2026 with
broad ecosystem support). Every UCP-aware platform discovers a merchant via
``/.well-known/ucp``, so shipping this profile means AgentScore-gated merchants are
discoverable through the same surface every other UCP merchant uses.

Spec reference: https://ucp.dev/

UCP profiles do NOT carry KYC / sanctions / age / jurisdiction claims natively —
identity in the UCP spec is "who signed this" (JWKS-backed). AgentScore claims layer
over UCP via a custom capability so consumers who care about verified-buyer identity
can read them; consumers who don't care just see a normal UCP profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from agentscore_commerce.identity.types import AssessResult

_DEFAULT_VERSION = "2026-04-17"
_SPEC_URL = "https://ucp.dev/"
# Reverse-DNS namespacing per UCP convention (``^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+$``).
# The bare ``agentscore-identity`` form fails the spec regex; vendor-namespacing under the
# ``sh.agentscore`` authority is honest about the capability being our extension, not a
# UCP-canonical slot.
AGENTSCORE_UCP_CAPABILITY = "sh.agentscore.identity"
"""Capability name AgentScore registers in the UCP profile. Consumers filter on this
to find verified-buyer claims attached to the profile."""

_AGENTSCORE_CAPABILITY_VERSION = "1"


@dataclass
class UCPSigningKey:
    """JWK entry for the profile's ``signing_keys`` array.

    Pass through the public key material verbatim — UCP requires JWKS-format keys.
    """

    kid: str
    kty: str
    alg: str | None = None
    use: str | None = None
    crv: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    """Additional JWK fields (x, y, n, e, etc.) merged into the serialized output."""

    _RESERVED = frozenset({"kid", "kty", "alg", "use", "crv"})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kid": self.kid, "kty": self.kty}
        if self.alg is not None:
            out["alg"] = self.alg
        if self.use is not None:
            out["use"] = self.use
        if self.crv is not None:
            out["crv"] = self.crv
        for k, v in self.extras.items():
            if k in self._RESERVED:
                msg = f"UCPSigningKey.extras key {k!r} collides with a reserved field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out

    @classmethod
    def from_jwk(cls, jwk: dict[str, Any]) -> UCPSigningKey:
        """Construct a UCPSigningKey from a public JWK dict.

        Routes the JWK's known fields (kid/kty/alg/use/crv) onto the dataclass and
        captures any other fields (x/y/n/e/etc.) into ``extras``. Use this when
        publishing the output of :func:`generate_ucp_signing_key` directly.

        Rejects symmetric (``oct``) keys and JWKs missing required fields with a
        typed ``ValueError`` rather than a bare ``KeyError``.
        """
        if not isinstance(jwk, dict):
            msg = f"UCPSigningKey.from_jwk expected a dict; got {type(jwk).__name__}."
            raise ValueError(msg)
        if "kid" not in jwk:
            msg = "UCPSigningKey.from_jwk: JWK missing required field `kid`."
            raise ValueError(msg)
        if "kty" not in jwk:
            msg = "UCPSigningKey.from_jwk: JWK missing required field `kty`."
            raise ValueError(msg)
        if jwk["kty"] not in {"OKP", "EC", "RSA"}:
            msg = (
                f"UCPSigningKey.from_jwk: kty={jwk['kty']!r} is not a supported "
                "asymmetric key type (expected OKP, EC, or RSA). Symmetric `oct` "
                "keys are rejected because they cannot publicly verify a JWS in "
                "the trust-mode UCP flow."
            )
            raise ValueError(msg)

        known = {"kid", "kty", "alg", "use", "crv"}
        return cls(
            kid=jwk["kid"],
            kty=jwk["kty"],
            alg=jwk.get("alg"),
            use=jwk.get("use"),
            crv=jwk.get("crv"),
            extras={k: v for k, v in jwk.items() if k not in known},
        )


@dataclass
class UCPService:
    """Transport binding entry."""

    type: str
    url: str | None = None
    version: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    _RESERVED = frozenset({"type", "url", "version"})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.url is not None:
            out["url"] = self.url
        if self.version is not None:
            out["version"] = self.version
        for k, v in self.extras.items():
            if k in self._RESERVED:
                msg = f"UCPService.extras key {k!r} collides with a reserved field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out


@dataclass
class UCPCapability:
    """Capability entry — name + schema URL + version + claims."""

    name: str
    schema: str | None = None
    version: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    _RESERVED = frozenset({"name", "schema", "version"})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.schema is not None:
            out["schema"] = self.schema
        if self.version is not None:
            out["version"] = self.version
        for k, v in self.extras.items():
            if k in self._RESERVED:
                msg = f"UCPCapability.extras key {k!r} collides with a reserved field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out


@dataclass
class UCPPaymentHandler:
    """Payment handler entry — name + config."""

    name: str
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Match Node SDK: omit `config` when empty (TypeScript optional-property
        # convention). Node's `UCPPaymentHandler.config` is `Record<string, unknown>?`
        # and `buildUCPProfile` passes the array verbatim, so a Node caller writing
        # `{ name: 'tempo' }` ships a wire profile WITHOUT the `config` key. Python
        # must do the same or the same logical input produces different canonical
        # bytes between SDKs. Callers who explicitly pass `config={}` get the same
        # treatment because an empty dict is semantically identical to "absent".
        out: dict[str, Any] = {"name": self.name}
        if self.config:
            out["config"] = self.config
        return out


@dataclass
class UCPProfile:
    """UCP profile body for ``/.well-known/ucp``.

    Use :meth:`to_dict` to serialize. Sign + envelope with your JWKS-backed signing
    flow before publishing.
    """

    services: list[UCPService] = field(default_factory=list)
    capabilities: list[UCPCapability] = field(default_factory=list)
    payment_handlers: list[UCPPaymentHandler] = field(default_factory=list)
    signing_keys: list[UCPSigningKey] = field(default_factory=list)
    name: str | None = None
    version: str = _DEFAULT_VERSION
    spec: str = _SPEC_URL
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "spec": self.spec,
            "services": [s.to_dict() for s in self.services],
            "capabilities": [c.to_dict() for c in self.capabilities],
            "payment_handlers": [h.to_dict() for h in self.payment_handlers],
            "signing_keys": [k.to_dict() for k in self.signing_keys],
        }
        if self.name is not None:
            out["name"] = self.name
        # Filter `extras` so a caller passing
        # ``extras={"signing_keys": [...]}`` can't silently destroy the
        # explicit field. ``__proto__`` / ``constructor`` / ``prototype``
        # match the node-commerce reserved set so a Node-signed profile
        # carrying those keys is rejected identically by both SDKs.
        reserved = {
            "version",
            "spec",
            "services",
            "capabilities",
            "payment_handlers",
            "signing_keys",
            "name",
            "signature",
            "__proto__",
            "constructor",
            "prototype",
        }
        for k, v in self.extras.items():
            if k in reserved:
                msg = f"UCPProfile.extras key {k!r} collides with a reserved profile field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out


def build_ucp_profile(
    services: list[UCPService],
    signing_keys: list[UCPSigningKey],
    capabilities: list[UCPCapability] | None = None,
    payment_handlers: list[UCPPaymentHandler] | None = None,
    name: str | None = None,
    version: str = _DEFAULT_VERSION,
    data: AssessResult | None = None,
    agentscore_schema_url: str | None = None,
    extras: dict[str, Any] | None = None,
) -> UCPProfile:
    """Compose a UCP profile body for ``/.well-known/ucp`` publication.

    Merges AgentScore identity claims into ``capabilities`` as an
    ``sh.agentscore.identity`` capability when ``data`` carries a resolved operator.
    Consumers reading the profile can opt into the AgentScore claims by filtering
    on the capability name.

    Example::

        from agentscore_commerce.identity.ucp import (
            UCPService,
            UCPSigningKey,
            UCPPaymentHandler,
            build_ucp_profile,
        )

        @app.get("/.well-known/ucp")
        async def ucp_profile():
            result = await client.acheck(identity)
            return build_ucp_profile(
                name="Example Merchant",
                services=[UCPService(type="rest", url="https://agents.example.com")],
                payment_handlers=[
                    UCPPaymentHandler(name="tempo", config={"recipient": TEMPO_ADDR}),
                    UCPPaymentHandler(name="stripe", config={"profile_id": STRIPE_PROFILE_ID}),
                ],
                signing_keys=[
                    UCPSigningKey(kid="merchant-2026-04", kty="EC", alg="ES256", crv="P-256",
                                  extras={"x": "...", "y": "..."}),
                ],
                data=result,
            ).to_dict()
    """
    base_capabilities = list(capabilities or [])

    if data is not None and data.resolved_operator:
        # Read typed AssessResult fields first (the canonical path). Fall back to
        # ``data.raw["operator_verification"]`` / ``data.raw["account_verification"]``
        # only when the typed field is ``None``; this is a Python-only legacy
        # escape hatch for callers who hand-construct ``AssessResult(raw=..., typed=None)``.
        # Node has no raw fallback at all (it reads typed fields directly via
        # optional chaining), so the typed-empty-wins-over-raw behavior is also
        # Python-only: a Python caller who passes ``account_verification={}``
        # explicitly suppresses the raw fallback (empty dict is None-distinguished
        # via ``is None``). Production callers populate typed fields consistently,
        # so this asymmetry is theoretical for typical usage.
        typed_op = data.operator_verification
        operator_verification: dict[str, Any]
        if typed_op is None:
            raw = data.raw or {}
            raw_op = raw.get("operator_verification") if isinstance(raw, dict) else None
            operator_verification = raw_op if isinstance(raw_op, dict) else {}
        elif isinstance(typed_op, dict):
            operator_verification = cast("dict[str, Any]", typed_op)
        else:
            # Convert OperatorVerification dataclass to a plain dict.
            operator_verification = {
                "level": getattr(typed_op, "level", None),
                "operator_type": getattr(typed_op, "operator_type", None),
                "verified_at": getattr(typed_op, "verified_at", None),
            }

        account_verification: dict[str, Any]
        if data.account_verification is None:
            raw = data.raw or {}
            raw_av = raw.get("account_verification") if isinstance(raw, dict) else None
            account_verification = raw_av if isinstance(raw_av, dict) else {}
        elif isinstance(data.account_verification, dict):
            account_verification = data.account_verification
        else:
            account_verification = {}
        # `dict.get(k) or DEFAULT` (not `dict.get(k, DEFAULT)`) coerces both a
        # missing key AND a present-but-falsy (None / "") value to the default,
        # matching the node sibling's `||` semantics. The API can return
        # `account_verification` with either null or `""` for un-set fields
        # depending on the row state, and a profile signed in one language must
        # verify in the other across both shapes.
        claims = {
            "operator_id": data.resolved_operator,
            "kyc_level": account_verification.get("kyc_level") or operator_verification.get("level") or "none",
            "sanctions_clear": account_verification.get("sanctions_clear") is True,
            "age_bracket": account_verification.get("age_bracket") or "unknown",
            "jurisdiction": account_verification.get("jurisdiction") or "",
            "verified_at": account_verification.get("verified_at") or operator_verification.get("verified_at"),
            "verify_url": data.verify_url,
            "issuer": "https://agentscore.sh",
        }
        base_capabilities.append(
            UCPCapability(
                name=AGENTSCORE_UCP_CAPABILITY,
                version=_AGENTSCORE_CAPABILITY_VERSION,
                schema=agentscore_schema_url or "https://agentscore.sh/schemas/ucp/sh-agentscore-identity-v1.json",
                extras={"claims": claims},
            ),
        )

    return UCPProfile(
        services=services,
        capabilities=base_capabilities,
        payment_handlers=list(payment_handlers or []),
        signing_keys=signing_keys,
        name=name,
        version=version,
        extras=extras or {},
    )


__all__ = [
    "AGENTSCORE_UCP_CAPABILITY",
    "UCPCapability",
    "UCPPaymentHandler",
    "UCPProfile",
    "UCPService",
    "UCPSigningKey",
    "build_ucp_profile",
]
