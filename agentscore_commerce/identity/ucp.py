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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentscore_commerce.identity.types import AssessResult

_DEFAULT_VERSION = "2026-04-17"
_SPEC_URL = "https://ucp.dev/"
AGENTSCORE_UCP_CAPABILITY = "agentscore-identity"
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

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kid": self.kid, "kty": self.kty}
        if self.alg is not None:
            out["alg"] = self.alg
        if self.use is not None:
            out["use"] = self.use
        if self.crv is not None:
            out["crv"] = self.crv
        out.update(self.extras)
        return out


@dataclass
class UCPService:
    """Transport binding entry."""

    type: str
    url: str | None = None
    version: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.url is not None:
            out["url"] = self.url
        if self.version is not None:
            out["version"] = self.version
        out.update(self.extras)
        return out


@dataclass
class UCPCapability:
    """Capability entry — name + schema URL + version + claims."""

    name: str
    schema: str | None = None
    version: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.schema is not None:
            out["schema"] = self.schema
        if self.version is not None:
            out["version"] = self.version
        out.update(self.extras)
        return out


@dataclass
class UCPPaymentHandler:
    """Payment handler entry — name + config."""

    name: str
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
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
        out.update(self.extras)
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
    ``agentscore-identity`` capability when ``data`` carries a resolved operator.
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
                name="Martin Estate",
                services=[UCPService(type="rest", url="https://agents.martinestate.com")],
                payment_handlers=[
                    UCPPaymentHandler(name="tempo", config={"recipient": TEMPO_ADDR}),
                    UCPPaymentHandler(name="stripe", config={"profile_id": STRIPE_PROFILE_ID}),
                ],
                signing_keys=[
                    UCPSigningKey(kid="me-2026-04", kty="EC", alg="ES256", crv="P-256",
                                  extras={"x": "...", "y": "..."}),
                ],
                data=result,
            ).to_dict()
    """
    base_capabilities = list(capabilities or [])

    if data is not None and data.resolved_operator:
        raw = data.raw or {}
        operator_verification = raw.get("operator_verification") if isinstance(raw, dict) else None
        account_verification = raw.get("account_verification") if isinstance(raw, dict) else None
        if not isinstance(operator_verification, dict):
            operator_verification = {}
        if not isinstance(account_verification, dict):
            account_verification = {}
        claims = {
            "operator_id": data.resolved_operator,
            "kyc_level": account_verification.get("kyc_level") or operator_verification.get("level") or "none",
            "sanctions_clear": account_verification.get("sanctions_clear") is True,
            "age_bracket": account_verification.get("age_bracket", "unknown"),
            "jurisdiction": account_verification.get("jurisdiction", ""),
            "verified_at": account_verification.get("verified_at") or operator_verification.get("verified_at"),
            "verify_url": data.verify_url,
            "issuer": "https://agentscore.sh",
        }
        base_capabilities.append(
            UCPCapability(
                name=AGENTSCORE_UCP_CAPABILITY,
                version=_AGENTSCORE_CAPABILITY_VERSION,
                schema=agentscore_schema_url or "https://agentscore.sh/schemas/ucp/agentscore-identity.v1.json",
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
