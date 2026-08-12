"""High-level Checkout orchestrator; composes 402-emit + verify+settle.

The Checkout primitive collapses the agent-commerce dance (emit 402 →
verify+settle on retry → respond) into a single ``await
checkout.handle(request)`` call. It services every merchant shape:

* **Goods sellers** wire inventory hooks (``on_settled`` persists the order;
  ``mint_recipients`` mints per-order Stripe-multichain addresses).
* **API sellers** wire per-call billing (``compute_pricing`` returns a fixed
  amount; ``on_settled`` returns the inline API response body).
* **Self-custody-only merchants** configure chain rails (Tempo / Base / Solana)
  via ``X402BaseRailSpec`` / ``TempoRailSpec`` / ``SolanaMppRailSpec``.
* **Custodial-only merchants** configure ``StripeRailSpec`` and skip the chain
  rails; Stripe SPT settles via the same ``compose_mppx`` hook.
* **Multi-rail merchants** configure all of the above; the agent picks the rail.

Three flexibility axes; every combination is supported:

* **x402 only / MPP only / both**; Checkout works with ``x402_server`` alone,
  ``compose_mppx`` alone, or both. Whichever payment header arrives is dispatched
  to the configured handler; the other path is simply absent.
* **Self-custody / Stripe / mixed**; rails dict is the single source of truth.
  Listing ``StripeRailSpec`` makes Stripe SPT an acceptable rail; omitting it
  makes the merchant chain-only. Mixing freely is the default.
* **Gated / ungated identity**; ``CheckoutRequest.assess`` is optional. Merchants
  who run :class:`AgentScoreGate` upstream pass its result through; merchants
  running anonymous (per-call API, public discovery) leave it ``None``.

Domain-neutral by design: every per-request value is keyed by
``reference_id`` (a UUID minted on first contact). Goods merchants persist
this as their order id; API merchants treat it as a per-call request id.

Usage (goods seller, full agent-commerce flow)::

    checkout = Checkout(
        rails={
            "tempo": TempoRailSpec(recipient=...),
            "x402_base": X402BaseRailSpec(recipient=...),
            "stripe": StripeRailSpec(profile_id=...),
        },
        url=APP_URL,
        compute_pricing=lambda ctx: PricingResult(amount_usd=cart_total(ctx.body)),
        mint_recipients=lambda ctx: stripe_multichain_addresses_for(ctx.amount_usd),
        on_settled=lambda ctx, outcome: persist_order(ctx.reference_id, ctx.body, outcome),
        compose_mppx=lambda ctx: mppx_compose(mppx, ctx.request),
        x402_server=x402,
        x402_base_network="eip155:8453",
    )

Usage (API seller, per-call billing with inline response)::

    checkout = Checkout(
        rails={"x402_base": X402BaseRailSpec(recipient=TREASURY, mode="exact")},
        url=APP_URL,
        compute_pricing=lambda ctx: PricingResult(amount_usd=0.01),
        on_settled=lambda ctx, outcome: {"data": await run_api_call(ctx.body)},
        x402_server=x402,
        x402_base_network="eip155:8453",
        # compose_mppx omitted; x402-only API merchants don't need MPP rails
    )

``handle(request)`` returns a framework-neutral :class:`CheckoutResult`
(``body`` + ``headers`` + ``status`` + ``reference_id`` + ``settled``); the
merchant wraps it in their framework's response shape.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from agentscore_commerce._headers import normalize_headers_to_lowercase
from agentscore_commerce._mppx_receipt import extract_mppx_receipt_header_from_raw
from agentscore_commerce.aip.jwks import AGENTSCORE_CANONICAL_ISSUER, canonicalize_issuer
from agentscore_commerce.challenge.accepted_methods import build_accepted_methods
from agentscore_commerce.challenge.agent_instructions import RailKey, build_agent_instructions
from agentscore_commerce.challenge.agent_memory import first_encounter_agent_memory
from agentscore_commerce.challenge.body import X402PaymentRequired, build_402_body
from agentscore_commerce.challenge.how_to_pay import build_how_to_pay
from agentscore_commerce.challenge.pricing import PricingBlock, build_pricing_block
from agentscore_commerce.challenge.respond_402 import Respond402Result, respond_402
from agentscore_commerce.challenge.validation_error import build_validation_error
from agentscore_commerce.errors import CheckoutValidationError
from agentscore_commerce.forwarded_proto import apply_forwarded_proto, read_forwarded_proto
from agentscore_commerce.payment.constants import STRIPE_MIN_CHARGE_USD
from agentscore_commerce.payment.mppx_failures import classify_mppx_failure
from agentscore_commerce.payment.payment_header import (
    has_mppx_header,
    has_x402_header,
    malformed_payment_credential,
)
from agentscore_commerce.payment.rail_spec import (
    RecipientLike,
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    TempoSessionRailSpec,
    X402BaseRailSpec,
)
from agentscore_commerce.payment.x402_settle import (
    ProcessX402SettleSuccess,
    classify_x402_settle_result,
    process_x402_settle,
)
from agentscore_commerce.payment.x402_validation import (
    VerifyX402RequestSuccess,
    verify_x402_request,
)
from agentscore_commerce.payment.zero_settle import zero_amount_carve_out

if TYPE_CHECKING:
    from agentscore_commerce.aip.jwks import JwksCache
    from agentscore_commerce.aip.types import TrustLevel

CheckoutRailSpec: TypeAlias = (
    TempoRailSpec | X402BaseRailSpec | SolanaMppRailSpec | StripeRailSpec | TempoSessionRailSpec
)


def _spec_rail_key(spec: CheckoutRailSpec) -> RailKey:
    """Map a ``*RailSpec`` instance to its canonical :data:`RailKey` slug.

    Tempo charge and Tempo session both speak MPP on Tempo, so they fold to
    ``"tempo_mpp"``.
    """
    if isinstance(spec, (TempoRailSpec, TempoSessionRailSpec)):
        return "tempo_mpp"
    if isinstance(spec, X402BaseRailSpec):
        return "x402_base"
    if isinstance(spec, SolanaMppRailSpec):
        return "solana_mpp"
    return "stripe"  # StripeRailSpec is the only remaining variant in CheckoutRailSpec.


def _spec_method_name(spec: CheckoutRailSpec) -> str:
    """Protocol-shaped method name for the ``methods: [...]`` discovery array."""
    if isinstance(spec, (TempoRailSpec, TempoSessionRailSpec)):
        return "tempo/charge"
    if isinstance(spec, X402BaseRailSpec):
        return "x402/exact (base)"
    if isinstance(spec, SolanaMppRailSpec):
        return "solana/charge"
    return "stripe/spt"  # StripeRailSpec is the only remaining variant in CheckoutRailSpec.


def _realm_from_url(url: str) -> str:
    """Derive the WWW-Authenticate ``realm`` from the checkout endpoint URL.

    The realm identifies the protection space and, by convention (and to match the Node
    SDK, which passes ``new URL(APP_URL).host``), is the bare host, not the full endpoint
    URL. ``Checkout(url="https://agents.example.com/purchase")`` yields realm
    ``agents.example.com``. Falls back to the input unchanged when it has no parseable host
    (e.g. already a bare host, or a relative path).
    """
    from urllib.parse import urlparse

    return urlparse(url).netloc or url


@dataclass
class CheckoutRequest:
    """Framework-neutral HTTP request input to :meth:`Checkout.handle`.

    Merchants build this from their framework's request object once; the
    Checkout layer then runs the same flow regardless of FastAPI / Flask /
    Django / aiohttp / Sanic.
    """

    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    """Parsed JSON body. For non-JSON endpoints, pass ``{}`` and stash the raw bytes elsewhere."""
    assess: dict[str, Any] | None = None
    """Optional assess block from the gate (operator/wallet identity, signer verdicts).

    When present, hooks can branch on identity (e.g. KYC-only pricing). When absent,
    the merchant is either running pre-gate (anonymous discovery) or chose to skip
    the gate for this endpoint.
    """
    raw: Any = None
    """Optional escape hatch for the framework's native request object. Pass when
    your ``compose_mppx`` hook needs to call ``mppx.compose(...)(raw_request)`` ;
    pympp's compose binds to the raw HTTP request, so the orchestrator forwards
    this through unchanged."""


@dataclass
class DiscoveryProbeConfig:
    """Auto-route discovery probes inside :meth:`Checkout.handle`.

    When set on the Checkout, an empty-body POST without any payment header
    short-circuits to a sample 402 advertising the merchant's discovery shape
    for crawlers (``awal x402 details``, x402-proxy, x402scan, ...). The
    probe DOES NOT settle anything; it's an SEO-shaped advertisement.

    Per-rail real-recipient discovery still happens via the regular 402 emit
    path on a non-probe request. Sample data here is intentionally minimal
    (single rail, single recipient) since crawlers only need the shape.
    """

    realm: str
    sample_rail: str
    sample_amount_usd: float
    sample_recipient: str
    intent: str = "charge"
    ttl_seconds: int = 300
    docs_url: str | None = None
    message: str | None = None
    x402_sample: Any = None  # X402SampleProbe, optional


@dataclass
class PricingResult:
    """Output of :attr:`Checkout.compute_pricing`; per-request pricing."""

    amount_usd: float
    """Total to charge in USD (or the upper bound, for ``mode="upto"`` rails)."""
    currency: str = "USD"
    block: PricingBlock | None = None
    """Optional pre-built :class:`PricingBlock`. When omitted, Checkout builds a minimal
    block from ``amount_usd`` so the 402 body always carries pricing metadata."""
    decimals: int = 2
    """Dollar-precision used to format ``amount_usd`` and the derived
    :class:`PricingBlock` fields. Default ``2`` (canonical USD cents). Raise for
    sub-cent unit pricing (per-token LLM, per-byte storage, etc.) so the 402
    body advertises the real amount instead of rounding to two decimals."""
    product: dict[str, str] | None = None
    """Optional product block surfaced in the 402 body's ``product`` field. Goods
    merchants populate ``{id, name, slug, list_price_usd, ...}``; API sellers leave
    this ``None`` since per-call billing has no product concept."""
    body_extras: dict[str, Any] | None = None
    """Optional merchant-specific fields merged into the 402 body alongside the
    standard ``accepted_methods`` / ``agent_instructions`` / ``pricing`` blocks.
    Useful for ``redemption_code_applied``, coupon hints, or any other field the
    merchant wants the agent to see in the challenge body."""


def pricing_result(
    *,
    subtotal_cents: float | None = None,
    tax_cents: float | None = None,
    shipping_cents: float | None = None,
    discount_cents: float | None = None,
    tax_rate: float | None = None,
    tax_state: str | None = None,
    currency: str = "USD",
    amount_usd: float | None = None,
    decimals: int = 2,
    product: dict[str, str] | None = None,
    body_extras: dict[str, Any] | None = None,
) -> PricingResult:
    """Build a :class:`PricingResult` from cents-denominated inputs.

    Saves the ``PricingResult(amount_usd=..., block=build_pricing_block(...))``
    dance every US-commerce merchant repeats. When ``subtotal_cents`` is set:

    * ``subtotal_cents`` is the list price (pre-discount). ``discount_cents``
      is the deduction applied (redemption code / coupon / promo).
    * ``amount_usd`` is derived from
      ``(subtotal + tax + shipping - discount) / 100`` (floored at 0) unless
      explicitly provided.
    * A :class:`PricingBlock` is built via :func:`build_pricing_block` and
      attached to the result's ``block`` field. ``discount`` is surfaced as a
      dollar-string when ``discount_cents`` is supplied.

    When ``subtotal_cents`` is omitted, the function passes through to the
    raw :class:`PricingResult` constructor; ``amount_usd`` is then required.

    Use this in ``compute_pricing`` hooks instead of hand-rolling::

        async def _compute_pricing(ctx: CheckoutContext) -> PricingResult:
            return pricing_result(
                subtotal_cents=25000,
                tax_cents=2000,
                tax_rate=0.08,
                tax_state="CA",
            )

        # Redemption-code applied (free order, agent sees the savings line):
        return pricing_result(subtotal_cents=7500, discount_cents=7500)
    """
    from agentscore_commerce.challenge import build_pricing_block

    if subtotal_cents is not None:
        gross_cents = subtotal_cents + (tax_cents or 0) + (shipping_cents or 0) - (discount_cents or 0)
        total_cents = max(0, gross_cents)
        derived_amount = total_cents / 100 if amount_usd is None else amount_usd
        block = build_pricing_block(
            subtotal_cents=subtotal_cents,
            tax_cents=tax_cents or 0,
            shipping_cents=shipping_cents,
            discount_cents=discount_cents,
            tax_rate=tax_rate,
            tax_state=tax_state,
            currency=currency,
            decimals=decimals,
        )
        return PricingResult(
            amount_usd=derived_amount,
            currency=currency,
            block=block,
            decimals=decimals,
            product=product,
            body_extras=body_extras,
        )
    if amount_usd is None:
        msg = "pricing_result requires either `subtotal_cents` or `amount_usd`."
        raise ValueError(msg)
    return PricingResult(
        amount_usd=amount_usd,
        currency=currency,
        decimals=decimals,
        product=product,
        body_extras=body_extras,
    )


@dataclass
class CheckoutContext:
    """In-flight state passed to every hook in the Checkout flow."""

    request: CheckoutRequest
    reference_id: str
    """UUID minted on first contact. Goods merchants persist as order id; API merchants
    treat as request id."""
    pricing: PricingResult | None = None
    """Set after :attr:`Checkout.compute_pricing` runs; ``None`` before."""
    recipients: dict[str, str] = field(default_factory=dict)
    """rail-key → recipient address, after :attr:`Checkout.mint_recipients` runs (if
    provided). Static rails (treasury-funded) inherit recipients from the RailSpec."""
    state: dict[str, Any] = field(default_factory=dict)
    """Merchant-supplied per-request state, populated by :attr:`Checkout.pre_validate`.
    Other hooks read from here (e.g. ``ctx.state["product"]`` after pre_validate
    resolved it). Stays empty when no pre_validate is configured."""
    operator_handle: str | None = None
    """Stable pairwise handle for the ACCOUNT behind this request's operator token.

    Set by Checkout's internal gate from the same ``/v1/assess`` response it already
    fetched, so it costs no extra round trip and nothing extra against quota.

    This is what durable merchant state should key on, prepaid balances above all: it
    survives the token rotating, expiring or being revoked, whereas state keyed on the token
    instance is stranded every time one rotates. ``None`` when no gate is configured, on
    wallet or AIT paths, on anonymous discovery legs, or when the API has no handle salt.
    """
    capture_wallet: Callable[..., Any] | None = None
    """Capture the signer wallet under the operator credential the gate resolved
    for this request. Set by Checkout's internal gate after a successful allow when
    an ``operator_token`` is present; ``None`` for wallet-authenticated requests
    (no operator_token to associate) or anonymous discovery legs.
    Fire-and-forget — invoke from ``on_settled`` with the recovered signer:
    ``await ctx.capture_wallet(wallet_address=..., network=..., idempotency_key=...)``.
    """

    @property
    def identity_status(self) -> str:
        """Read the gate's identity verdict out of ``request.assess``.

        Returns ``"verified"`` / ``"unverified"`` / ``"anonymous"`` / ``"denied"``.
        Defaults to ``"anonymous"`` when no gate ran for this request.
        """
        assess = self.request.assess or {}
        value = assess.get("identity_status")
        return value if isinstance(value, str) else "anonymous"


def get_identity_status(ctx: CheckoutContext) -> str:
    """Function form of :attr:`CheckoutContext.identity_status`.

    Returns ``"verified"`` / ``"unverified"`` / ``"anonymous"`` / ``"denied"``.
    """
    return ctx.identity_status


@dataclass
class AipIssuerPolicy:
    """Per-issuer compliance policy block for :attr:`AipGateConfig.issuer_policies`.

    The same compliance fields as the gate, applied (as a whole-policy *replacement*, not a
    merge) only to AITs from the matching issuer. An override of
    ``AipIssuerPolicy(require_kyc=True, min_age=21)`` evaluates ONLY those two rules for that
    issuer (sanctions / jurisdiction omitted -> not enforced for that issuer).
    """

    require_kyc: bool | None = None
    require_sanctions_clear: bool | None = None
    min_age: int | None = None
    blocked_jurisdictions: list[str] | None = None
    allowed_jurisdictions: list[str] | None = None


@dataclass
class AipGateConfig:
    """AIP acceptance config for :attr:`CheckoutGateConfig.aip`.

    When set and a settle-leg request carries an ``Agent-Identity`` header, the gate verifies
    the AIT offline (issuer signature via the trusted-issuer JWKS + RFC 9421
    proof-of-possession) BEFORE the assess call, then forwards the raw token to ``/v1/assess``
    as ``aip_token`` so the same KYC / age / sanctions / jurisdiction policy evaluates against
    the token's attested identity. A present-but-invalid AIT is a hard deny (the gate does NOT
    fall through to wallet / operator-token). Requests with no ``Agent-Identity`` header use the
    existing wallet / operator-token path unchanged.
    """

    trusted_issuers: list[str] | None = None
    """ADDITIONAL external issuers to trust beyond AgentScore's own (e.g.
    ``["https://issuer.example"]``), matched after canonicalization. AgentScore's canonical
    issuer (:data:`AGENTSCORE_CANONICAL_ISSUER`) is ALWAYS trusted and never needs listing.
    Omit / empty to accept only AgentScore-issued AITs."""
    max_skew_seconds: float | None = None
    """Clock-skew tolerance in seconds for the RFC 9421 signature window (and, as an override,
    the AIT ``exp`` / ``iat``). Defaults to 60s for both when unset."""
    authority: str | None = None
    """Expected ``@authority`` (public hostname) the RFC 9421 signature must cover. When set,
    the verifier binds the signature to this value instead of trusting the inbound ``Host``
    header -- pin it to your real public host when behind a proxy that does not normalize
    ``Host``, to prevent a captured AIT+signature from being replayed to a different virtual
    host on the same origin."""
    require_trust_level: TrustLevel | None = None
    """Minimum ``trust_level`` an AIT must assert (autonomous < human_present <
    human_confirmed) -- the spec's human-presence gate. Enforced at the edge from the verified
    token; insufficient -> 403 weak_auth with ``required_trust_level``. Unset = any trust level
    accepted."""
    require_amr: list[str] | None = None
    """Acceptable ``auth.amr`` methods (RFC 8176); the AIT must carry at least one (e.g.
    ``["face", "fpt", "hwk"]`` to require strong human auth). Insufficient -> 403 weak_auth with
    ``required_amr``. Unset = not enforced."""
    issuer_policies: dict[str, AipIssuerPolicy] | None = None
    """Per-issuer compliance policy override, keyed by issuer URL (canonicalized before
    lookup). When a request's AIT is verified and its ``iss`` matches a key here, that block
    REPLACES the gate's default policy fields for that request -- letting a merchant apply
    different rules by issuer (e.g. full compliance for its own AITs, a relaxed set for a
    partner issuer). The replacement is whole-policy, not a merge. Issuers NOT listed use the
    gate's default policy unchanged. Only the AIT path consults this -- wallet / operator-token
    requests are unaffected."""


def _aip_trusted_issuer_set(cfg: AipGateConfig) -> list[str]:
    """The effective trusted-issuer list for an :class:`AipGateConfig` (canonical + externals)."""
    return build_aip_trusted_issuers(cfg.trusted_issuers)


def _aip_required_claims(policy: AipIssuerPolicy) -> list[str]:
    """Project the gate's effective compliance policy onto the AIT identity claims it requires.

    For the ``required_claims`` escalation hint on an ``insufficient_claims`` AIP denial. Mirrors
    the claim names the API checks an AIT against (``id_verified`` / ``sanctions_clear`` /
    ``age_over_<N>`` / ``jurisdiction``). Empty when the policy is identity-only.
    """
    claims: list[str] = []
    if policy.require_kyc:
        claims.append("id_verified")
    if policy.require_sanctions_clear:
        claims.append("sanctions_clear")
    if policy.min_age is not None:
        claims.append(f"age_over_{policy.min_age}")
    if policy.blocked_jurisdictions is not None or policy.allowed_jurisdictions is not None:
        claims.append("jurisdiction")
    return claims


def _resolve_issuer_policy(
    issuer_policies: dict[str, AipIssuerPolicy],
    iss: str,
) -> AipIssuerPolicy | None:
    """Resolve the per-issuer policy override for ``iss``, matched on the canonical issuer.

    Keys are canonicalized before comparison so a trailing-slash key still applies. Returns
    ``None`` when no key canonicalizes to ``iss``.
    """
    target = canonicalize_issuer(iss)
    if target is None:
        return None
    for key, policy in issuer_policies.items():
        if canonicalize_issuer(key) == target:
            return policy
    return None


@dataclass
class CheckoutGateConfig:
    """Optional gate configuration for :class:`Checkout`.

    When set, Checkout runs the AgentScore identity gate on the settle leg (no
    header → 402 emit only) and surfaces ``identity_status`` to hooks via
    ``ctx.assess``.

    The gate flow has three customization seams:

    1. ``run_gate`` — full escape hatch. Replaces the SDK's gate flow entirely.
       Used by merchants with custom auth (e.g. enterprise SSO bridges) who
       need full control. Other fields are ignored when set.
    2. ``per_request_policy`` — reads ``ctx.state`` (populated by pre_validate)
       and returns a dict that overrides static gate policy fields per request.
       Goods merchants resolve per-product compliance from this.
    3. ``on_denied`` — invoked AFTER the SDK builds the canonical DenialReason.
       Returns a custom denial body shape, or ``None`` to keep the canonical body.

    ``create_session_on_missing`` auto-mints a verification session when no
    identity is present and returns 403 with verify_url + poll instructions
    instead of a bare ``missing_identity`` denial. Pass an explicit
    :class:`CreateSessionOnMissing` to customize ``get_session_options`` /
    ``on_before_session`` hooks; omit and Checkout auto-builds one from
    ``api_key`` + ``base_url`` + ``context`` + ``merchant_name``.
    """

    api_key: str
    """AgentScore API key. Required when ``run_gate`` is omitted."""
    base_url: str = "https://api.agentscore.com"
    """AgentScore API base URL. Override for self-hosted / staging deployments."""
    merchant_name: str | None = None
    """Surfaced on auto-minted verification sessions (``product_name`` field) so
    agents see the merchant they were paying when they hit the verify URL."""
    user_agent: str | None = None
    """Optional User-Agent string prepended to the SDK's default. Useful for
    per-merchant telemetry."""
    context: str = "checkout"
    """Session context label minted on auto-session creation."""
    require_kyc: bool | None = None
    """Require ``kyc_status == 'verified'`` on the resolved account."""
    require_sanctions_clear: bool | None = None
    """Require ``sanctions_status == 'clear'`` on the resolved account."""
    min_age: int | None = None
    """Minimum age in years; reads ``age_bracket`` from account verification."""
    blocked_jurisdictions: list[str] | None = None
    """ISO-3166 alpha-2 list. Deny when the resolved jurisdiction matches."""
    allowed_jurisdictions: list[str] | None = None
    """ISO-3166 alpha-2 list. Deny when the resolved jurisdiction is NOT in the list."""
    fail_open: bool = False
    """When True, 429 / 5xx / timeouts pass through as ``allow`` (with
    ``degraded=True`` on ctx.assess); compliance denials still deny."""
    cache_seconds: int = 300
    """TTL for the per-identity assess cache. Default 5 minutes."""
    chain: str | None = None
    """Default chain hint passed to /v1/assess (CAIP-2)."""
    create_session_on_missing: Any | None = None
    """Optional :class:`CreateSessionOnMissing`. When set, missing-identity denials
    auto-mint a session and return 403 with ``verify_url`` + poll instructions.
    When omitted, Checkout builds a default config from ``api_key`` + ``base_url``
    + ``context`` + ``merchant_name``."""
    per_request_policy: Callable[[CheckoutContext], Any] | None = None
    """Per-request policy override hook. Receives the CheckoutContext (with
    ``ctx.state`` populated by pre_validate); returns a dict merged over the
    static policy fields. Return ``None`` to skip the gate entirely for that
    request."""
    on_denied: Callable[[CheckoutContext, Any], Any] | None = None
    """Optional callback invoked AFTER the SDK builds the canonical DenialReason.
    Receives ``(ctx, denial_reason)``; returns a dict with ``{status, body,
    headers?}`` to override the canonical body, or ``None`` to keep it. Use this
    to map gate denial codes to merchant-specific body shapes."""
    aip: AipGateConfig | None = None
    """Accept AIP Agent Identity Tokens (AITs) on this route. When set and a request carries an
    ``Agent-Identity`` header, the gate verifies the token offline (issuer signature via the
    trusted-issuer JWKS + RFC 9421 proof-of-possession) BEFORE the assess call, then sends the
    raw token to ``/v1/assess`` as ``aip_token`` so the same KYC / age / sanctions / jurisdiction
    policy evaluates against the token's attested identity. A present-but-invalid AIT is a hard
    deny (the gate does NOT fall through to wallet / operator-token). Requests with no
    ``Agent-Identity`` header use the existing wallet / operator-token path unchanged.

    Ignored when ``run_gate`` is also set (a custom gate fully owns the flow). Without an
    ``api_key``, a verified AIT is honored offline for identity-only gates, but a gate that
    declares policy fields (KYC / age / sanctions / jurisdiction) without an ``api_key`` fails
    closed (``aip_policy_requires_api_key``) since policy can only be evaluated via
    ``/v1/assess``."""
    run_gate: Callable[[CheckoutContext], Any] | None = None
    """Full escape hatch. When set, replaces the SDK's gate flow entirely. Other
    fields above are ignored. Returns ``None`` on allow, or a dict with
    ``{status, body, headers?}`` on denial. Used by merchants with custom auth
    bridges (enterprise SSO) who need full control."""


@dataclass
class SettleOutcome:
    """Surface passed to :attr:`Checkout.on_settled` after a payment lands.

    Normalized fields (``tx_hash`` / ``signer_address`` / ``signer_network``) are
    extracted by Checkout from the underlying settle result so merchants don't
    need to know that x402's raw is a Pydantic ``SettleResponse`` with
    ``.transaction`` while MPP's raw is a ``{credential, receipt}`` dict with
    the signer hidden inside ``credential.source``. Read these directly.
    """

    rail: Literal["x402", "mpp"]
    """Which protocol settled. ``"mpp"`` covers tempo / tempo-session / solana / stripe-spt."""
    rail_key: str = ""
    """The merchant's rails-dict key that handled this settle (e.g. ``"x402_base"``,
    ``"tempo"``, ``"stripe"``). Read this directly in ``on_settled`` to label the
    rail however the merchant persists it; saves the ``"x402" → "x402-base"``
    translation."""
    tx_hash: str | None = None
    """On-chain transaction hash when the rail settled to chain. ``None`` for $0
    carve-outs, Stripe SPT, and pre-pympp-SessionIntent tempo sessions."""
    signer_address: str | None = None
    """Wallet that signed the payment credential. Normalized (EVM lowercased,
    Solana base58 preserved). ``None`` for rails without a signer (Stripe SPT)."""
    signer_network: str | None = None
    """``"evm"`` / ``"solana"`` for chain signers; ``None`` otherwise."""
    payment_response_header: str | None = None
    """The ``PAYMENT-RESPONSE`` header to echo (x402 success path). ``None`` for MPP."""
    payment_receipt_header: str | None = None
    """The ``Payment-Receipt`` header to echo (MPP success path, paymentauth.org §5).
    ``None`` for x402 and for the MPP zero-settle carve-out (no receipt minted)."""
    raw: Any = None
    """The underlying settle result. Inspect for power-user fields (facilitator
    diagnostics, raw receipt blobs); prefer the normalized fields above for the
    common case."""


@dataclass
class MppxComposeOutcome:
    """Result a ``compose_mppx`` hook returns when handling an MPP credential.

    ``status=200`` means pympp validated the ``Authorization: Payment`` credential
    and the settlement landed; Checkout runs ``on_settled`` and returns success.

    ``status=402`` means pympp emitted a 402 (no credential / invalid credential).
    Checkout layers its rich body on top of pympp's WWW-Authenticate header and
    optional x402 PAYMENT-REQUIRED, returning the composed 402.

    On ``status=200``, return ``tx_hash`` / ``signer_address`` / ``signer_network``
    so they flow through to ``SettleOutcome`` without merchants having to
    destructure ``raw`` per pympp version. The canonical hook
    :func:`make_mppx_compose_hook` populates these for tempo MPP.
    """

    status: Literal[200, 402]
    headers: dict[str, str] = field(default_factory=dict)
    """For ``status=402``: the WWW-Authenticate (+ any other) headers pympp's
    compose emitted. Checkout merges these into the final 402 response."""
    rail_key: str = "tempo"
    """For ``status=200``: which merchant rails-dict key handled this settle.
    Defaults to ``"tempo"`` (most common MPP rail); override for Stripe SPT or
    Solana MPP. Surfaced verbatim on :attr:`SettleOutcome.rail_key`."""
    tx_hash: str | None = None
    """For ``status=200``: on-chain tx hash from the pympp Receipt (when settled
    to chain). ``None`` for $0 carve-outs and Stripe SPT."""
    signer_address: str | None = None
    """For ``status=200``: wallet that signed the MPP credential, normalized."""
    signer_network: str | None = None
    """For ``status=200``: ``"evm"`` / ``"solana"`` depending on the rail."""
    payment_response_header: str | None = None
    """For ``status=200``: optional PAYMENT-RESPONSE header echoed to the agent."""
    payment_receipt_header: str | None = None
    """For ``status=200``: serialized ``Payment-Receipt`` header (base64url-encoded
    receipt struct per pympp's ``Receipt.to_payment_receipt``). Echoed to the agent
    so spec-strict MPP clients (tempo CLI, etc.) can lift tx_hash + source from
    headers without parsing the JSON body."""
    raw: Any = None
    """The underlying pympp compose result for ``on_settled`` introspection."""
    failure_reason: str | None = None
    """For ``status=402``: optional reason string captured from the swallowed
    inner verifier exception (e.g. ``"keychain validation failed: KeyNotFound"``
    when a Tempo signer isn't enrolled). When set, ``_handle_mppx`` runs
    :func:`classify_mppx_failure` and returns a typed envelope
    (``tempo_key_not_registered``, etc.) instead of the generic
    ``payment_proof_invalid``. Populated by :func:`make_mppx_compose_hook` when
    pympp's verifier raises; custom hooks can set it explicitly to opt in."""


@dataclass
class CheckoutResult:
    """Framework-neutral output of :meth:`Checkout.handle`."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str]
    reference_id: str
    settled: bool = False
    settle_phase: str | None = None
    """``None`` on settlement success; otherwise the failure phase (``"verify_failed"``,
    ``"settle_failed"``, ...) for diagnostics."""


PreValidateFn: TypeAlias = Callable[
    [CheckoutContext],
    "Awaitable[dict[str, Any] | None] | dict[str, Any] | None",
]
PricingFn: TypeAlias = Callable[[CheckoutContext], Awaitable[PricingResult] | PricingResult]
RecipientsFn: TypeAlias = Callable[[CheckoutContext], Awaitable[dict[str, str]] | dict[str, str]]
ReferenceIdFn: TypeAlias = Callable[[CheckoutContext], Awaitable[str] | str]
OnSettledFn: TypeAlias = Callable[
    [CheckoutContext, SettleOutcome],
    Awaitable[dict[str, Any] | None] | dict[str, Any] | None,
]
ComposeMppxFn: TypeAlias = Callable[[CheckoutContext], Awaitable[MppxComposeOutcome] | MppxComposeOutcome]
IsCachedAddressFn: TypeAlias = Callable[[str], Awaitable[bool] | bool]


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _resolve_resource_url(request: CheckoutRequest) -> str:
    """Resource URL for the x402 402, scheme-corrected for TLS-terminating edge proxies.

    Behind ALB / CloudFront the inbound ``request.url`` is ``http://``; x402 discovery
    requires ``https://``, so honor ``X-Forwarded-Proto`` (the proxy's original scheme).
    """
    return apply_forwarded_proto(request.url, read_forwarded_proto(request.headers))


def _resolve_identity_metadata(ctx: CheckoutContext) -> dict[str, Any] | None:
    """Compose the identity_metadata block from request + assess state.

    Wallet-mode merchants get ``required_signer`` + ``linked_wallets`` +
    ``signer_constraint`` pre-advertised on the 402, so agents self-correct at
    discovery instead of at the 403 retry. Returns ``None`` when the request
    shows no wallet intent (operator-token only); the 402 then omits the block
    entirely.
    """
    from agentscore_commerce.challenge.identity import build_identity_metadata

    lower = normalize_headers_to_lowercase(ctx.request.headers)
    wallet = lower.get("x-wallet-address")
    if not wallet:
        return None
    linked_wallets: list[str] | None = None
    assess = ctx.request.assess
    if isinstance(assess, dict):
        identity = assess.get("identity")
        if isinstance(identity, dict):
            lw = identity.get("linked_wallets")
            if isinstance(lw, list) and all(isinstance(x, str) for x in lw):
                linked_wallets = lw
    return build_identity_metadata(mode="wallet", wallet=wallet, linked_wallets=linked_wallets)


def _header_is_payment_credential(orig: Any, key: str) -> bool:
    lk = key.lower()
    if lk in ("payment-signature", "x-payment"):
        return True
    if lk == "authorization":
        value = orig.get("authorization")
        return isinstance(value, str) and value.startswith("Payment ")
    return False


class _StrippedHeaders:
    """Read-only view over a framework ``headers`` mapping that hides credentials.

    Hides the payment-credential headers (``x-payment`` / ``payment-signature`` /
    an ``Authorization: Payment`` value) while supporting the ``.get`` / ``[]`` /
    ``in`` / ``.items`` / iteration access patterns hooks use.
    """

    def __init__(self, orig: Any) -> None:
        self._orig = orig

    def get(self, key: str, default: Any = None) -> Any:
        if _header_is_payment_credential(self._orig, key):
            return default
        return self._orig.get(key, default)

    def __getitem__(self, key: str) -> Any:
        if _header_is_payment_credential(self._orig, key):
            raise KeyError(key)
        return self._orig[key]

    def __contains__(self, key: str) -> bool:
        if _header_is_payment_credential(self._orig, key):
            return False
        return key in self._orig

    def items(self) -> Any:
        return [(k, v) for k, v in self._orig.items() if not _header_is_payment_credential(self._orig, k)]

    def __iter__(self) -> Any:
        return iter(k for k in self._orig if not _header_is_payment_credential(self._orig, k))


class _RawHeaderStripProxy:
    """Wrap the native request so ``.headers`` hides payment credentials.

    Every other attribute (``.json``, ``.scope``, mppx's fetch surface, ...)
    delegates to the original request unchanged.
    """

    def __init__(self, raw: Any, headers: _StrippedHeaders) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "headers", headers)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_raw"), name)


def _strip_payment_headers_from_raw(raw: Any) -> Any:
    """Return ``raw`` with the payment-credential headers hidden.

    Hooks that read the native request (``ctx.request.raw``) on the malformed
    re-challenge then see a discovery leg. Non-header-bearing ``raw`` (or
    ``None``) passes through unchanged.
    """
    if raw is None or not hasattr(raw, "headers"):
        return raw
    return _RawHeaderStripProxy(raw, _StrippedHeaders(raw.headers))


class Checkout:
    """High-level agent-commerce orchestrator.

    Composes :func:`build_accepted_methods`, :func:`build_how_to_pay`,
    :func:`respond_402`, :func:`verify_x402_request`, and
    :func:`process_x402_settle` into a single ``await checkout.handle(request)``
    call. For MPP rails, the merchant supplies a ``compose_mppx`` hook that
    drives pympp's ``compose()`` (intent dispatch is merchant-owned because
    pympp binds intents per instance).

    Required:

    * ``rails``; rail-key → ``*RailSpec``. The same map every other helper
      consumes (:func:`build_accepted_methods`, :func:`build_how_to_pay`,
      :func:`create_mppx_server`).
    * ``url``; absolute URL of the checkout endpoint.
    * ``compute_pricing``; async/sync function ``(ctx) -> PricingResult``.

    Optional:

    * ``x402_server``; built via :func:`create_x402_server`. Pair it with an
      ``X402BaseRailSpec`` in ``rails["x402_base"]``; the CAIP-2 network is
      read from ``rail.network`` (defaults to ``eip155:8453``).
    * ``compose_mppx``; async/sync function ``(ctx) -> MppxComposeOutcome``.
      Required when the merchant accepts ``Authorization: Payment`` credentials
      (Tempo / Solana MPP / Stripe SPT). Omit for x402-only merchants.
    * ``mint_recipients``; async/sync function ``(ctx) -> dict[rail_key, address]``.
      Use for Stripe-multichain merchants who mint per-order deposit addresses.
      When omitted, every rail's recipient is taken from its ``*RailSpec``.
    * ``mint_reference_id``; async/sync function ``(ctx) -> str``. Default is
      :func:`uuid.uuid4`. Goods merchants typically mint an order id here.
    * ``on_settled``; async/sync function ``(ctx, outcome) -> dict | None``. Runs
      after the payment settles successfully. Goods merchants persist the order
      here. API merchants can return the inline API response body; when the hook
      returns a dict, it becomes the 200 response body (with ``reference_id``
      auto-merged).
    * ``is_cached_address``; pass when the merchant mints per-order addresses
      so :func:`verify_x402_request` can confirm the ``payTo`` was minted by
      this merchant. Default permissive (accepts any payTo) for static-treasury
      merchants.
    * ``zero_settle_carve_out``; when ``True`` and ``compute_pricing`` returns
      ``amount_usd=0`` with a payment header attached, Checkout verifies the
      credential, lifts the signer, and fires ``on_settled`` with
      ``tx_hash=None`` instead of attempting an on-chain settle. Coinbase's
      CDP facilitator and pympp's tempo intents both reject $0 settles outright;
      this carve-out makes free-redemption flows work uniformly across rails.
      Default ``False`` (every payment header attempts a real settle).
    * ``credential_pre_check``; reject payment credentials that fail the cheap
      wire-shape check (not base64 JSON, not a token-shaped value) BEFORE any
      merchant hook runs, so junk headers never trigger ``pre_validate`` /
      pricing / recipient minting / the gate's assess call. Shape only —
      signature and payTo verification stay on the settle path. Default
      ``True``; set ``False`` for custom ``compose_mppx`` implementations that
      accept non-standard credential encodings.
    """

    def __init__(
        self,
        *,
        rails: dict[str, CheckoutRailSpec],
        url: str,
        compute_pricing: PricingFn,
        pre_validate: PreValidateFn | None = None,
        # Explicit handler overrides; pass these when the merchant has custom
        # x402 / MPP wiring. When omitted, Checkout auto-derives from the
        # flat-config kwargs below (the common case).
        x402_server: Any = None,
        compose_mppx: ComposeMppxFn | None = None,
        # Flat-config kwargs; Checkout auto-builds x402_server + compose_mppx
        # from these so merchants don't write the lazy-init / hook boilerplate.
        cdp_api_key_id: str | None = None,
        cdp_api_key_secret: str | None = None,
        mppx_secret_key: str | None = None,
        mint_recipients: RecipientsFn | None = None,
        mint_reference_id: ReferenceIdFn | None = None,
        on_settled: OnSettledFn | None = None,
        is_cached_address: IsCachedAddressFn | None = None,
        zero_settle_carve_out: bool = False,
        credential_pre_check: bool = True,
        gate: CheckoutGateConfig | None = None,
        discovery_extensions: dict[str, Any] | None = None,
        resource_info: dict[str, Any] | None = None,
        discovery_probe: DiscoveryProbeConfig | None = None,
    ) -> None:
        # Auto-derive x402_server when not supplied: rails has an X402BaseRailSpec
        # → lazy-init via SDK helper. Merchants only pass CDP creds (or omit
        # them for the public facilitator); no manual server wiring needed.
        if x402_server is None:
            base_spec = next(
                (spec for spec in rails.values() if isinstance(spec, X402BaseRailSpec)),
                None,
            )
            if base_spec is not None:
                from agentscore_commerce.payment.lazy import lazy_x402_server

                x402_server_getter = lazy_x402_server(
                    spec=base_spec,
                    cdp_api_key_id=cdp_api_key_id,
                    cdp_api_key_secret=cdp_api_key_secret,
                )
                # Cache the getter; Checkout awaits it on first settle path use.
                self._x402_server_getter: Callable[[], Awaitable[Any]] | None = x402_server_getter
            else:
                self._x402_server_getter = None
        else:
            self._x402_server_getter = None
        if x402_server is not None and not any(isinstance(spec, X402BaseRailSpec) for spec in rails.values()):
            msg = (
                "Checkout: x402_server requires an X402BaseRailSpec in `rails` "
                "(the rail's `network` field supplies the CAIP-2)."
            )
            raise ValueError(msg)

        # Auto-derive compose_mppx when not supplied: any MPP rail + secret_key
        # → wire make_mppx_compose_hook + lazy_mppx_server internally.
        if compose_mppx is None and mppx_secret_key is not None:
            mpp_rails = {
                k: v
                for k, v in rails.items()
                if isinstance(v, (TempoRailSpec, SolanaMppRailSpec, TempoSessionRailSpec, StripeRailSpec))
            }
            if mpp_rails:
                from agentscore_commerce.checkout_hooks import make_mppx_compose_hook
                from agentscore_commerce.payment.lazy import lazy_mppx_server

                getter = lazy_mppx_server(
                    rails=mpp_rails,
                    secret_key=mppx_secret_key,
                    realm=_realm_from_url(url),
                )
                compose_mppx = make_mppx_compose_hook(server_getter=getter)

        self.rails = rails
        self.url = url
        self.merchant_name = gate.merchant_name if gate is not None else None
        self.compute_pricing = compute_pricing
        self.pre_validate = pre_validate
        self.x402_server = x402_server
        self.compose_mppx = compose_mppx
        self.mint_recipients = mint_recipients
        self.mint_reference_id = mint_reference_id
        self.on_settled = on_settled
        self.is_cached_address = is_cached_address
        self.zero_settle_carve_out = zero_settle_carve_out
        self.credential_pre_check = credential_pre_check
        self.gate = gate
        # Lazily-built JWKS cache for AIP verification, shared across requests so issuer keys are
        # fetched once and cached (per the verifier's hard 24h cap). Built on first AIT.
        self._aip_jwks: JwksCache | None = None
        self.discovery_extensions = discovery_extensions
        # Optional x402 v2 ResourceInfo metadata (keys are the wire field names:
        # serviceName / tags / iconUrl / description) advertised on the 402, in both
        # the body and the PAYMENT-REQUIRED header. url + mimeType are auto-filled.
        self.resource_info = resource_info
        self.discovery_probe = discovery_probe
        """Per-endpoint x402 ``extensions`` block emitted on the 402 body. Merge
        outputs of ``build_bazaar_discovery_payload({...})`` (or other extension
        declarers) here — Checkout forwards verbatim into the 402 response
        body's ``extensions`` field so Bazaar crawlers and other spec-compliant
        clients read the route's declared input/output schema."""

    def _has_identity_gate(self) -> bool:
        """Return True when the merchant configured an identity-bearing flag.

        The identity-bearing flags are ``require_kyc``, ``require_sanctions_clear``
        (name screening on the KYC identity), ``min_age``, or jurisdiction
        lists. Wallet OFAC SDN enforcement (the always-on default) does NOT
        count as an identity gate; agents don't need an AgentScore credential
        to satisfy it.

        Used to conditionally emit AgentScore identity boilerplate in 402
        bodies (``agent_memory``, ``X-Operator-Token`` references in per-rail
        commands).
        """
        g = self.gate
        if g is None:
            return False
        return bool(
            g.require_kyc
            or g.require_sanctions_clear
            or g.min_age is not None
            or (g.allowed_jurisdictions and len(g.allowed_jurisdictions) > 0)
            or (g.blocked_jurisdictions and len(g.blocked_jurisdictions) > 0)
        )

    async def _get_x402_server(self) -> Any:
        """Resolve the x402 server.

        Explicit ``x402_server`` wins; otherwise the auto-derived lazy getter
        is awaited once and cached.
        """
        if self.x402_server is not None:
            return self.x402_server
        if self._x402_server_getter is None:
            return None
        self.x402_server = await self._x402_server_getter()
        return self.x402_server

    def _x402_server_available(self) -> bool:
        """Whether Checkout can resolve an x402 server.

        True when either an explicit ``x402_server`` was supplied or an
        auto-derived lazy getter is available.
        """
        return self.x402_server is not None or self._x402_server_getter is not None

    @property
    def accepted_rails(self) -> list[RailKey]:
        """Canonical ``RailKey`` list derived from the configured rails dict.

        Each ``*RailSpec`` type maps to one ``RailKey`` (Tempo & TempoSession
        both fold to ``"tempo_mpp"``). Dedupes so listing per protocol, not
        per recipient address. Use in /.well-known/mpp.json,
        skill.md / llms.txt discovery responses.
        """
        out: list[RailKey] = []
        seen: set[str] = set()
        for spec in self.rails.values():
            key = _spec_rail_key(spec)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    @property
    def accepted_method_names(self) -> list[str]:
        """Protocol-shaped method-name list (``"tempo/charge"``, ``"x402/exact (base)"``).

        Suitable for the ``methods: [...]`` array of
        ``/.well-known/mpp.json``'s ``PaymentMethodConfig``.
        """
        out: list[str] = []
        seen: set[str] = set()
        for spec in self.rails.values():
            name = _spec_method_name(spec)
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    def _x402_rail_key(self) -> str:
        """Return the merchant's rails-dict key for the X402BaseRailSpec entry.

        Defaults to ``"x402_base"`` when no match is found.
        """
        for key, spec in self.rails.items():
            if isinstance(spec, X402BaseRailSpec):
                return key
        return "x402_base"

    def _mpp_rail_key(self) -> str:
        """Return the merchant's rails-dict key for the primary MPP rail.

        Prefers ``tempo`` (most common MPP rail today). Used by the zero-settle
        carve-out path when the merchant hasn't otherwise specified rail_key.
        """
        for key, spec in self.rails.items():
            if isinstance(spec, TempoRailSpec):
                return key
        for key, spec in self.rails.items():
            if isinstance(spec, (SolanaMppRailSpec, TempoSessionRailSpec, StripeRailSpec)):
                return key
        return "tempo"

    def _rails_key_for_mppx_method(self, method: str) -> str | None:
        """Map an mppx credential ``method`` to the merchant's rails-dict key.

        Used in ``_handle_mppx`` so the settle outcome distinguishes Solana
        from Tempo (both fall under ``rail="mpp"``) and from Stripe SPT.
        ``method`` is one of ``tempo`` / ``solana`` / ``stripe``. Returns
        ``None`` when the merchant has no rail registered for that method.
        """
        if method == "stripe":
            for key, spec in self.rails.items():
                if isinstance(spec, StripeRailSpec):
                    return key
            return None
        if method == "solana":
            for key, spec in self.rails.items():
                if isinstance(spec, SolanaMppRailSpec):
                    return key
            return None
        if method == "tempo":
            for key, spec in self.rails.items():
                if isinstance(spec, (TempoRailSpec, TempoSessionRailSpec)):
                    return key
            return None
        return None

    @property
    def _x402_base_network(self) -> str | None:
        """CAIP-2 read from ``rails['x402_base'].network`` (or its default).

        Defined only when an ``X402BaseRailSpec`` is present in rails AND a
        server is configured (explicit or auto-derived); otherwise ``None``.
        """
        if not self._x402_server_available():
            return None
        for spec in self.rails.values():
            if isinstance(spec, X402BaseRailSpec):
                return spec.network
        return None

    async def handle(self, request: CheckoutRequest) -> CheckoutResult:
        """One-call agent-commerce flow.

        * x402 ``X-Payment`` header present → verify, settle via ``x402_server``,
          run ``on_settled`` hook, return 200 with the hook's body (or
          ``{ok: true, reference_id}``).
        * MPP ``Authorization: Payment`` header present + ``compose_mppx`` hook
          configured → invoke hook, 200 / 402 outcome composed.
        * Otherwise → emit 402 with all configured rails.
        """
        reference_id = await self._mint_reference_id(request)
        ctx = CheckoutContext(request=request, reference_id=reference_id)

        # Discovery probe (optional): empty-body POST without a payment header
        # → sample 402 advertising the merchant's shape for crawlers. Routes
        # AHEAD of pre_validate so probe responses don't trip on body-validation
        # rules (probes carry no business body).
        if self.discovery_probe is not None:
            from agentscore_commerce.discovery import (
                build_discovery_probe_response,
                is_discovery_probe_request,
            )

            auth = request.headers.get("authorization") or request.headers.get("Authorization")
            body_text = json.dumps(request.body) if request.body else ""
            if await is_discovery_probe_request(request.method, auth, body_text):
                cfg = self.discovery_probe
                probe = build_discovery_probe_response(
                    realm=cfg.realm,
                    sample_rail=cfg.sample_rail,
                    sample_amount_usd=cfg.sample_amount_usd,
                    sample_recipient=cfg.sample_recipient,
                    intent=cfg.intent,
                    ttl_seconds=cfg.ttl_seconds,
                    docs_url=cfg.docs_url,
                    message=cfg.message,
                    x402_sample=cfg.x402_sample,
                )
                return CheckoutResult(
                    status=probe.status,
                    body=json.loads(probe.body),
                    headers=probe.headers,
                    reference_id=ctx.reference_id,
                    settled=False,
                    settle_phase="discovery_probe",
                )

        # Credential shape gate: runs BEFORE pre_validate / the identity gate /
        # pricing / recipient minting so a junk payment header cannot trigger
        # merchant hooks (which may do paid upstream work) or burn an assess
        # call. Shape only — real verification stays on the settle path, which
        # needs per-request state the hooks produce. Scoped to the credential
        # channels this Checkout actually dispatches on, so e.g. an x402 header
        # at a Tempo-only merchant keeps its current discovery-leg behavior.
        if self.credential_pre_check:
            malformed = malformed_payment_credential(request.headers)
            enforced = malformed is not None and (
                (self._x402_server_available() and self._x402_base_network is not None)
                if malformed.channel == "x402"
                else self.compose_mppx is not None
            )
            if enforced and malformed is not None:
                # A junk credential is treated as a discovery request: strip it
                # and re-enter handle() so pre_validate + pricing + recipient
                # minting + compose all run their fresh path exactly as for a
                # no-credential request. That yields a fresh 402 the agent
                # re-pays against, not a dead-end 400, and not a 500 when
                # compute_pricing reads state that pre_validate populates. The
                # gate/assess and settle are skipped by construction: after
                # stripping there is no payment header, so no re-trigger of this
                # check (max one level of recursion) and no identity call.
                result = await self.handle(self._strip_payment_headers(request))
                return dataclasses.replace(result, settle_phase="credential_malformed")

        # Pre-validate (optional): resolve merchant-specific per-request state
        # (product lookup, code resolution, shipping checks, ...). May raise
        # CheckoutValidationError to short-circuit with a 4xx; otherwise return
        # a dict that's stashed on ``ctx.state`` for downstream hooks to read.
        if self.pre_validate is not None:
            try:
                state = await _maybe_await(self.pre_validate(ctx))
            except CheckoutValidationError as err:
                return CheckoutResult(
                    status=err.status,
                    body=build_validation_error(
                        code=err.code,
                        message=err.message,
                        next_steps={"action": err.action, "user_message": err.message},
                        extra=err.extra,
                    ),
                    headers={},
                    reference_id=ctx.reference_id,
                    settled=False,
                    settle_phase="pre_validate_failed",
                )
            if isinstance(state, dict):
                ctx.state = state

        # Per-request compliance: runs on the settle leg only (anonymous
        # discovery passes through to 402).
        #
        # Two paths converge here:
        #   - Merchants with an explicit ``gate`` config run the full identity
        #     policy (KYC / age / sanctions / jurisdiction) via ``_run_gate``.
        #   - Merchants WITHOUT a ``gate`` config still get wallet OFAC SDN
        #     enforcement via ``_run_wallet_sanctions_only`` — the always-on
        #     strict-liability default. Falls back to AGENTSCORE_API_KEY env
        #     var when set; logs a warning and skips when no key is set
        #     (dev/testnet pattern).
        has_payment_header = has_x402_header(request.headers) or has_mppx_header(request.headers)
        if has_payment_header:
            gate_result = (
                await self._run_gate(ctx) if self.gate is not None else await self._run_wallet_sanctions_only(ctx)
            )
            if gate_result is not None:
                return gate_result

        # Pricing is computed AFTER the gate (computed after the gate -> computePricing
        # order) so identity-aware pricing hooks see the gate's verdict (ctx.identity_status /
        # ctx.request.assess), not "anonymous". The gate is pure identity and never reads
        # ctx.pricing; every pricing consumer (zero-settle, settle, 402) runs below here.
        ctx.pricing = await _maybe_await(self.compute_pricing(ctx))

        # Zero-amount carve-out: CDP rejects EIP-3009 with value=0 and pympp's
        # tempo intents reject ``proof`` payloads. When pricing is $0 AND a
        # payment header is present, verify the credential to lift the signer
        # then short-circuit to ``on_settled`` with tx_hash=None.
        if (
            self.zero_settle_carve_out
            and ctx.pricing is not None
            and ctx.pricing.amount_usd == 0
            and (has_x402_header(request.headers) or has_mppx_header(request.headers))
        ):
            return await self._handle_zero_settle(ctx)

        if has_x402_header(request.headers) and self._x402_server_available() and self._x402_base_network:
            return await self._handle_x402(ctx)

        if has_mppx_header(request.headers) and self.compose_mppx is not None:
            return await self._handle_mppx(ctx)

        # Discovery leg: emit the fresh 402 challenge. compose_mppx (if
        # configured) supplies the fresh ``WWW-Authenticate`` the agent signs on
        # the retry; ``_emit_402`` resolves per-order recipients.
        return await self._emit_fresh_challenge(ctx)

    @staticmethod
    def _extra_headers(headers: dict[str, str]) -> dict[str, str]:
        """Strip ``Content-Type`` (case-insensitive); framework JSON helpers set it themselves."""
        return {k: v for k, v in headers.items() if k.lower() != "content-type"}

    @staticmethod
    def _render_content_type(headers: dict[str, str]) -> str:
        """Resolve the response Content-Type for a framework renderer.

        Honors an explicitly-set ``content-type`` from the result headers (the AIP deny paths set
        ``application/problem+json`` so both the edge-deny and the policy-deny superset
        content-negotiate as RFC 9457), and falls back to ``application/json`` for every other
        response. Non-AIP paths never set a content-type header, so this leaves them on the JSON
        default untouched.
        """
        for k, v in headers.items():
            if k.lower() == "content-type":
                return v
        return "application/json"

    async def handle_fastapi(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        """FastAPI / Starlette adapter; returns a ``JSONResponse``.

        Saves merchants from constructing :class:`CheckoutRequest` by hand and
        wrapping the :class:`CheckoutResult` in a response. When ``body`` is not
        provided, the adapter calls ``await request.json()``; pass a pre-parsed
        pydantic dump when the route already validated the body shape.

        Compatible with ``Checkout(gate=...)``; the request is passed through
        as ``CheckoutRequest.raw`` so the gate operates on it.
        """
        from fastapi.responses import JSONResponse

        if body is None:
            try:
                parsed_body = await request.json()
            except (ValueError, TypeError):
                # Empty / unparseable body must reach handle(), not 400 before it: x402
                # discovery validators probe with an empty body and no payment header and
                # require the 402 challenge. Treat it as {} and let handle() decide; body
                # validation runs on the paid leg (pre_validate / gate).
                parsed_body = {}
        else:
            parsed_body = body
        result = await self.handle(
            CheckoutRequest(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=parsed_body,
                assess=None,
                raw=request,
            ),
        )
        return JSONResponse(
            content=result.body,
            status_code=result.status,
            headers=self._extra_headers(result.headers),
            media_type=self._render_content_type(result.headers),
        )

    async def handle_aiohttp(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        """Aiohttp adapter; returns ``aiohttp.web.Response``.

        Uses ``await request.json()`` for body parsing when ``body`` isn't supplied;
        passes the native ``aiohttp.web.Request`` through as ``CheckoutRequest.raw``.
        """
        from aiohttp import web

        if body is None:
            try:
                parsed_body = await request.json()
            except (ValueError, TypeError):
                # See handle_fastapi: empty / unparseable body falls through to the paywall.
                parsed_body = {}
        else:
            parsed_body = body
        result = await self.handle(
            CheckoutRequest(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=parsed_body,
                assess=None,
                raw=request,
            ),
        )
        return web.json_response(
            result.body,
            status=result.status,
            headers=self._extra_headers(result.headers),
            content_type=self._render_content_type(result.headers),
        )

    async def handle_sanic(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        """Sanic adapter; returns ``sanic.response.HTTPResponse``.

        Sanic exposes ``request.json`` as a sync property (already-parsed). Pass
        ``body=`` to skip the property read.
        """
        from sanic.response import json as sanic_json

        if body is None:
            try:
                parsed_body = request.json or {}
            except Exception:
                # See handle_fastapi: empty / unparseable body falls through to the paywall.
                parsed_body = {}
        else:
            parsed_body = body
        result = await self.handle(
            CheckoutRequest(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=parsed_body,
                assess=None,
                raw=request,
            ),
        )
        return sanic_json(
            result.body,
            status=result.status,
            headers=self._extra_headers(result.headers),
            content_type=self._render_content_type(result.headers),
        )

    def handle_flask(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        """Flask adapter; returns a ``flask.Response``.

        Flask is sync; this method bridges into the async :meth:`handle` via
        :func:`asgiref.sync.async_to_sync`. Use inside a sync ``@app.route``
        handler, or call from an ``async def`` view in Flask 2.2+ (which uses
        the same bridge internally).
        """
        from asgiref.sync import async_to_sync
        from flask import jsonify

        if body is None:
            parsed_body = request.get_json(silent=True)
            if parsed_body is None:
                # See handle_fastapi: empty / unparseable body falls through to the paywall.
                parsed_body = {}
        else:
            parsed_body = body
        checkout_request = CheckoutRequest(
            method=request.method,
            url=request.url,
            headers=dict(request.headers),
            body=parsed_body,
            assess=None,
            raw=request,
        )
        result = async_to_sync(self.handle)(checkout_request)
        resp = jsonify(result.body)
        resp.status_code = result.status
        # Honor an explicit content-type (AIP problem+json); jsonify defaults to application/json.
        resp.content_type = self._render_content_type(result.headers)
        for k, v in self._extra_headers(result.headers).items():
            resp.headers[k] = v
        return resp

    def handle_django(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        """Django adapter; returns a ``django.http.JsonResponse``.

        Django is sync (async views are supported but not assumed here); this
        method bridges into the async :meth:`handle` via
        :func:`asgiref.sync.async_to_sync`.
        """
        import json as _json

        from asgiref.sync import async_to_sync
        from django.http import JsonResponse

        if body is None:
            try:
                parsed_body = _json.loads(request.body) if request.body else {}
            except (ValueError, TypeError):
                # See handle_fastapi: empty / unparseable body falls through to the paywall.
                parsed_body = {}
        else:
            parsed_body = body
        checkout_request = CheckoutRequest(
            method=request.method,
            url=request.build_absolute_uri(),
            headers=dict(request.headers.items()),
            body=parsed_body,
            assess=None,
            raw=request,
        )
        result = async_to_sync(self.handle)(checkout_request)
        return JsonResponse(
            result.body,
            status=result.status,
            headers=self._extra_headers(result.headers),
            content_type=self._render_content_type(result.headers),
        )

    # ─────────────────────────────────────────────────────────────────────
    # mount_ucp_routes_<framework> — register `/.well-known/ucp` + `/jwks.json`
    # + OPTIONS preflights on the app in one call. Saves merchants the ~40-line
    # 3-route registration block every UCP-publishing merchant otherwise
    # hand-rolls. Equivalent across all five Python framework adapters.
    # ─────────────────────────────────────────────────────────────────────

    def _build_ucp_resp(
        self,
        request_headers: Mapping[str, str],
        *,
        name: str,
        well_known_ucp_url: str,
        services: dict[str, Any],
        signing_kid: str,
        agentscore_gate: Any,
    ) -> Any:
        from agentscore_commerce.discovery.well_known import build_signed_ucp_response

        return build_signed_ucp_response(
            checkout=self,
            name=name,
            well_known_ucp_url=well_known_ucp_url,
            services=services,
            request_headers=request_headers,
            signing_kid=signing_kid,
            agentscore_gate=agentscore_gate,
        )

    def _build_jwks_resp(self, request_headers: Mapping[str, str], *, signing_kid: str) -> Any:
        from agentscore_commerce.discovery.well_known import build_signed_jwks_response

        return build_signed_jwks_response(request_headers=request_headers, signing_kid=signing_kid)

    def _build_preflight(self, request_headers: Mapping[str, str]) -> Any:
        from agentscore_commerce.discovery.well_known import well_known_preflight_response

        return well_known_preflight_response(request_headers)

    def mount_ucp_routes_fastapi(
        self,
        app: Any,
        *,
        name: str,
        well_known_ucp_url: str,
        services: dict[str, Any],
        signing_kid: str = "merchant-default",
        agentscore_gate: Any = None,
        ucp_path: str = "/.well-known/ucp",
        jwks_path: str = "/.well-known/jwks.json",
    ) -> None:
        """Register signed UCP + JWKS + preflight routes on a FastAPI app."""
        from fastapi import Request

        from agentscore_commerce.discovery.well_known import (
            signed_response_fastapi,
        )

        async def _ucp(request):  # type: ignore[no-untyped-def]
            return signed_response_fastapi(
                self._build_ucp_resp(
                    dict(request.headers),
                    name=name,
                    well_known_ucp_url=well_known_ucp_url,
                    services=services,
                    signing_kid=signing_kid,
                    agentscore_gate=agentscore_gate,
                )
            )

        async def _jwks(request):  # type: ignore[no-untyped-def]
            return signed_response_fastapi(self._build_jwks_resp(dict(request.headers), signing_kid=signing_kid))

        async def _preflight(request):  # type: ignore[no-untyped-def]
            return signed_response_fastapi(self._build_preflight(dict(request.headers)))

        # Patch annotations so FastAPI's signature inspection sees the real
        # Request class (PEP 563 / `from __future__ import annotations` would
        # otherwise stringify the annotation and break Request injection).
        for fn in (_ucp, _jwks, _preflight):
            fn.__annotations__ = {"request": Request}

        app.get(ucp_path)(_ucp)
        app.get(jwks_path)(_jwks)
        app.options(ucp_path)(_preflight)
        app.options(jwks_path)(_preflight)

    def mount_ucp_routes_flask(
        self,
        app: Any,
        *,
        name: str,
        well_known_ucp_url: str,
        services: dict[str, Any],
        signing_kid: str = "merchant-default",
        agentscore_gate: Any = None,
        ucp_path: str = "/.well-known/ucp",
        jwks_path: str = "/.well-known/jwks.json",
    ) -> None:
        """Register signed UCP + JWKS + preflight routes on a Flask app."""
        from flask import request as flask_request

        from agentscore_commerce.discovery.well_known import signed_response_flask

        def _ucp() -> Any:
            headers = dict(flask_request.headers)
            if flask_request.method == "OPTIONS":
                return signed_response_flask(self._build_preflight(headers))
            return signed_response_flask(
                self._build_ucp_resp(
                    headers,
                    name=name,
                    well_known_ucp_url=well_known_ucp_url,
                    services=services,
                    signing_kid=signing_kid,
                    agentscore_gate=agentscore_gate,
                )
            )

        def _jwks() -> Any:
            headers = dict(flask_request.headers)
            if flask_request.method == "OPTIONS":
                return signed_response_flask(self._build_preflight(headers))
            return signed_response_flask(self._build_jwks_resp(headers, signing_kid=signing_kid))

        app.add_url_rule(
            ucp_path,
            "agentscore_ucp",
            _ucp,
            methods=["GET", "OPTIONS"],
            provide_automatic_options=False,
        )
        app.add_url_rule(
            jwks_path,
            "agentscore_jwks",
            _jwks,
            methods=["GET", "OPTIONS"],
            provide_automatic_options=False,
        )

    def mount_ucp_routes_django(
        self,
        urlpatterns: list[Any],
        *,
        name: str,
        well_known_ucp_url: str,
        services: dict[str, Any],
        signing_kid: str = "merchant-default",
        agentscore_gate: Any = None,
        ucp_path: str = ".well-known/ucp",
        jwks_path: str = ".well-known/jwks.json",
    ) -> None:
        """Append signed UCP + JWKS + preflight URL patterns to a Django urlpatterns list.

        Django routes don't take leading slashes; the defaults already omit them.
        Each path serves GET + OPTIONS through the same view; the view dispatches
        on ``request.method``.
        """
        from django.urls import path

        from agentscore_commerce.discovery.well_known import signed_response_django

        def _ucp_view(request: Any) -> Any:
            headers = dict(request.headers.items())
            if request.method == "OPTIONS":
                return signed_response_django(self._build_preflight(headers))
            return signed_response_django(
                self._build_ucp_resp(
                    headers,
                    name=name,
                    well_known_ucp_url=well_known_ucp_url,
                    services=services,
                    signing_kid=signing_kid,
                    agentscore_gate=agentscore_gate,
                )
            )

        def _jwks_view(request: Any) -> Any:
            headers = dict(request.headers.items())
            if request.method == "OPTIONS":
                return signed_response_django(self._build_preflight(headers))
            return signed_response_django(self._build_jwks_resp(headers, signing_kid=signing_kid))

        urlpatterns.append(path(ucp_path, _ucp_view))
        urlpatterns.append(path(jwks_path, _jwks_view))

    def mount_ucp_routes_aiohttp(
        self,
        app: Any,
        *,
        name: str,
        well_known_ucp_url: str,
        services: dict[str, Any],
        signing_kid: str = "merchant-default",
        agentscore_gate: Any = None,
        ucp_path: str = "/.well-known/ucp",
        jwks_path: str = "/.well-known/jwks.json",
    ) -> None:
        """Register signed UCP + JWKS + preflight routes on an aiohttp app."""
        from agentscore_commerce.discovery.well_known import signed_response_aiohttp

        async def _ucp(request: Any) -> Any:
            return signed_response_aiohttp(
                self._build_ucp_resp(
                    dict(request.headers),
                    name=name,
                    well_known_ucp_url=well_known_ucp_url,
                    services=services,
                    signing_kid=signing_kid,
                    agentscore_gate=agentscore_gate,
                )
            )

        async def _jwks(request: Any) -> Any:
            return signed_response_aiohttp(self._build_jwks_resp(dict(request.headers), signing_kid=signing_kid))

        async def _preflight(request: Any) -> Any:
            return signed_response_aiohttp(self._build_preflight(dict(request.headers)))

        app.router.add_get(ucp_path, _ucp)
        app.router.add_get(jwks_path, _jwks)
        app.router.add_options(ucp_path, _preflight)
        app.router.add_options(jwks_path, _preflight)

    def mount_ucp_routes_sanic(
        self,
        app: Any,
        *,
        name: str,
        well_known_ucp_url: str,
        services: dict[str, Any],
        signing_kid: str = "merchant-default",
        agentscore_gate: Any = None,
        ucp_path: str = "/.well-known/ucp",
        jwks_path: str = "/.well-known/jwks.json",
    ) -> None:
        """Register signed UCP + JWKS + preflight routes on a Sanic app."""
        from agentscore_commerce.discovery.well_known import signed_response_sanic

        async def _ucp(request: Any) -> Any:
            return signed_response_sanic(
                self._build_ucp_resp(
                    dict(request.headers),
                    name=name,
                    well_known_ucp_url=well_known_ucp_url,
                    services=services,
                    signing_kid=signing_kid,
                    agentscore_gate=agentscore_gate,
                )
            )

        async def _jwks(request: Any) -> Any:
            return signed_response_sanic(self._build_jwks_resp(dict(request.headers), signing_kid=signing_kid))

        async def _preflight(request: Any) -> Any:
            return signed_response_sanic(self._build_preflight(dict(request.headers)))

        app.add_route(_ucp, ucp_path, methods=["GET"], name="agentscore_ucp")
        app.add_route(_jwks, jwks_path, methods=["GET"], name="agentscore_jwks")
        app.add_route(_preflight, ucp_path, methods=["OPTIONS"], name="agentscore_ucp_options")
        app.add_route(_preflight, jwks_path, methods=["OPTIONS"], name="agentscore_jwks_options")

    def _get_aip_jwks(self, cfg: AipGateConfig) -> JwksCache:
        """Resolve the lazily-built JWKS cache for AIP verification.

        Built once on first AIT and shared across requests so issuer keys are fetched once and
        cached. :class:`JwksCache` merges AgentScore's canonical issuer itself, so only the
        merchant's external issuers (if any) are passed.
        """
        if self._aip_jwks is None:
            from agentscore_commerce.aip.jwks import JwksCache

            self._aip_jwks = (
                JwksCache(trusted_issuers=cfg.trusted_issuers) if cfg.trusted_issuers is not None else JwksCache()
            )
        return self._aip_jwks

    async def _run_aip_assess(
        self,
        ctx: CheckoutContext,
        gate: CheckoutGateConfig,
        eff_policy: AipIssuerPolicy,
        aip_token: str,
        aip_signature: dict[str, str] | None,
    ) -> CheckoutResult | None:
        """Forward a verified AIT to /v1/assess and map the decision to allow / deny.

        The edge already verified the issuer signature + RFC 9421 PoP (fail-fast); the API
        re-verifies PoP authoritatively and evaluates ``eff_policy`` against the token's attested
        claims. Returns ``None`` on allow (stamping ``identity_status='verified'`` on
        ``ctx.assess``); a denial :class:`CheckoutResult` otherwise. Compliance fields come from
        ``eff_policy`` — the per-issuer override for the verified AIT's issuer when configured,
        else the gate defaults (a whole-policy replacement, mirroring node).
        """
        from agentscore.errors import (
            AgentScoreError,
            InvalidCredentialError,
            TokenExpiredError,
        )

        from agentscore_commerce.identity.core import AgentScoreCore
        from agentscore_commerce.identity.types import DenialReason

        assert gate.api_key is not None  # noqa: S101  # only reached on the api_key path.
        core_kwargs: dict[str, Any] = {
            "api_key": gate.api_key,
            "base_url": gate.base_url,
            "fail_open": gate.fail_open,
            "cache_seconds": gate.cache_seconds,
        }
        if gate.user_agent is not None:
            core_kwargs["user_agent"] = gate.user_agent
        if gate.chain is not None:
            core_kwargs["chain"] = gate.chain
        if eff_policy.require_kyc is not None:
            core_kwargs["require_kyc"] = eff_policy.require_kyc
        if eff_policy.require_sanctions_clear is not None:
            core_kwargs["require_sanctions_clear"] = eff_policy.require_sanctions_clear
        if eff_policy.min_age is not None:
            core_kwargs["min_age"] = eff_policy.min_age
        if eff_policy.blocked_jurisdictions is not None:
            core_kwargs["blocked_jurisdictions"] = eff_policy.blocked_jurisdictions
        if eff_policy.allowed_jurisdictions is not None:
            core_kwargs["allowed_jurisdictions"] = eff_policy.allowed_jurisdictions
        core = AgentScoreCore(**core_kwargs)

        # Extract the payment signer (when present) so the API can OFAC-screen the crypto-rail
        # signer alongside the AIT. Signer-match enforcement is NOT applied on the AIT path: the
        # identity is the token (PoP-bound via cnf), and assess is keyed by aip_token, so there is
        # no address-keyed signer verdict to read (the wallet binding for AITs is the IdP's
        # payment.signer claim, enforced server-side).
        from agentscore_commerce.payment.signer import extract_payment_signer, read_x402_payment_header

        x402_header = read_x402_payment_header(ctx.request.headers)
        authorization_header: str | None = None
        for header_key, header_value in ctx.request.headers.items():
            if header_key.lower() == "authorization":
                authorization_header = header_value
                break
        signer = extract_payment_signer(x402_header, authorization_header=authorization_header)
        signer_arg = {"address": signer.address, "network": signer.network} if signer is not None else None

        try:
            result = await core.acheck(
                aip_token=aip_token,
                aip_signature=cast("Any", aip_signature),
                signer=signer_arg,
            )
        except (TokenExpiredError, InvalidCredentialError) as err:
            reason = DenialReason(
                code="invalid_credential" if isinstance(err, InvalidCredentialError) else "token_expired",
                message=str(err),
            )
            return await self._aip_denial_result(ctx, gate, eff_policy, reason)
        except (AgentScoreError, Exception) as err:
            # Fail-closed (strict liability): API outage / network failure → 503 api_error.
            reason = DenialReason(code="api_error", message=str(err))
            return await self._aip_denial_result(ctx, gate, eff_policy, reason)

        if not result.allow:
            reason = DenialReason(
                code="wallet_not_trusted",
                reasons=list(result.reasons or []),
                decision=result.decision,
            )
            return await self._aip_denial_result(ctx, gate, eff_policy, reason)

        # Allow: stamp identity_status so downstream hooks see the verified AIT identity.
        assess = dict(ctx.request.assess or {})
        assess["identity_status"] = "verified"
        ctx.request = CheckoutRequest(
            method=ctx.request.method,
            url=ctx.request.url,
            headers=ctx.request.headers,
            body=ctx.request.body,
            assess=assess,
            raw=ctx.request.raw,
        )
        return None

    async def _aip_denial_result(
        self,
        ctx: CheckoutContext,
        gate: CheckoutGateConfig,
        eff_policy: AipIssuerPolicy,
        reason: Any,
    ) -> CheckoutResult:
        """Build the AIT-path denial CheckoutResult.

        ``on_denied`` runs FIRST (node parity): when it returns an override it fully owns the body,
        so no superset wrapping happens. Otherwise the AgentScore denial body is emitted as an
        RFC 9457 + AIP-spec SUPERSET (``application/problem+json``) — both schemes at once: the rich
        AgentScore ``{ error, agent_instructions, ... }`` AND the spec's ``type``/``title``/
        ``status``/``detail`` (+ escalation). The wallet / operator-token paths never reach here, so
        they keep the bare AgentScore body + ``application/json``.
        """
        from agentscore_commerce.aip.gate import AipErrorRequirements, build_aip_policy_deny_body
        from agentscore_commerce.identity._denial import denial_reason_status
        from agentscore_commerce.identity._response import denial_reason_to_body

        canonical_body = denial_reason_to_body(reason)
        if gate.on_denied is not None:
            custom = await _maybe_await(gate.on_denied(ctx, reason))
            if isinstance(custom, dict) and "body" in custom:
                return CheckoutResult(
                    status=custom.get("status", denial_reason_status(reason)),
                    body=custom.get("body") or canonical_body,
                    headers={},
                    reference_id=ctx.reference_id,
                    settled=False,
                    settle_phase="gate_denied",
                )
        requirements = AipErrorRequirements(
            trusted_issuers=_aip_trusted_issuer_set(gate.aip) if gate.aip is not None else None,
            required_claims=_aip_required_claims(eff_policy),
            required_trust_level=gate.aip.require_trust_level if gate.aip is not None else None,
            required_amr=gate.aip.require_amr if gate.aip is not None else None,
        )
        superset = build_aip_policy_deny_body(reason.code, reason.reasons, canonical_body, requirements)
        return CheckoutResult(
            status=int(superset["status"]),
            body=superset,
            headers={"content-type": "application/problem+json"},
            reference_id=ctx.reference_id,
            settled=False,
            settle_phase="gate_denied",
        )

    async def _run_gate(self, ctx: CheckoutContext) -> CheckoutResult | None:
        """Run the per-request gate.

        Returns a denial CheckoutResult on hard denial; ``None`` on accept /
        soft-unverified / anonymous (in which case ``ctx.assess`` is populated
        with ``identity_status``).

        Three customization seams (in order of precedence):

        1. ``gate.run_gate`` — when set, replaces the SDK's gate flow entirely.
        2. ``gate.per_request_policy`` — per-request policy override merged over
           static gate fields. Return ``None`` to skip the gate.
        3. ``gate.on_denied`` — invoked after canonical DenialReason is built to
           reshape the body for the merchant's response contract.
        """
        if self.gate is None:
            return None

        gate = self.gate
        # 1. run_gate escape hatch — replaces everything else (also bypasses the gate.aip AIP
        #    pre-step below; a custom gate owns AIT verification too, so run_gate and gate.aip
        #    are mutually exclusive).
        if gate.run_gate is not None:
            result = await _maybe_await(gate.run_gate(ctx))
            return self._coerce_run_gate_result(ctx, result)

        # AIP pre-step — runs BEFORE the no-api_key fallback so a present-but-invalid AIT is
        # always a hard deny, and a cryptographically verified AIT is honored even on an
        # offline-only gate. The RFC 9421 proof-of-possession can only be checked here at the
        # edge, where the signed HTTP message lives. A valid AIT becomes the sole identity (wins
        # over wallet / operator-token).
        from agentscore_commerce.aip.request import has_agent_identity_header_parts

        headers_lower = normalize_headers_to_lowercase(ctx.request.headers)
        aip_token: str | None = None
        aip_issuer: str | None = None
        aip_signature: dict[str, str] | None = None
        if gate.aip is not None and has_agent_identity_header_parts(headers_lower):
            from agentscore_commerce.aip.gate import (
                AipErrorRequirements,
                AipGateOptions,
                build_aip_error_body,
                build_aip_weak_auth_body,
                check_trust_requirements,
                verify_ait_parts,
            )
            from agentscore_commerce.aip.request import VerifyContextParts

            parts: VerifyContextParts = {
                "method": ctx.request.method,
                "url": ctx.request.url,
                "headers": headers_lower,
            }
            if gate.aip.authority is not None:
                parts["authority"] = gate.aip.authority
            opts = AipGateOptions(
                jwks=self._get_aip_jwks(gate.aip),
                max_skew_seconds=gate.aip.max_skew_seconds,
                require_trust_level=gate.aip.require_trust_level,
                require_amr=gate.aip.require_amr,
                trusted_issuers=_aip_trusted_issuer_set(gate.aip),
            )
            aip_result = await verify_ait_parts(parts, opts)
            if not aip_result.ok or aip_result.ait is None:
                assert aip_result.failure is not None  # noqa: S101  # ok=False -> failure is set.
                body = build_aip_error_body(
                    aip_result.failure,
                    AipErrorRequirements(
                        trusted_issuers=_aip_trusted_issuer_set(gate.aip),
                        required_trust_level=gate.aip.require_trust_level,
                        required_amr=gate.aip.require_amr,
                    ),
                )
                status = int(body.get("status", 403))
                resp_headers = {"content-type": "application/problem+json"}
                # 503 = the IdP's JWKS was unreachable (transient infra, not a bad token). Hint a
                # short backoff so agents retry rather than uselessly re-signing.
                if status == 503:
                    resp_headers["retry-after"] = "5"
                return CheckoutResult(
                    status=status,
                    body=body,
                    headers=resp_headers,
                    reference_id=ctx.reference_id,
                    settled=False,
                    settle_phase="gate_denied",
                )
            ait = aip_result.ait
            aip_token = ait.token
            aip_issuer = ait.iss
            aip_signature = dataclasses.asdict(ait.signature_material)

            # Enforce the merchant's trust_level / auth.amr requirement (the spec's human-presence
            # gate). Verification-derived (carried in the verified token), so enforced here at the
            # edge — insufficient → weak_auth (403) with required_* so the agent can step up.
            weak_detail = check_trust_requirements(ait.payload, gate.aip.require_trust_level, gate.aip.require_amr)
            if weak_detail is not None:
                body = build_aip_weak_auth_body(
                    detail=weak_detail,
                    required_trust_level=gate.aip.require_trust_level,
                    required_amr=gate.aip.require_amr,
                    trusted_issuers=_aip_trusted_issuer_set(gate.aip),
                )
                return CheckoutResult(
                    status=403,
                    body=body,
                    headers={"content-type": "application/problem+json"},
                    reference_id=ctx.reference_id,
                    settled=False,
                    settle_phase="gate_denied",
                )

        # Resolve the per-issuer policy override (if any) for the verified AIT's issuer. Matched
        # on the canonicalized issuer so keys line up with the trust list's canonicalization. When
        # set, it REPLACES the gate's default compliance policy for this request. The effective
        # compliance fields: the issuer override when present, else the gate defaults.
        issuer_policy: AipIssuerPolicy | None = (
            _resolve_issuer_policy(gate.aip.issuer_policies, aip_issuer)
            if aip_issuer is not None and gate.aip is not None and gate.aip.issuer_policies is not None
            else None
        )
        eff_policy: AipIssuerPolicy = issuer_policy or AipIssuerPolicy(
            require_kyc=gate.require_kyc,
            require_sanctions_clear=gate.require_sanctions_clear,
            min_age=gate.min_age,
            blocked_jurisdictions=gate.blocked_jurisdictions,
            allowed_jurisdictions=gate.allowed_jurisdictions,
        )

        # Gate configured without an API key — full policy enforcement requires
        # /v1/assess access, which we can't reach. Fall through to wallet OFAC
        # SDN enforcement (the strict-liability default) so the merchant still
        # gets the basic protection layer instead of silently allowing.
        if not gate.api_key:
            if aip_token is not None:
                # A cryptographically verified AIT is a complete offline *identity* check (issuer
                # signature + RFC 9421 PoP). But compliance *policy* is evaluated against the
                # token's claims by /v1/assess, which needs an api_key. If the merchant declared
                # policy fields without an api_key we cannot enforce them — fail closed rather than
                # silently allow a verified-but-non-compliant identity. Identity-only gates (no
                # policy fields) are satisfied by the verified AIT alone.
                has_policy = bool(
                    eff_policy.require_kyc
                    or eff_policy.require_sanctions_clear
                    or eff_policy.min_age is not None
                    or eff_policy.blocked_jurisdictions is not None
                    or eff_policy.allowed_jurisdictions is not None
                )
                if has_policy:
                    return CheckoutResult(
                        status=403,
                        body={
                            "error": {
                                "code": "aip_policy_requires_api_key",
                                "message": (
                                    "This gate declares compliance policy (KYC / age / sanctions / "
                                    "jurisdiction) but has no AgentScore api_key, so the Agent "
                                    "Identity Token's claims cannot be evaluated. Configure "
                                    "gate.api_key to enable policy enforcement on AITs."
                                ),
                            },
                        },
                        headers={},
                        reference_id=ctx.reference_id,
                        settled=False,
                        settle_phase="gate_denied",
                    )
                return None
            return await self._run_wallet_sanctions_only(ctx)

        # A verified AIT is the sole identity (wins over wallet / operator-token): forward the
        # token + RFC 9421 signature material to /v1/assess so the API re-verifies PoP
        # authoritatively and evaluates the effective policy against the token's attested claims.
        if aip_token is not None:
            return await self._run_aip_assess(ctx, gate, eff_policy, aip_token, aip_signature)

        # 2. per_request_policy resolves per-product compliance (e.g. wine vs
        # generic merch). Returning None means "no per-product *identity* policy
        # for this product" — but it must NOT skip the always-on wallet OFAC SDN
        # floor. Route to _run_wallet_sanctions_only so a NULL-enforcement product
        # still screens its payment signer (identical to the no-gate dispatch). The
        # floor is a no-op for non-wallet flows (no api_key, or no extractable
        # signer on Stripe SPT / card), so this never forces a wallet onto a
        # free/card/no-signer settle.
        policy: Any = None
        if gate.per_request_policy is not None:
            policy = await _maybe_await(gate.per_request_policy(ctx))
            if policy is None:
                return await self._run_wallet_sanctions_only(ctx)

        from agentscore_commerce.identity.policy import (
            build_gate_from_policy,
            run_gate_with_enforcement,
        )
        from agentscore_commerce.identity.sessions import CreateSessionOnMissing

        # Static gate fields land as the "base" policy; per_request_policy result
        # merges over them so per-product hooks can refine compliance per call.
        merged_policy: dict[str, Any] = {}
        if gate.require_kyc is not None:
            merged_policy["require_kyc"] = gate.require_kyc
        if gate.require_sanctions_clear is not None:
            merged_policy["require_sanctions_clear"] = gate.require_sanctions_clear
        if gate.min_age is not None:
            merged_policy["min_age"] = gate.min_age
        if gate.blocked_jurisdictions is not None:
            merged_policy["blocked_jurisdictions"] = gate.blocked_jurisdictions
        if gate.allowed_jurisdictions is not None:
            merged_policy["allowed_jurisdictions"] = gate.allowed_jurisdictions
        if isinstance(policy, dict):
            merged_policy.update(policy)
        if not merged_policy:
            merged_policy = {}
        # `enforcement` is per-product (soft/hard); read it for the soft/hard handling
        # below but DO NOT remove it — build_gate_from_policy keys off `enforcement` to
        # decide whether to build a gate at all (no enforcement => no gate), and the gate
        # constructor reads only specific fields (require_*, min_age, jurisdictions), so
        # leaving `enforcement` in the dict is harmless. Popping it here previously made
        # build_gate_from_policy always return None, silently bypassing the gate.
        #
        # Static-gate default: a `Checkout(gate=CheckoutGateConfig(require_kyc=True, ...))`
        # built from the static fields alone NEVER carries an `enforcement` key (that only
        # comes from a per_request_policy hook). Without a default, `enforcement` would be
        # None → build_gate_from_policy returns None → run_gate_with_enforcement(None, None)
        # short-circuits to status="anonymous" (allow), silently bypassing ALL compliance.
        # So when the merged policy declares ANY compliance gate field but no explicit
        # enforcement, default to "hard" so the static gate fires. (The per_request_policy
        # path supplies its own enforcement, including an intentional soft/None.) Node has
        # no enforcement abstraction here — it builds the core and calls evaluate whenever
        # policy fields are present (the reference gate); this default
        # restores that always-fire behavior for the static-gate path.
        enforcement = merged_policy.get("enforcement") if isinstance(merged_policy, dict) else None
        if enforcement is None and any(
            key in merged_policy
            for key in (
                "require_kyc",
                "require_sanctions_clear",
                "min_age",
                "allowed_jurisdictions",
                "blocked_jurisdictions",
            )
        ):
            enforcement = "hard"
            merged_policy["enforcement"] = enforcement

        # Use the merchant-supplied CreateSessionOnMissing when provided; else
        # auto-build one from the gate config so missing-identity denials still
        # auto-mint a verify session.
        session = gate.create_session_on_missing or CreateSessionOnMissing(
            api_key=gate.api_key,
            base_url=gate.base_url,
            product_name=gate.merchant_name,
            context=gate.context,
        )
        gate_instance = build_gate_from_policy(
            merged_policy or None,
            api_key=gate.api_key,
            base_url=gate.base_url,
            create_session_on_missing=session,
            # Surface AIP acceptance in the missing-identity recovery instructions +
            # agent_memory hint so agents holding an AIT learn they can present it
            # instead of bootstrapping a session.
            aip_trusted_issuers=_aip_trusted_issuer_set(gate.aip) if gate.aip is not None else None,
        )
        if ctx.request.raw is None:
            msg = (
                "Checkout: gate=... requires CheckoutRequest.raw to be set to the "
                "framework's native request object (today: FastAPI Request)."
            )
            raise RuntimeError(msg)
        result = await run_gate_with_enforcement(
            ctx.request.raw,
            gate_instance,
            enforcement=enforcement,
        )
        if result.status == "denied":
            denial_body = result.denial_body or {}
            denial_status = result.denial_status or 403
            # 3. on_denied callback — let merchants reshape the canonical body.
            if gate.on_denied is not None:
                custom = await _maybe_await(gate.on_denied(ctx, denial_body))
                if isinstance(custom, dict) and "body" in custom:
                    denial_body = custom.get("body") or denial_body
                    denial_status = custom.get("status", denial_status)
            return CheckoutResult(
                status=denial_status,
                body=denial_body,
                headers={},
                reference_id=ctx.reference_id,
                settled=False,
                settle_phase="gate_denied",
            )
        # Post-allow signer-match enforcement (mirrors the reference Checkout.runGate). The
        # gate's primary /v1/assess call composed a signer_match verdict when a payment signer
        # was extracted; a non-`pass` verdict means the payment signer doesn't match the claimed
        # wallet (or a same-operator linked wallet). Convert it into a 403 here so Checkout
        # enforces wallet-signer binding inline — without this, python settles a mismatch that
        # node blocks. Enforcement applies ONLY to the wallet identity path: on the AIT path the
        # identity is the token (PoP-bound, assess keyed by aip_token, no address-keyed verdict),
        # and on the operator-token path the operator-token wins and signer-match is deliberately
        # not enforced. `gate_instance._client` is request-local (built fresh per call by
        # build_gate_from_policy), so get_signer_verdict reads THIS request's verdict, not a
        # raced shared slot.
        wallet_address = ctx.request.headers.get("x-wallet-address") or ctx.request.headers.get("X-Wallet-Address")
        operator_token_header = ctx.request.headers.get("x-operator-token") or ctx.request.headers.get(
            "X-Operator-Token"
        )
        if (
            result.status == "verified"
            and aip_token is None
            and wallet_address
            and not operator_token_header
            and gate_instance is not None
        ):
            signer_denial = await self._enforce_signer_match(ctx, gate, gate_instance, wallet_address)
            if signer_denial is not None:
                return signer_denial

        # The pairwise account handle rides the same assess response the gate just used.
        from agentscore_commerce.identity.core import project_operator_handle

        ctx.operator_handle = project_operator_handle(ctx.request.assess)

        # Stash ctx.capture_wallet so on_settled can bind the signer wallet to
        # the operator credential without needing a framework-specific context.
        # No-op when the request was wallet-authenticated (no operator_token).
        if operator_token_header:
            self._set_capture_wallet(ctx, operator_token=operator_token_header, gate=gate)

        assess = dict(ctx.request.assess or {})
        assess["identity_status"] = result.status
        ctx.request = CheckoutRequest(
            method=ctx.request.method,
            url=ctx.request.url,
            headers=ctx.request.headers,
            body=ctx.request.body,
            assess=assess,
            raw=ctx.request.raw,
        )
        return None

    async def _enforce_signer_match(
        self,
        ctx: CheckoutContext,
        gate: CheckoutGateConfig,
        gate_instance: Any,
        wallet_address: str,
    ) -> CheckoutResult | None:
        """Convert a non-``pass`` signer_match verdict into a 403, or ``None`` to allow.

        Reads the request-local signer verdict the gate composed (``gate_instance._client``
        is built fresh per request, so this is race-free) and maps a wallet_signer_mismatch /
        wallet_auth_requires_wallet_signing verdict onto the canonical 403 body via
        ``denial_reason_to_body`` — byte-for-byte the SAME path + shape the reference implementation's
        ``Checkout.runGate`` emits (an ``agent_instructions`` recovery container, not the
        standalone ``build_signer_mismatch_body`` helper's ``next_steps`` container). Runs the
        gate's ``on_denied`` reshaper if configured. Returns ``None`` when the verdict is ``pass``
        or absent (no signer was on the request).
        """
        from agentscore_commerce.identity._response import denial_reason_to_body
        from agentscore_commerce.identity.types import DenialReason

        verdict = gate_instance._client.get_signer_verdict(wallet_address)
        signer_match = verdict.signer_match if verdict is not None else None
        if signer_match is None or signer_match.kind == "pass":
            return None

        # Project the verdict onto a DenialReason, mirroring the reference Checkout.runGate.
        if signer_match.kind == "wallet_auth_requires_wallet_signing":
            reason = DenialReason(
                code="wallet_auth_requires_wallet_signing",
                expected_signer=signer_match.claimed_wallet,
                agent_instructions=signer_match.agent_instructions,
            )
        else:
            reason = DenialReason(
                code="wallet_signer_mismatch",
                claimed_operator=signer_match.claimed_operator,
                actual_signer_operator=signer_match.actual_signer_operator,
                expected_signer=signer_match.expected_signer,
                actual_signer=signer_match.actual_signer,
                linked_wallets=signer_match.linked_wallets or [],
                agent_instructions=signer_match.agent_instructions,
            )
        denial_body = denial_reason_to_body(reason)
        denial_status = 403
        if gate.on_denied is not None:
            custom = await _maybe_await(gate.on_denied(ctx, denial_body))
            if isinstance(custom, dict) and "body" in custom:
                denial_body = custom.get("body") or denial_body
                denial_status = custom.get("status", denial_status)
        return CheckoutResult(
            status=denial_status,
            body=denial_body,
            headers={},
            reference_id=ctx.reference_id,
            settled=False,
            settle_phase="gate_denied",
        )

    async def _run_wallet_sanctions_only(self, ctx: CheckoutContext) -> CheckoutResult | None:
        """Wallet OFAC SDN enforcement.

        Runs on settle (payment header present) when either ``self.gate`` is
        None OR a gate is configured but has no ``api_key`` to reach
        ``/v1/assess`` for full policy enforcement (fallback to the
        strict-liability default).

        Env knobs:
          - ``AGENTSCORE_API_KEY`` — required. No key → one-time warning + skip
            (dev/testnet pattern; production should always configure a key).
          - ``AGENTSCORE_BASE_URL`` — optional override for staging/dev API
            (e.g. ``https://api.staging.example`` or ``http://localhost:3002``).

        Stripe SPT (no extractable wallet signer) → skip silently; Stripe runs
        its own OFAC screen on the buyer's Stripe account at customer creation.

        Calls ``/v1/assess`` with the signer wallet as both the primary address
        and the signer block. The API enforces signer-sanctions unconditionally
        when a signer is present (no policy flag needed). Denies on OFAC SDN
        hit; fail-closed on unavailable lookup (strict liability — falsely
        allowing a sanctioned settle is an OFAC violation, falsely denying a
        clean buyer is just bad UX).
        """
        import os

        from agentscore_commerce._warnings import warn_missing_api_key_once

        api_key = (self.gate.api_key if self.gate is not None else None) or os.environ.get("AGENTSCORE_API_KEY")
        if not api_key:
            warn_missing_api_key_once("checkout")
            return None

        from agentscore_commerce.payment.signer import extract_payment_signer, read_x402_payment_header

        x402_header = read_x402_payment_header(ctx.request.headers)
        authorization_header: str | None = None
        for header_key, header_value in ctx.request.headers.items():
            if header_key.lower() == "authorization":
                authorization_header = header_value
                break
        signer = extract_payment_signer(x402_header, authorization_header=authorization_header)
        if signer is None:
            # Stripe SPT path — no wallet signer, no OFAC check possible. Stripe
            # screens its own customer accounts; we have nothing to add here.
            return None

        from agentscore_commerce.api import AgentScore
        from agentscore_commerce.identity._denial import denial_reason_status
        from agentscore_commerce.identity._response import denial_reason_to_body
        from agentscore_commerce.identity.types import DenialReason

        base_url = os.environ.get("AGENTSCORE_BASE_URL")
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = AgentScore(**client_kwargs)
        from agentscore.errors import (
            AgentScoreError,
            InvalidCredentialError,
            TokenExpiredError,
        )

        try:
            result = await client.aassess(
                address=signer.address,
                signer={"address": signer.address, "network": signer.network},
            )
        except (TokenExpiredError, InvalidCredentialError) as err:
            # 401 — credential issues map to invalid_credential. Unusual on the
            # wallet-OFAC-only path (no operator_token) but handled for completeness.
            reason = DenialReason(
                code="invalid_credential" if isinstance(err, InvalidCredentialError) else "token_expired",
                message=str(err),
            )
            return CheckoutResult(
                status=denial_reason_status(reason),
                body=denial_reason_to_body(reason),
                headers={},
                reference_id=ctx.reference_id,
                settled=False,
            )
        except (AgentScoreError, Exception) as err:
            # 503 — API outage or network failure. Fail-closed: strict-liability.
            reason = DenialReason(code="api_error", message=str(err))
            return CheckoutResult(
                status=denial_reason_status(reason),
                body=denial_reason_to_body(reason),
                headers={},
                reference_id=ctx.reference_id,
                settled=False,
            )

        decision = result.get("decision") if isinstance(result, dict) else None
        if decision == "deny":
            decision_reasons = result.get("decision_reasons") or [] if isinstance(result, dict) else []
            reason = DenialReason(
                code="wallet_not_trusted",
                reasons=list(decision_reasons),
                decision=decision,
            )
            return CheckoutResult(
                status=denial_reason_status(reason),
                body=denial_reason_to_body(reason),
                headers={},
                reference_id=ctx.reference_id,
                settled=False,
            )
        return None

    def _coerce_run_gate_result(
        self,
        ctx: CheckoutContext,
        result: Any,
    ) -> CheckoutResult | None:
        """Map a `gate.run_gate` callback's return into a CheckoutResult or pass-through."""
        if result is None:
            return None
        if isinstance(result, dict):
            return CheckoutResult(
                status=int(result.get("status", 403)),
                body=result.get("body", {}) or {},
                headers=result.get("headers") or {},
                reference_id=ctx.reference_id,
                settled=False,
                settle_phase="gate_denied",
            )
        msg = "gate.run_gate must return None (allow) or a dict {status, body, headers?} (deny)"
        raise TypeError(msg)

    def _set_capture_wallet(
        self,
        ctx: CheckoutContext,
        *,
        operator_token: str,
        gate: CheckoutGateConfig,
    ) -> None:
        """Stash ``ctx.capture_wallet`` after a successful gate allow.

        Closes over the resolved operator_token + an AgentScoreCore-backed
        client so ``on_settled`` can link the signer wallet without needing the
        framework-specific request context.
        """
        from agentscore_commerce.identity.core import AgentScoreCore

        async def _capture(
            *,
            wallet_address: str,
            network: Literal["evm", "solana"],
            idempotency_key: str | None = None,
        ) -> None:
            client = AgentScoreCore(
                api_key=gate.api_key,
                base_url=gate.base_url,
                user_agent=gate.user_agent,
            )
            await client.acapture_wallet(
                operator_token=operator_token,
                wallet_address=wallet_address,
                network=network,
                idempotency_key=idempotency_key,
            )

        ctx.capture_wallet = _capture

    async def _handle_zero_settle(self, ctx: CheckoutContext) -> CheckoutResult:
        """Zero-amount carve-out: verify the credential, lift the signer, skip settle.

        CDP rejects EIP-3009 with value=0 (``invalid_payload``) and pympp's tempo
        intents reject ``proof`` payloads; both refuse $0 settles outright. For
        redemption flows that drop the amount to $0, we still want to:

        * authenticate the credential the agent submitted,
        * capture the signer wallet so cross-merchant identity attaches,
        * fire ``on_settled`` so the merchant can persist the order.

        Returns a 200 success path identical to a real settle, except
        ``tx_hash`` is ``None``.
        """
        if has_x402_header(ctx.request.headers):
            # Honor per-request minted recipients on the zero-settle path too (parity with
            # _handle_x402 / node's handleZeroSettle, which runs post-resolveRecipientsForCtx).
            await self._resolve_recipients(ctx)
            verified = await verify_x402_request(
                headers=ctx.request.headers,
                is_cached_address=lambda addr: self._async_is_cached_address(addr, ctx),
                accepted_network=self._x402_base_network or "",
            )
            if not isinstance(verified, VerifyX402RequestSuccess):
                return CheckoutResult(
                    status=verified.status,
                    body=verified.body,
                    headers={},
                    reference_id=ctx.reference_id,
                    settled=False,
                    settle_phase="verify_failed",
                )
            carve = zero_amount_carve_out(
                rail="x402-base",
                payload=verified.payload if isinstance(verified.payload, dict) else None,
            )
            outcome = SettleOutcome(
                rail="x402",
                rail_key=self._x402_rail_key(),
                tx_hash=None,
                signer_address=carve.signer_address,
                signer_network=carve.signer_network,
                payment_response_header=None,
                payment_receipt_header=None,
                raw=verified,
            )
            return await self._build_success(ctx, outcome)
        # MPP $0 carve-out: parse the Authorization header to lift the signer.
        carve = zero_amount_carve_out(
            rail="tempo",
            authorization_header=ctx.request.headers.get("authorization"),
        )
        # No receipt is minted on the $0 path, so the receipt-method derivation in
        # _handle_mppx can't run. Resolve the rails key from the bound credential's
        # signer network instead of the primary-MPP default, so Solana zero-settles
        # don't report under the Tempo key (and vice versa).
        derived_key = (
            self._rails_key_for_mppx_method("solana" if carve.signer_network == "solana" else "tempo")
            if carve.signer_network is not None
            else None
        )
        outcome = SettleOutcome(
            rail="mpp",
            rail_key=derived_key or self._mpp_rail_key(),
            tx_hash=None,
            signer_address=carve.signer_address,
            signer_network=carve.signer_network,
            payment_response_header=None,
            payment_receipt_header=None,
            raw=None,
        )
        return await self._build_success(ctx, outcome)

    async def _async_is_cached_address(self, addr: str, ctx: CheckoutContext | None = None) -> bool:
        # Security: the signed ``payTo`` is agent-controlled (it rides in the X-Payment header the
        # agent constructs). If we accept it blindly, an agent can re-point settlement at a wallet
        # it owns and drain funds the merchant expected to receive. So bind it to the recipient the
        # merchant actually advertised, in precedence order:
        #   1. merchant supplied is_cached_address (per-order minted addresses, e.g. Stripe
        #      multichain) → delegate; the merchant owns the cache that proves THIS payTo was minted
        #      for THIS order.
        #   2. per-request minted recipient (``ctx.recipients["x402_base"]`` from mint_recipients) →
        #      bind to it. A rail can carry BOTH a static recipient AND mint_recipients (the static
        #      recipient is the discovery/sentinel default; the per-request mint is the real payTo).
        #      Binding to the construction-time static set here would reject the legit minted payTo,
        #      so the per-request recipient wins — exactly as the compute-first path already does
        #      (checkout_compute_first ``expected_pay_to = recipients["x402_base"]``).
        #   3. otherwise (static-treasury rail) → accept ONLY the configured x402_base recipient.
        # Mirrors the reference payTo-binding fix.
        if self.is_cached_address is not None:
            out = self.is_cached_address(addr)
            if inspect.isawaitable(out):
                return await out
            return bool(out)
        minted = ctx.recipients.get("x402_base") if ctx is not None else None
        if minted is not None and minted.strip():
            return addr.lower() == minted.lower()
        static_recipient = await self._resolve_static_x402_recipient()
        if static_recipient is None:
            # No x402_base rail / no resolvable static recipient — nothing to bind against. Keep
            # the prior permissive behavior so non-x402 / dynamically-recipient setups are unaffected.
            return True
        return addr.lower() == static_recipient.lower()

    async def _resolve_static_x402_recipient(self) -> str | None:
        """Resolve the configured x402_base rail's recipient to a concrete address (or None).

        Returns ``None`` when there is no ``X402BaseRailSpec`` configured OR when its recipient
        resolves to an empty/blank string. The empty-string ``recipient=""`` is the documented
        per-order-mint sentinel (``build_default_checkout_rails``): the real address is minted per
        request via ``mint_recipients`` and lives in ``ctx.recipients``, never on the spec. Treating
        it as "no static recipient" (→ permissive fallthrough) mirrors the reference implementation's
        ``staticRecipient`` ``r.length > 0`` guard; without it, ``addr.lower() == ""`` rejects EVERY
        honest minted payTo. Resolves a ``RecipientLike`` (str / sync / async factory) so callable
        static recipients still bind.
        """
        from agentscore_commerce.payment.rail_spec import resolve_recipient

        for spec in self.rails.values():
            if isinstance(spec, X402BaseRailSpec):
                resolved = await resolve_recipient(spec.recipient)
                # Empty/blank sentinel → nothing static to bind against.
                return resolved if resolved and resolved.strip() else None
        return None

    async def _mint_reference_id(self, request: CheckoutRequest) -> str:
        if self.mint_reference_id is None:
            return str(uuid.uuid4())
        ctx = CheckoutContext(request=request, reference_id="")
        return str(await _maybe_await(self.mint_reference_id(ctx)))

    async def _resolve_recipients(self, ctx: CheckoutContext) -> dict[str, str]:
        if self.mint_recipients is None:
            return ctx.recipients
        # Idempotent: if a prior call (e.g. pre-compose on the discovery leg)
        # already minted, skip — re-running would mint fresh Stripe PIs / etc.
        if ctx.recipients:
            return ctx.recipients
        ctx.recipients = dict(await _maybe_await(self.mint_recipients(ctx)))
        return ctx.recipients

    async def _handle_x402(self, ctx: CheckoutContext) -> CheckoutResult:
        if ctx.pricing is None or self._x402_base_network is None:
            msg = "Checkout._handle_x402: missing pricing or x402 rail config"
            raise RuntimeError(msg)
        # Resolve per-request recipients BEFORE binding the payTo so a rail that mints a fresh
        # recipient per order (mint_recipients) binds to the minted address, not the construction-
        # time static sentinel. Idempotent (no-op once ctx.recipients is populated); node resolves
        # the same way before dispatch (Checkout.handle → resolveRecipientsForCtx → handleX402).
        await self._resolve_recipients(ctx)
        verified = await verify_x402_request(
            headers=ctx.request.headers,
            is_cached_address=lambda addr: self._async_is_cached_address(addr, ctx),
            accepted_network=self._x402_base_network,
        )
        if not isinstance(verified, VerifyX402RequestSuccess):
            return CheckoutResult(
                status=verified.status,
                body=verified.body,
                headers={},
                reference_id=ctx.reference_id,
                settled=False,
                settle_phase="verify_failed",
            )
        x402_srv = await self._get_x402_server()
        settle = await process_x402_settle(
            x402_server=x402_srv,
            payload=verified.payload,
            resource_config={
                "scheme": "exact",
                "network": verified.signed_network,
                "price": f"${ctx.pricing.amount_usd:.{ctx.pricing.decimals}f}",
                "payTo": verified.signed_pay_to,
                "maxTimeoutSeconds": 300,
            },
            resource_meta={
                "url": _resolve_resource_url(ctx.request),
                "description": "Agent purchase via x402",
                "mimeType": "application/json",
            },
        )
        if not isinstance(settle, ProcessX402SettleSuccess):
            # Map each failure phase to its canonical merchant-facing response:
            # verify_failed → 400 payment_proof_invalid, facilitator_error /
            # settle_failed → 503 payment_provider_unavailable, etc.
            classified = classify_x402_settle_result(settle)
            response_headers = (
                {"Cache-Control": "no-store"} if classified is not None and classified.status >= 500 else {}
            )
            if classified is not None:
                return CheckoutResult(
                    status=classified.status,
                    body={
                        "error": {"code": classified.code, "message": classified.message},
                        "next_steps": classified.next_steps,
                    },
                    headers=response_headers,
                    reference_id=ctx.reference_id,
                    settled=False,
                    settle_phase=settle.phase or "settle_failed",
                )
            return CheckoutResult(
                status=400,
                body=build_validation_error(
                    code="payment_proof_invalid",
                    message=f"Payment failed during settlement (phase: {settle.phase or 'unknown'}).",
                    next_steps={"action": "regenerate_payment_credential"},
                    extra={"phase": settle.phase},
                ),
                headers={},
                reference_id=ctx.reference_id,
                settled=False,
                settle_phase=settle.phase or "settle_failed",
            )
        # Lift the on-chain tx hash from x402 2.9's typed SettleResponse so
        # merchants don't have to dig into raw.settle_result.transaction.
        x402_tx_hash: str | None = None
        settle_obj = getattr(settle, "settle_result", None)
        if settle_obj is not None:
            x402_tx_hash = getattr(settle_obj, "transaction", None) or getattr(settle_obj, "tx_hash", None)
        # The signer is the EIP-3009 ``payload.authorization.from``; extract
        # via the SDK helper so address normalization is consistent.
        from agentscore_commerce.payment.signer import extract_payment_signer, read_x402_payment_header

        x402_signer = extract_payment_signer(
            read_x402_payment_header(ctx.request.headers),
        )
        outcome = SettleOutcome(
            rail="x402",
            rail_key=self._x402_rail_key(),
            tx_hash=x402_tx_hash,
            signer_address=x402_signer.address if x402_signer else None,
            signer_network=x402_signer.network if x402_signer else None,
            payment_response_header=settle.payment_response_header,
            payment_receipt_header=None,
            raw=settle,
        )
        return await self._build_success(ctx, outcome)

    async def _handle_mppx(self, ctx: CheckoutContext) -> CheckoutResult:
        if self.compose_mppx is None:
            msg = "Checkout._handle_mppx: compose_mppx hook not configured"
            raise RuntimeError(msg)
        # Resolve per-request recipients BEFORE composing so a mint_recipients-based MPP merchant
        # sees ctx.recipients populated in both compose_mppx and on_settled on the settle leg.
        # Idempotent (no-op once ctx.recipients is populated); matches the sibling handlers
        # (_handle_x402 / _handle_zero_settle / _emit_402) and node, which resolves before dispatch
        # (Checkout.handle -> resolveRecipientsForCtx -> handleMppx).
        await self._resolve_recipients(ctx)
        composed: MppxComposeOutcome = await _maybe_await(self.compose_mppx(ctx))
        if composed.status == 200:
            receipt_method: str | None = None
            raw_receipt = composed.raw.get("receipt") if isinstance(composed.raw, dict) else None
            if isinstance(raw_receipt, dict):
                m = raw_receipt.get("method")
                if isinstance(m, str):
                    receipt_method = m
            elif raw_receipt is not None:
                m = getattr(raw_receipt, "method", None)
                if isinstance(m, str):
                    receipt_method = m
            derived_key = self._rails_key_for_mppx_method(receipt_method) if receipt_method is not None else None
            outcome = SettleOutcome(
                rail="mpp",
                rail_key=derived_key or composed.rail_key or self._mpp_rail_key(),
                tx_hash=composed.tx_hash,
                signer_address=composed.signer_address,
                signer_network=composed.signer_network,
                payment_response_header=composed.payment_response_header,
                payment_receipt_header=composed.payment_receipt_header
                or extract_mppx_receipt_header_from_raw(composed.raw),
                raw=composed.raw,
            )
            return await self._build_success(ctx, outcome)
        # _handle_mppx is only invoked when an ``Authorization: Payment`` header
        # was present, so a 402 here means mppx REJECTED the credential. Try to
        # classify the swallowed inner error (e.g. Tempo ``KeyNotFound``) into a
        # typed envelope agents can route on; fall back to the generic
        # ``payment_proof_invalid`` regenerate hint otherwise.
        classified = classify_mppx_failure(composed.failure_reason)
        if classified is not None:
            return CheckoutResult(
                status=classified.status,
                body=build_validation_error(
                    code=classified.code,
                    message=classified.message,
                    next_steps=classified.next_steps,
                    extra=classified.extra or None,
                ),
                headers=dict(composed.headers or {}),
                reference_id=ctx.reference_id,
                settled=False,
                settle_phase="verify_failed",
            )
        # mppx already emitted a fresh challenge (in ``composed.headers``), so
        # return it as a 402 the agent re-pays against, not a dead-end 400.
        # x402/MPP clients version-route on the status code: a 402 triggers a
        # retry with a new credential, a 400 aborts.
        return CheckoutResult(
            status=402,
            body=build_validation_error(
                code="payment_proof_invalid",
                message="MPP credential rejected; regenerate from the fresh 402 challenge and retry.",
                next_steps={"action": "regenerate_payment_credential"},
            ),
            headers=dict(composed.headers or {}),
            reference_id=ctx.reference_id,
            settled=False,
            settle_phase="verify_failed",
        )

    async def _emit_fresh_challenge(self, ctx: CheckoutContext) -> CheckoutResult:
        """Emit the discovery-leg 402.

        pre_validate + pricing already ran in the main flow before this is
        reached; idempotent on already-computed pricing / resolved recipients
        (``_emit_402`` resolves recipients), so it primes nothing twice.
        """
        if ctx.pricing is None:
            ctx.pricing = await _maybe_await(self.compute_pricing(ctx))
        mppx_headers: dict[str, str] = {}
        if self.compose_mppx is not None:
            try:
                pre_composed = await _maybe_await(self.compose_mppx(ctx))
                if pre_composed.status == 402:
                    mppx_headers = dict(pre_composed.headers or {})
            except Exception:  # noqa: S110
                # The MPP challenge is optional; the 402 still goes out with
                # whatever rails resolved. A junk credential in the raw request
                # can make compose raise here, which is why it is best-effort.
                pass
        return await self._emit_402(ctx, mppx_headers=mppx_headers)

    def _strip_payment_headers(self, request: CheckoutRequest) -> CheckoutRequest:
        """Return a copy of the request with payment-credential headers removed.

        Re-entering handle() with it treats the request as a discovery
        (no-credential) request: pre_validate + pricing + minting + compose run
        their fresh path, and the gate/assess and settle are skipped. Turns a
        malformed-credential request into a clean 402 re-challenge. The native
        request (``raw``) is stripped in lockstep with ``headers`` so hooks that
        read ``ctx.request.raw`` (e.g. ``mint_multichain_recipients``, which
        parses the MPP credential off the raw ``Authorization: Payment`` header)
        also see a discovery leg instead of throwing on the junk credential.
        """
        headers = {
            k: v
            for k, v in request.headers.items()
            if not (
                k.lower() in ("payment-signature", "x-payment")
                or (k.lower() == "authorization" and v.startswith("Payment "))
            )
        }
        return dataclasses.replace(request, headers=headers, raw=_strip_payment_headers_from_raw(request.raw))

    async def _emit_402(
        self,
        ctx: CheckoutContext,
        mppx_headers: dict[str, str] | None = None,
    ) -> CheckoutResult:
        if ctx.pricing is None:
            msg = "Checkout._emit_402: pricing not computed"
            raise RuntimeError(msg)
        try:
            await self._resolve_recipients(ctx)
        except CheckoutValidationError as err:
            return CheckoutResult(
                status=err.status,
                body=build_validation_error(
                    code=err.code,
                    message=err.message,
                    next_steps={"action": err.action, "user_message": err.message},
                    extra=err.extra,
                ),
                headers={},
                reference_id=ctx.reference_id,
                settled=False,
                settle_phase="mint_recipients_failed",
            )
        emit_rails = _apply_recipient_overrides(self.rails, ctx.recipients)

        # Auto-drop stripe when priced below Stripe's $0.50 USD minimum so the
        # emitted accepted_methods + how_to_pay stay consistent with what the
        # mppx compose layer will actually accept (see build_mppx_compose_rails).
        # Without this, the 402 body advertises a stripe rail that has no
        # matching WWW-Authenticate challenge — agents see it offered but any
        # SPT pay attempt fails. The compose-time auto-drop emits the
        # user-facing warn; here we just strip the slot from the discovery body.
        if Decimal(str(ctx.pricing.amount_usd)) < STRIPE_MIN_CHARGE_USD and "stripe" in emit_rails:
            emit_rails = {k: v for k, v in emit_rails.items() if k != "stripe"}

        accepted = await build_accepted_methods(
            tempo=_pick(emit_rails, "tempo", TempoRailSpec),
            x402_base=_pick(emit_rails, "x402_base", X402BaseRailSpec),
            solana_mpp=_pick(emit_rails, "solana_mpp", SolanaMppRailSpec),
            stripe=_pick(emit_rails, "stripe", StripeRailSpec),
        )
        how_to_pay_rails: dict[str, TempoRailSpec | X402BaseRailSpec | SolanaMppRailSpec | StripeRailSpec] = {
            k: v
            for k, v in emit_rails.items()
            if isinstance(v, (TempoRailSpec, X402BaseRailSpec, SolanaMppRailSpec, StripeRailSpec))
        }
        pricing_decimals = ctx.pricing.decimals
        how_to_pay = await build_how_to_pay(
            url=self.url,
            retry_body_json=str(ctx.request.body),
            total_usd=f"{ctx.pricing.amount_usd:.{pricing_decimals}f}",
            rails=how_to_pay_rails,
            decimals=pricing_decimals,
            # Merchants without an identity-bearing policy flag get clean commands
            # without an X-Operator-Token header — agents don't need one to satisfy
            # the always-on wallet OFAC enforcement default.
            op_token_placeholder=None if not self._has_identity_gate() else "<your_opc_token>",
        )
        pricing_block = ctx.pricing.block or build_pricing_block(
            subtotal_cents=ctx.pricing.amount_usd * 100,
            currency=ctx.pricing.currency,
            decimals=pricing_decimals,
        )
        # Build x402 accepts BEFORE the body so they appear both in the rich body
        # (agents read JSON) AND in the PAYMENT-REQUIRED header (x402-spec clients).
        x402_accepts: list[Any] = []
        x402_resource: dict[str, Any] | None = None
        x402_network = self._x402_base_network
        if self._x402_server_available() and x402_network:
            from agentscore_commerce.payment.x402_server import build_x402_accepts_for_402

            base_spec = next(
                (spec for spec in emit_rails.values() if isinstance(spec, X402BaseRailSpec)),
                None,
            )
            if base_spec is not None:
                recipient = await _resolve_recipient_value(base_spec.recipient)
                try:
                    x402_srv = await self._get_x402_server()
                    x402_accepts = list(
                        build_x402_accepts_for_402(
                            x402_srv,
                            network=x402_network,
                            price=f"${ctx.pricing.amount_usd:.{pricing_decimals}f}",
                            pay_to=recipient,
                            max_timeout_seconds=300,
                        )
                    )
                    x402_resource = {"url": _resolve_resource_url(ctx.request), "mimeType": "application/json"}
                    if self.resource_info:
                        x402_resource.update(self.resource_info)
                except Exception:
                    # Facilitator/scheme build failure: drop x402 from accepts but
                    # keep other rails in the body. Merchant logs internally.
                    x402_accepts = []

        # Pre-advertise wallet-mode signer constraint when the request shows
        # wallet intent. Saves agents a round trip: they learn required_signer
        # + linked_wallets at discovery instead of at the 403 on retry.
        identity_metadata = _resolve_identity_metadata(ctx)

        # Enrich the declared Bazaar discovery extension with the request method +
        # route so info.input.method (required by the v2 discovery schema) and
        # routeTemplate populate, matching the reference x402 server flow.
        from urllib.parse import urlparse

        from agentscore_commerce.discovery.bazaar import enrich_bazaar_discovery_extensions

        request_path = urlparse(_resolve_resource_url(ctx.request)).path or ctx.request.url
        enriched_extensions = enrich_bazaar_discovery_extensions(
            self.discovery_extensions, method=ctx.request.method, path=request_path
        )

        body = build_402_body(
            accepted_methods=accepted,
            agent_instructions=build_agent_instructions(how_to_pay=how_to_pay),
            identity_metadata=identity_metadata,
            pricing=pricing_block,
            amount_usd=f"{ctx.pricing.amount_usd:.{pricing_decimals}f}",
            retry_body=ctx.request.body,
            # Merchants without an identity-bearing gate get a clean 402: no
            # AgentScore-identity bootstrap describing a verification flow they
            # don't run. Wallet OFAC (the always-on default) doesn't need it. When
            # the merchant accepts AIP, advertise the agent_identity path too
            # (AgentScore's own issuer is always trusted, so this fires even with
            # no external issuers).
            agent_memory=first_encounter_agent_memory(
                first_encounter=self._has_identity_gate(),
                aip_trusted_issuers=(
                    _aip_trusted_issuer_set(self.gate.aip)
                    if self.gate is not None and self.gate.aip is not None
                    else None
                ),
            ),
            product=ctx.pricing.product,
            extra=ctx.pricing.body_extras,
            x402=X402PaymentRequired(
                version=2,
                accepts=x402_accepts,
                resource=x402_resource,
                extensions=enriched_extensions or None,
            )
            if x402_accepts
            else None,
        )

        x402_kwargs: dict[str, Any] | None = None
        if x402_accepts:
            x402_kwargs = {
                "x402_version": 2,
                "accepts": x402_accepts,
                "resource": x402_resource,
                "extensions": enriched_extensions or None,
            }

        respond = respond_402(
            mppx_challenge_headers=mppx_headers or {},
            body=body,
            x402=x402_kwargs,
        )
        return CheckoutResult(
            status=respond.status,
            body=respond.body,
            headers=respond.headers,
            reference_id=ctx.reference_id,
            settled=False,
        )

    async def _build_success(self, ctx: CheckoutContext, outcome: SettleOutcome) -> CheckoutResult:
        custom_body: dict[str, Any] | None = None
        if self.on_settled is not None:
            result = await _maybe_await(self.on_settled(ctx, outcome))
            if isinstance(result, dict):
                custom_body = result
        body: dict[str, Any] = custom_body if custom_body is not None else {"ok": True}
        body.setdefault("reference_id", ctx.reference_id)
        headers: dict[str, str] = {}
        if outcome.payment_response_header:
            headers["payment-response"] = outcome.payment_response_header
        if outcome.payment_receipt_header:
            headers["payment-receipt"] = outcome.payment_receipt_header
        return CheckoutResult(
            status=200,
            body=body,
            headers=headers,
            reference_id=ctx.reference_id,
            settled=True,
        )


def format_pydantic_errors(err: Any) -> str:
    """Render a pydantic ``ValidationError`` as a clean agent-readable summary.

    Default ``str(err)`` leaks pydantic.dev URLs and library version into the
    response, which agents shouldn't see. Returns ``"<loc>: <msg>; ..."`` joined
    with semicolons. Accepts any object with a callable ``.errors()`` method
    returning ``[{"loc": (...), "msg": ...}, ...]``.
    """
    errors_fn = getattr(err, "errors", None)
    if not callable(errors_fn):
        return str(err)
    parts: list[str] = []
    for e in errors_fn():
        loc = ".".join(str(p) for p in e.get("loc", ())) or "body"
        parts.append(f"{loc}: {e.get('msg', '')}")
    return "; ".join(parts)


def validation_envelope(
    *,
    code: str,
    message: str,
    action: str = "fix_request",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Framework-neutral 4xx envelope (``{error, next_steps, agent_instructions}``).

    Returns the body dict; merchants wrap in their framework's JSON response.
    The per-framework :func:`validation_response_*` helpers do this for you.
    """
    return build_validation_error(
        code=code,
        message=message,
        next_steps={"action": action, "user_message": message},
        extra=extra,
    )


def validation_response_fastapi(
    *,
    code: str,
    message: str,
    action: str = "fix_request",
    status: int = 400,
    extra: dict[str, Any] | None = None,
) -> Any:
    """FastAPI / Starlette one-liner for the canonical 4xx envelope."""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        content=validation_envelope(code=code, message=message, action=action, extra=extra),
        status_code=status,
    )


def validation_response_flask(
    *,
    code: str,
    message: str,
    action: str = "fix_request",
    status: int = 400,
    extra: dict[str, Any] | None = None,
) -> Any:
    """Flask one-liner; returns a ``flask.Response``."""
    from flask import jsonify

    resp = jsonify(validation_envelope(code=code, message=message, action=action, extra=extra))
    resp.status_code = status
    return resp


def validation_response_django(
    *,
    code: str,
    message: str,
    action: str = "fix_request",
    status: int = 400,
    extra: dict[str, Any] | None = None,
) -> Any:
    """Django one-liner; returns a ``django.http.JsonResponse``."""
    from django.http import JsonResponse

    return JsonResponse(
        validation_envelope(code=code, message=message, action=action, extra=extra),
        status=status,
    )


def validation_response_aiohttp(
    *,
    code: str,
    message: str,
    action: str = "fix_request",
    status: int = 400,
    extra: dict[str, Any] | None = None,
) -> Any:
    """Aiohttp one-liner; returns an ``aiohttp.web.Response``."""
    from aiohttp import web

    return web.json_response(
        validation_envelope(code=code, message=message, action=action, extra=extra),
        status=status,
    )


def validation_response_sanic(
    *,
    code: str,
    message: str,
    action: str = "fix_request",
    status: int = 400,
    extra: dict[str, Any] | None = None,
) -> Any:
    """Sanic one-liner; returns a ``sanic.response.HTTPResponse``."""
    from sanic.response import json as sanic_json

    return sanic_json(
        validation_envelope(code=code, message=message, action=action, extra=extra),
        status=status,
    )


async def _resolve_recipient_value(r: RecipientLike) -> str:
    from agentscore_commerce.payment.rail_spec import resolve_recipient

    return await resolve_recipient(r)


def _pick(rails: dict[str, CheckoutRailSpec], key: str, expected: type) -> Any:
    """Return ``rails[key]`` when it's an instance of ``expected``, else ``None``."""
    spec = rails.get(key)
    return spec if isinstance(spec, expected) else None


def _apply_recipient_overrides(
    rails: dict[str, CheckoutRailSpec],
    overrides: dict[str, str],
) -> dict[str, CheckoutRailSpec]:
    """Apply per-call recipient overrides (from ``mint_recipients``) to rail specs.

    Returns a new dict; original rails dict is not mutated. Stripe rails are
    passed through unchanged (no on-chain recipient; they use ``profile_id``).

    Drop-empty: when a merchant declares rails with sentinel empty-string
    recipients (the per-order-mint pattern — e.g. Stripe-multichain merchants
    that mint a fresh deposit address per request) and ``mint_recipients`` only
    returns addresses for some rails, drop rails that resolve to an empty
    recipient — those weren't actually minted for this request and shouldn't be
    advertised in the 402.
    """
    from dataclasses import replace

    out: dict[str, CheckoutRailSpec] = {}
    for key, spec in rails.items():
        if isinstance(spec, StripeRailSpec):
            out[key] = spec
            continue
        override = overrides.get(key)
        spec_recipient = getattr(spec, "recipient", None)
        final_recipient = override if override is not None else spec_recipient
        if final_recipient is None or final_recipient == "":
            continue
        if override is not None:
            out[key] = replace(spec, recipient=override)
        else:
            out[key] = spec
    return out


def build_aip_trusted_issuers(external_issuers: list[str] | None = None) -> list[str]:
    """The effective AIP trusted-issuer list.

    AgentScore's canonical issuer (ALWAYS trusted) plus any external issuers, de-duped
    after canonicalization. Use this for the ``agent_memory`` hint and any presentation
    surface (llms.txt / mpp.json / skill.md) that advertises AIP acceptance, so a merchant
    relying solely on AgentScore AITs (no external issuers) still advertises the
    ``agent_identity`` path. Trust enforcement itself lives in ``JwksCache``, which merges
    the canonical issuer independently.
    """
    out = [AGENTSCORE_CANONICAL_ISSUER, *(external_issuers or [])]
    # De-dupe on canonical form so an explicit ``https://www.agentscore.com`` (or
    # trailing-slash variant) doesn't double up; keep the first-seen original string
    # for each canonical key.
    seen: set[str] = set()
    deduped: list[str] = []
    for iss in out:
        key = canonicalize_issuer(iss) or iss
        if key not in seen:
            seen.add(key)
            deduped.append(iss)
    return deduped


__all__ = [
    "Checkout",
    "CheckoutContext",
    "CheckoutRailSpec",
    "CheckoutRequest",
    "CheckoutResult",
    "MppxComposeOutcome",
    "PricingResult",
    "Respond402Result",
    "SettleOutcome",
    "build_aip_trusted_issuers",
]
