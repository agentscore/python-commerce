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
from typing import Any, Literal

_DEFAULT_VERSION = "2026-04-08"

# Reverse-DNS namespacing per UCP convention. The bare ``agentscore-identity`` form
# fails the spec regex; vendor-namespacing under the ``sh.agentscore`` authority is
# honest about the capability being our extension, not a UCP-canonical slot.
AGENTSCORE_UCP_CAPABILITY = "sh.agentscore.identity"
"""Capability name AgentScore registers in the UCP profile. Consumers filter on this
to find verified-buyer claims attached to the profile."""

_AGENTSCORE_CAPABILITY_VERSION = "2026-04-08"
_AGENTSCORE_DEFAULT_SPEC_URL = "https://agentscore.sh/specification/identity"
_AGENTSCORE_DEFAULT_SCHEMA_URL = "https://agentscore.sh/schemas/ucp/sh-agentscore-identity-v1.json"


@dataclass
class AgentScoreGatePolicy:
    """Merchant gate policy declared on the UCP profile via ``sh.agentscore.identity``.

    All fields optional; merchant declares which AgentScore checks the gate enforces.
    Snake-case field names match the AgentScore API's ``/v1/assess`` policy contract
    verbatim. No conversion layer between this declaration and what the gate enforces.
    """

    require_kyc: bool | None = None
    """Gate denies if the operator/account behind the agent is not Stripe-Identity-verified."""

    require_sanctions_clear: bool | None = None
    """Gate denies if the operator/account is flagged by OpenSanctions screening."""

    min_age: int | None = None
    """Gate denies if the verified age (from KYC) is below this threshold. Common: 18, 21."""

    allowed_jurisdictions: list[str] | None = None
    """ISO-3166-1 alpha-2 country codes the gate accepts. Mutually exclusive with
    ``blocked_jurisdictions``."""

    blocked_jurisdictions: list[str] | None = None
    """ISO-3166-1 alpha-2 country codes the gate denies. Mutually exclusive with
    ``allowed_jurisdictions``."""

    def to_config(self) -> dict[str, Any]:
        """Serialize as the binding's ``config`` object. Omits unset fields."""
        out: dict[str, Any] = {}
        if self.require_kyc is not None:
            out["require_kyc"] = self.require_kyc
        if self.require_sanctions_clear is not None:
            out["require_sanctions_clear"] = self.require_sanctions_clear
        if self.min_age is not None:
            out["min_age"] = self.min_age
        if self.allowed_jurisdictions is not None:
            out["allowed_jurisdictions"] = self.allowed_jurisdictions
        if self.blocked_jurisdictions is not None:
            out["blocked_jurisdictions"] = self.blocked_jurisdictions
        return out


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
        # Per UCP spec service.json: rest/mcp/a2a transports REQUIRE endpoint;
        # embedded does not. Validate at serialization so a misconfigured profile
        # fails locally instead of being rejected by spec-strict platforms.
        if self.transport in ("rest", "mcp", "a2a") and self.endpoint is None:
            msg = (
                f"UCPServiceBinding(transport={self.transport!r}) requires `endpoint`. "
                "Per UCP spec service.json business_schema, rest/mcp/a2a bindings MUST "
                "carry an endpoint URL."
            )
            raise ValueError(msg)
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
        # Per UCP spec payment_handler.json: available_instruments has minItems:1.
        # Drop the field when empty so a caller passing `[]` doesn't ship an
        # invalid profile.
        if self.available_instruments:
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
    agentscore_gate: AgentScoreGatePolicy | None = None,
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

    Auto-injects ``sh.agentscore.identity`` as a vendor capability extending both
    ``dev.ucp.shopping.checkout`` and ``dev.ucp.shopping.cart`` when
    ``agentscore_gate`` is provided. The capability's ``config`` carries the
    merchant's static gate policy declaration (require_kyc / require_sanctions_clear
    / min_age / allowed_jurisdictions / blocked_jurisdictions). NO per-operator
    data is ever placed on the public profile — per-operator identity attestation
    flows through the AP2 risk-signal endpoint, not here.

    Example::

        from agentscore_commerce.identity import (
            AgentScoreGatePolicy,
            UCPServiceBinding,
            UCPSigningKey,
            UCPPaymentHandlerBinding,
            build_ucp_profile,
        )

        @app.get("/.well-known/ucp")
        async def ucp_profile():
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
                agentscore_gate=AgentScoreGatePolicy(
                    require_kyc=True, min_age=21, allowed_jurisdictions=["US"],
                ),
            ).to_dict()
    """
    services = services if services is not None else {}
    signing_keys = signing_keys if signing_keys is not None else []

    # Deep-copy the capabilities map so we can safely mutate (auto-inject the
    # AgentScore identity capability) without altering the caller's input.
    base_capabilities: dict[str, list[UCPCapabilityBinding]] = {
        k: list(bindings) for k, bindings in (capabilities or {}).items()
    }

    # Auto-inject `sh.agentscore.identity` capability when the merchant declares a gate
    # policy. Static merchant-policy declaration only — no per-operator data on the public
    # profile. Per-operator identity attestation flows through the AP2 risk-signal endpoint
    # or per-request 4xx response bodies, not here. Multi-parent extension matching
    # Shopify's `dev.shopify.catalog.storefront` and UCP-canonical
    # `dev.ucp.shopping.discount` (extends [checkout, cart]).
    if agentscore_gate is not None:
        binding = UCPCapabilityBinding(
            version=_AGENTSCORE_CAPABILITY_VERSION,
            spec=agentscore_spec_url or _AGENTSCORE_DEFAULT_SPEC_URL,
            schema=agentscore_schema_url or _AGENTSCORE_DEFAULT_SCHEMA_URL,
            extends=["dev.ucp.shopping.checkout", "dev.ucp.shopping.cart"],
            config=agentscore_gate.to_config(),
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
    "AgentScoreGatePolicy",
    "UCPCapabilityBinding",
    "UCPPaymentHandlerBinding",
    "UCPProfile",
    "UCPProfileBody",
    "UCPServiceBinding",
    "UCPSigningKey",
    "build_ucp_profile",
]
