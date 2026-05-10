"""UCP (Universal Commerce Protocol) profile builder.

Compose the JSON payload published at ``/.well-known/ucp`` per the UCP spec. Output
shape matches the spec example: top-level ``{"ucp": {...}, "signing_keys": [...]}``
envelope, with ``services`` / ``capabilities`` / ``payment_handlers`` as MAPS keyed by
reverse-DNS name (UCP spec §3 + §6).

AgentScore identity claims layer over UCP via the ``sh.agentscore.identity`` capability
(vendor-namespaced; UCP doesn't define KYC/sanctions/age/jurisdiction natively).

Pass the unsigned profile through :func:`sign_ucp_profile` to attach the
``agentscore-profile+jws`` signature for trust-mode verifiers (vendor extension; UCP
itself doesn't mandate profile-body signing).

Spec reference: https://ucp.dev/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from agentscore_commerce.identity.types import AssessResult

_DEFAULT_VERSION = "2026-04-17"

# Reverse-DNS namespacing per UCP convention. The bare ``agentscore-identity`` form
# fails the spec regex; vendor-namespacing under the ``sh.agentscore`` authority is
# honest about the capability being our extension, not a UCP-canonical slot.
AGENTSCORE_UCP_CAPABILITY = "sh.agentscore.identity"
"""Capability name AgentScore registers in the UCP profile. Consumers filter on this
to find verified-buyer claims attached to the profile."""

_AGENTSCORE_CAPABILITY_VERSION = "1"
_AGENTSCORE_DEFAULT_SPEC_URL = "https://agentscore.sh/specification/identity"
_AGENTSCORE_DEFAULT_SCHEMA_URL = "https://agentscore.sh/schemas/ucp/sh-agentscore-identity-v1.json"


@dataclass
class UCPSigningKey:
    """JWK entry for the profile's ``signing_keys`` array.

    Pass through public key material verbatim; UCP requires JWKS-format keys.
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
        """
        if not isinstance(jwk, dict):
            msg = f"UCPSigningKey.from_jwk expected a dict; got {type(jwk).__name__}."
            raise ValueError(msg)
        if not isinstance(jwk.get("kid"), str) or not jwk["kid"]:
            msg = "UCPSigningKey.from_jwk: JWK missing required field `kid` (or non-string/empty)."
            raise ValueError(msg)
        if not isinstance(jwk.get("kty"), str) or not jwk["kty"]:
            msg = "UCPSigningKey.from_jwk: JWK missing required field `kty` (or non-string/empty)."
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
class UCPServiceBinding:
    """Transport binding entry — keyed under a service name (e.g., ``dev.ucp.shopping``)."""

    version: str
    spec: str
    transport: Literal["rest", "mcp", "a2a", "embedded"]
    endpoint: str | None = None
    schema: str | None = None
    id: str | None = None
    config: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    _RESERVED = frozenset({"version", "spec", "transport", "endpoint", "schema", "id", "config"})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "spec": self.spec,
            "transport": self.transport,
        }
        if self.endpoint is not None:
            out["endpoint"] = self.endpoint
        if self.schema is not None:
            out["schema"] = self.schema
        if self.id is not None:
            out["id"] = self.id
        if self.config:
            out["config"] = self.config
        for k, v in self.extras.items():
            if k in self._RESERVED:
                msg = f"UCPServiceBinding.extras key {k!r} collides with a reserved field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out


@dataclass
class UCPCapabilityBinding:
    """Capability binding entry — keyed under a capability name (e.g., ``dev.ucp.shopping.checkout``)."""

    version: str
    spec: str
    schema: str
    id: str | None = None
    config: dict[str, Any] | None = None
    extends: str | list[str] | None = None
    requires: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    _RESERVED = frozenset({"version", "spec", "schema", "id", "config", "extends", "requires"})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "spec": self.spec,
            "schema": self.schema,
        }
        if self.id is not None:
            out["id"] = self.id
        if self.config:
            out["config"] = self.config
        if self.extends is not None:
            out["extends"] = self.extends
        if self.requires is not None:
            out["requires"] = self.requires
        for k, v in self.extras.items():
            if k in self._RESERVED:
                msg = f"UCPCapabilityBinding.extras key {k!r} collides with a reserved field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out


@dataclass
class UCPPaymentHandlerBinding:
    """Payment handler binding entry — keyed under a handler reverse-DNS name (e.g., ``com.google.pay``)."""

    id: str
    version: str
    spec: str
    schema: str
    available_instruments: list[dict[str, Any]] | None = None
    config: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    _RESERVED = frozenset({"id", "version", "spec", "schema", "available_instruments", "config"})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "version": self.version,
            "spec": self.spec,
            "schema": self.schema,
        }
        if self.available_instruments is not None:
            out["available_instruments"] = self.available_instruments
        if self.config:
            out["config"] = self.config
        for k, v in self.extras.items():
            if k in self._RESERVED:
                msg = f"UCPPaymentHandlerBinding.extras key {k!r} collides with a reserved field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out


@dataclass
class UCPProfileBody:
    """UCP body — nested under the ``ucp`` key of the published profile."""

    version: str = _DEFAULT_VERSION
    services: dict[str, list[UCPServiceBinding]] = field(default_factory=dict)
    capabilities: dict[str, list[UCPCapabilityBinding]] = field(default_factory=dict)
    payment_handlers: dict[str, list[UCPPaymentHandlerBinding]] = field(default_factory=dict)
    name: str | None = None
    supported_versions: dict[str, str] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    _RESERVED = frozenset(
        {
            "version",
            "name",
            "services",
            "capabilities",
            "payment_handlers",
            "supported_versions",
            "__proto__",
            "constructor",
            "prototype",
        },
    )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "services": {k: [s.to_dict() for s in bindings] for k, bindings in self.services.items()},
            "capabilities": {k: [c.to_dict() for c in bindings] for k, bindings in self.capabilities.items()},
            "payment_handlers": {k: [h.to_dict() for h in bindings] for k, bindings in self.payment_handlers.items()},
        }
        if self.name is not None:
            out["name"] = self.name
        if self.supported_versions is not None:
            out["supported_versions"] = self.supported_versions
        for k, v in self.extras.items():
            if k in self._RESERVED:
                msg = f"UCPProfileBody.extras key {k!r} collides with a reserved `ucp` field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out


@dataclass
class UCPProfile:
    """UCP profile body for ``/.well-known/ucp``.

    Top-level shape: ``{"ucp": {...}, "signing_keys": [...], "signature?": "..."}``.
    Use :meth:`to_dict` to serialize. Pass through :func:`sign_ucp_profile` to attach
    the JWS signature.
    """

    ucp: UCPProfileBody = field(default_factory=UCPProfileBody)
    signing_keys: list[UCPSigningKey] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    _RESERVED = frozenset(
        {"ucp", "signing_keys", "signature", "__proto__", "constructor", "prototype"},
    )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ucp": self.ucp.to_dict(),
            "signing_keys": [k.to_dict() for k in self.signing_keys],
        }
        for k, v in self.extras.items():
            if k in self._RESERVED:
                msg = f"UCPProfile.extras key {k!r} collides with a reserved profile field; rejected."
                raise ValueError(msg)
            out[k] = v
        return out


def build_ucp_profile(
    services: dict[str, list[UCPServiceBinding]] | None = None,
    signing_keys: list[UCPSigningKey] | None = None,
    *,
    capabilities: dict[str, list[UCPCapabilityBinding]] | None = None,
    payment_handlers: dict[str, list[UCPPaymentHandlerBinding]] | None = None,
    name: str | None = None,
    version: str = _DEFAULT_VERSION,
    data: AssessResult | None = None,
    agentscore_schema_url: str | None = None,
    agentscore_spec_url: str | None = None,
    supported_versions: dict[str, str] | None = None,
    ucp_extras: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> UCPProfile:
    """Compose a UCP profile body for ``/.well-known/ucp`` publication.

    Returns the spec-compliant shape: ``{"ucp": {...}, "signing_keys": [...]}``
    with ``services`` / ``capabilities`` / ``payment_handlers`` as maps keyed by
    reverse-DNS name. Pass through :func:`sign_ucp_profile` to attach a JWS
    signature for trust-mode verifiers.

    Auto-injects ``sh.agentscore.identity`` as a vendor capability when ``data``
    carries a resolved operator. Verifiers that recognize the AgentScore namespace
    can parse the ``claims`` extra; vanilla UCP agents see a normal capability.

    Example::

        from agentscore_commerce.identity.ucp import (
            UCPServiceBinding,
            UCPSigningKey,
            UCPPaymentHandlerBinding,
            build_ucp_profile,
        )

        @app.get("/.well-known/ucp")
        async def ucp_profile():
            result = await client.acheck(identity)
            return build_ucp_profile(
                services={
                    "dev.ucp.shopping": [
                        UCPServiceBinding(
                            version="2026-04-08",
                            spec="https://ucp.dev/2026-04-08/specification/overview",
                            transport="mcp",
                            endpoint="https://merchant.example/api/ucp/mcp",
                            schema="https://ucp.dev/services/shopping/openrpc.json",
                        ),
                    ],
                },
                signing_keys=[UCPSigningKey.from_jwk(public_jwk)],
                payment_handlers={
                    "sh.agentscore.payment.tempo": [
                        UCPPaymentHandlerBinding(
                            id="tempo",
                            version="2026-04-08",
                            spec="https://agentscore.sh/specification/payment-handlers/tempo",
                            schema="https://agentscore.sh/schemas/payment-handlers/tempo.json",
                            config={"recipient": TEMPO_ADDR},
                        ),
                    ],
                },
                name="Example Merchant",
                data=result,
            ).to_dict()
    """
    services = services if services is not None else {}
    signing_keys = signing_keys if signing_keys is not None else []

    # Deep-copy the capabilities map so we can safely mutate (auto-inject the
    # AgentScore identity capability) without altering the caller's input.
    base_capabilities: dict[str, list[UCPCapabilityBinding]] = {
        k: list(bindings) for k, bindings in (capabilities or {}).items()
    }

    if data is not None and data.resolved_operator:
        # Read typed AssessResult fields first (canonical path). Fall back to
        # ``data.raw["operator_verification"]`` / ``data.raw["account_verification"]``
        # only when the typed field is ``None`` (Python-only legacy escape hatch
        # for callers who hand-construct ``AssessResult(raw=..., typed=None)``).
        # Node has no raw fallback at all.
        typed_op = data.operator_verification
        operator_verification: dict[str, Any]
        if typed_op is None:
            raw = data.raw or {}
            raw_op = raw.get("operator_verification") if isinstance(raw, dict) else None
            operator_verification = raw_op if isinstance(raw_op, dict) else {}
        elif isinstance(typed_op, dict):
            operator_verification = cast("dict[str, Any]", typed_op)
        else:
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
        # matching the node sibling's `||` semantics.
        claims = {
            "operator_id": data.resolved_operator,
            "kyc_level": account_verification.get("kyc_level") or operator_verification.get("level") or "none",
            "sanctions_clear": account_verification.get("sanctions_clear") is True,
            "age_bracket": account_verification.get("age_bracket") or "unknown",
            "jurisdiction": account_verification.get("jurisdiction") or "",
            "verified_at": account_verification.get("verified_at") or operator_verification.get("verified_at") or None,
            "verify_url": data.verify_url,
            "issuer": "https://agentscore.sh",
        }
        # Multi-parent extension matching Shopify's `dev.shopify.catalog.storefront`
        # and UCP-canonical `dev.ucp.shopping.discount` (extends [checkout, cart]).
        # `claims` lives in `extras` so it serializes as a vendor field on the binding.
        binding = UCPCapabilityBinding(
            version=_AGENTSCORE_CAPABILITY_VERSION,
            spec=agentscore_spec_url or _AGENTSCORE_DEFAULT_SPEC_URL,
            schema=agentscore_schema_url or _AGENTSCORE_DEFAULT_SCHEMA_URL,
            extends=["dev.ucp.shopping.checkout", "dev.ucp.shopping.cart"],
            extras={"claims": claims},
        )
        if AGENTSCORE_UCP_CAPABILITY in base_capabilities:
            base_capabilities[AGENTSCORE_UCP_CAPABILITY].append(binding)
        else:
            base_capabilities[AGENTSCORE_UCP_CAPABILITY] = [binding]

    body = UCPProfileBody(
        version=version,
        services=services,
        capabilities=base_capabilities,
        payment_handlers=payment_handlers if payment_handlers is not None else {},
        name=name,
        supported_versions=supported_versions,
        extras=ucp_extras or {},
    )

    return UCPProfile(
        ucp=body,
        signing_keys=signing_keys,
        extras=extras or {},
    )


__all__ = [
    "AGENTSCORE_UCP_CAPABILITY",
    "UCPCapabilityBinding",
    "UCPPaymentHandlerBinding",
    "UCPProfile",
    "UCPProfileBody",
    "UCPServiceBinding",
    "UCPSigningKey",
    "build_ucp_profile",
]
