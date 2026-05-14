"""High-level Checkout orchestrator — composes 402-emit + verify+settle.

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
  rails — Stripe SPT settles via the same ``compose_mppx`` hook.
* **Multi-rail merchants** configure all of the above; the agent picks the rail.

Three flexibility axes — every combination is supported:

* **x402 only / MPP only / both** — Checkout works with ``x402_server`` alone,
  ``compose_mppx`` alone, or both. Whichever payment header arrives is dispatched
  to the configured handler; the other path is simply absent.
* **Self-custody / Stripe / mixed** — rails dict is the single source of truth.
  Listing ``StripeRailSpec`` makes Stripe SPT an acceptable rail; omitting it
  makes the merchant chain-only. Mixing freely is the default.
* **Gated / ungated identity** — ``CheckoutRequest.assess`` is optional. Merchants
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
        # compose_mppx omitted — x402-only API merchants don't need MPP rails
    )

``handle(request)`` returns a framework-neutral :class:`CheckoutResult`
(``body`` + ``headers`` + ``status`` + ``reference_id`` + ``settled``); the
merchant wraps it in their framework's response shape.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from agentscore_commerce.challenge.accepted_methods import build_accepted_methods
from agentscore_commerce.challenge.agent_instructions import build_agent_instructions
from agentscore_commerce.challenge.agent_memory import first_encounter_agent_memory
from agentscore_commerce.challenge.body import build_402_body
from agentscore_commerce.challenge.how_to_pay import build_how_to_pay
from agentscore_commerce.challenge.pricing import PricingBlock, build_pricing_block
from agentscore_commerce.challenge.respond_402 import Respond402Result, respond_402
from agentscore_commerce.challenge.validation_error import build_validation_error
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
    process_x402_settle,
)
from agentscore_commerce.payment.x402_validation import (
    VerifyX402RequestSuccess,
    verify_x402_request,
)

CheckoutRailSpec: TypeAlias = (
    TempoRailSpec | X402BaseRailSpec | SolanaMppRailSpec | StripeRailSpec | TempoSessionRailSpec
)


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
    your ``compose_mppx`` hook needs to call ``mppx.compose(...)(raw_request)`` —
    pympp's compose binds to the raw HTTP request, so the orchestrator forwards
    this through unchanged."""


@dataclass
class PricingResult:
    """Output of :attr:`Checkout.compute_pricing` — per-request pricing."""

    amount_usd: float
    """Total to charge in USD (or the upper bound, for ``mode="upto"`` rails)."""
    currency: str = "USD"
    block: PricingBlock | None = None
    """Optional pre-built :class:`PricingBlock`. When omitted, Checkout builds a minimal
    block from ``amount_usd`` so the 402 body always carries pricing metadata."""


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


@dataclass
class SettleOutcome:
    """Surface passed to :attr:`Checkout.on_settled` after a payment lands."""

    rail: Literal["x402", "mpp"]
    """Which protocol settled. ``"mpp"`` covers tempo / tempo-session / solana / stripe-spt."""
    payment_response_header: str | None = None
    """The ``PAYMENT-RESPONSE`` header to echo (x402 success path). ``None`` for MPP."""
    raw: Any = None
    """The underlying settle result (``ProcessX402SettleSuccess`` or merchant-supplied
    MPP compose result) for merchants that need to inspect tx hash / facilitator details."""


@dataclass
class MppxComposeOutcome:
    """Result a ``compose_mppx`` hook returns when handling an MPP credential.

    ``status=200`` means pympp validated the ``Authorization: Payment`` credential
    and the settlement landed — Checkout runs ``on_settled`` and returns success.

    ``status=402`` means pympp emitted a 402 (no credential / invalid credential).
    Checkout layers its rich body on top of pympp's WWW-Authenticate header and
    optional x402 PAYMENT-REQUIRED, returning the composed 402.
    """

    status: Literal[200, 402]
    headers: dict[str, str] = field(default_factory=dict)
    """For ``status=402``: the WWW-Authenticate (+ any other) headers pympp's
    compose emitted. Checkout merges these into the final 402 response."""
    payment_response_header: str | None = None
    """For ``status=200``: optional PAYMENT-RESPONSE header echoed to the agent."""
    raw: Any = None
    """The underlying pympp compose result for ``on_settled`` introspection."""


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


PricingFn: TypeAlias = Callable[["CheckoutContext"], "Awaitable[PricingResult] | PricingResult"]
RecipientsFn: TypeAlias = Callable[["CheckoutContext"], "Awaitable[dict[str, str]] | dict[str, str]"]
ReferenceIdFn: TypeAlias = Callable[["CheckoutContext"], "Awaitable[str] | str"]
OnSettledFn: TypeAlias = Callable[
    ["CheckoutContext", "SettleOutcome"],
    "Awaitable[dict[str, Any] | None] | dict[str, Any] | None",
]
ComposeMppxFn: TypeAlias = Callable[["CheckoutContext"], "Awaitable[MppxComposeOutcome] | MppxComposeOutcome"]
IsCachedAddressFn: TypeAlias = Callable[[str], "Awaitable[bool] | bool"]


def _has_x402_header(headers: dict[str, str]) -> bool:
    lower = {k.lower(): v for k, v in headers.items()}
    return bool(lower.get("payment-signature") or lower.get("x-payment"))


def _has_mppx_header(headers: dict[str, str]) -> bool:
    lower = {k.lower(): v for k, v in headers.items()}
    auth = lower.get("authorization") or ""
    return auth.startswith("Payment ")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class Checkout:
    """High-level agent-commerce orchestrator.

    Composes :func:`build_accepted_methods`, :func:`build_how_to_pay`,
    :func:`respond_402`, :func:`verify_x402_request`, and
    :func:`process_x402_settle` into a single ``await checkout.handle(request)``
    call. For MPP rails, the merchant supplies a ``compose_mppx`` hook that
    drives pympp's ``compose()`` (intent dispatch is merchant-owned because
    pympp binds intents per instance).

    Required:

    * ``rails`` — rail-key → ``*RailSpec``. The same map every other helper
      consumes (:func:`build_accepted_methods`, :func:`build_how_to_pay`,
      :func:`create_mppx_server`).
    * ``url`` — absolute URL of the checkout endpoint.
    * ``compute_pricing`` — async/sync function ``(ctx) -> PricingResult``.

    Optional:

    * ``x402_server`` — built via :func:`create_x402_server`. Pair it with an
      ``X402BaseRailSpec`` in ``rails["x402_base"]``; the CAIP-2 network is
      read from ``rail.network`` (defaults to ``eip155:8453``).
    * ``compose_mppx`` — async/sync function ``(ctx) -> MppxComposeOutcome``.
      Required when the merchant accepts ``Authorization: Payment`` credentials
      (Tempo / Solana MPP / Stripe SPT). Omit for x402-only merchants.
    * ``mint_recipients`` — async/sync function ``(ctx) -> dict[rail_key, address]``.
      Use for Stripe-multichain merchants who mint per-order deposit addresses.
      When omitted, every rail's recipient is taken from its ``*RailSpec``.
    * ``mint_reference_id`` — async/sync function ``(ctx) -> str``. Default is
      :func:`uuid.uuid4`. Goods merchants typically mint an order id here.
    * ``on_settled`` — async/sync function ``(ctx, outcome) -> dict | None``. Runs
      after the payment settles successfully. Goods merchants persist the order
      here. API merchants can return the inline API response body — when the hook
      returns a dict, it becomes the 200 response body (with ``reference_id``
      auto-merged).
    * ``is_cached_address`` — pass when the merchant mints per-order addresses
      so :func:`verify_x402_request` can confirm the ``payTo`` was minted by
      this merchant. Default permissive (accepts any payTo) for static-treasury
      merchants.
    """

    def __init__(
        self,
        *,
        rails: dict[str, CheckoutRailSpec],
        url: str,
        compute_pricing: PricingFn,
        x402_server: Any = None,
        compose_mppx: ComposeMppxFn | None = None,
        mint_recipients: RecipientsFn | None = None,
        mint_reference_id: ReferenceIdFn | None = None,
        on_settled: OnSettledFn | None = None,
        is_cached_address: IsCachedAddressFn | None = None,
    ) -> None:
        if x402_server is not None:
            base_spec = rails.get("x402_base")
            if not isinstance(base_spec, X402BaseRailSpec):
                msg = (
                    "Checkout: x402_server requires an X402BaseRailSpec in "
                    "rails['x402_base'] (the rail's `network` field supplies the CAIP-2)."
                )
                raise ValueError(msg)
        self.rails = rails
        self.url = url
        self.compute_pricing = compute_pricing
        self.x402_server = x402_server
        self.compose_mppx = compose_mppx
        self.mint_recipients = mint_recipients
        self.mint_reference_id = mint_reference_id
        self.on_settled = on_settled
        self.is_cached_address = is_cached_address

    @property
    def _x402_base_network(self) -> str | None:
        """CAIP-2 read from ``rails['x402_base'].network`` (or its default).

        Defined only when ``x402_server`` is configured + an ``X402BaseRailSpec`` is
        present in rails; otherwise ``None``.
        """
        if self.x402_server is None:
            return None
        spec = self.rails.get("x402_base")
        if not isinstance(spec, X402BaseRailSpec):
            return None
        return spec.network

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
        ctx.pricing = await _maybe_await(self.compute_pricing(ctx))

        if _has_x402_header(request.headers) and self.x402_server is not None and self._x402_base_network:
            return await self._handle_x402(ctx)

        if _has_mppx_header(request.headers) and self.compose_mppx is not None:
            return await self._handle_mppx(ctx)

        return await self._emit_402(ctx)

    async def _async_is_cached_address(self, addr: str) -> bool:
        if self.is_cached_address is None:
            return True
        out = self.is_cached_address(addr)
        if inspect.isawaitable(out):
            return await out
        return bool(out)

    async def _mint_reference_id(self, request: CheckoutRequest) -> str:
        if self.mint_reference_id is None:
            return str(uuid.uuid4())
        ctx = CheckoutContext(request=request, reference_id="")
        return str(await _maybe_await(self.mint_reference_id(ctx)))

    async def _resolve_recipients(self, ctx: CheckoutContext) -> dict[str, str]:
        if self.mint_recipients is None:
            return {}
        ctx.recipients = dict(await _maybe_await(self.mint_recipients(ctx)))
        return ctx.recipients

    async def _handle_x402(self, ctx: CheckoutContext) -> CheckoutResult:
        if ctx.pricing is None or self._x402_base_network is None:
            msg = "Checkout._handle_x402: missing pricing or x402 rail config"
            raise RuntimeError(msg)
        verified = await verify_x402_request(
            headers=ctx.request.headers,
            is_cached_address=self._async_is_cached_address,
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
        settle = await process_x402_settle(
            x402_server=self.x402_server,
            payload=verified.payload,
            resource_config={
                "scheme": "exact",
                "network": verified.signed_network,
                "price": f"${ctx.pricing.amount_usd}",
                "payTo": verified.signed_pay_to,
                "maxTimeoutSeconds": 300,
            },
            resource_meta={
                "url": ctx.request.url,
                "description": "Agent purchase via x402",
                "mimeType": "application/json",
            },
        )
        if not isinstance(settle, ProcessX402SettleSuccess):
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
        outcome = SettleOutcome(
            rail="x402",
            payment_response_header=settle.payment_response_header,
            raw=settle,
        )
        return await self._build_success(ctx, outcome)

    async def _handle_mppx(self, ctx: CheckoutContext) -> CheckoutResult:
        if self.compose_mppx is None:
            msg = "Checkout._handle_mppx: compose_mppx hook not configured"
            raise RuntimeError(msg)
        composed: MppxComposeOutcome = await _maybe_await(self.compose_mppx(ctx))
        if composed.status == 200:
            outcome = SettleOutcome(
                rail="mpp",
                payment_response_header=composed.payment_response_header,
                raw=composed.raw,
            )
            return await self._build_success(ctx, outcome)
        return await self._emit_402(ctx, mppx_headers=composed.headers)

    async def _emit_402(
        self,
        ctx: CheckoutContext,
        mppx_headers: dict[str, str] | None = None,
    ) -> CheckoutResult:
        if ctx.pricing is None:
            msg = "Checkout._emit_402: pricing not computed"
            raise RuntimeError(msg)
        await self._resolve_recipients(ctx)
        emit_rails = _apply_recipient_overrides(self.rails, ctx.recipients)

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
        how_to_pay = await build_how_to_pay(
            url=self.url,
            retry_body_json=str(ctx.request.body),
            total_usd=str(ctx.pricing.amount_usd),
            rails=how_to_pay_rails,
        )
        pricing_block = ctx.pricing.block or build_pricing_block(
            subtotal_cents=round(ctx.pricing.amount_usd * 100),
            currency=ctx.pricing.currency,
        )
        body = build_402_body(
            accepted_methods=accepted,
            agent_instructions=build_agent_instructions(how_to_pay=how_to_pay),
            pricing=pricing_block,
            amount_usd=str(ctx.pricing.amount_usd),
            retry_body=ctx.request.body,
            agent_memory=first_encounter_agent_memory(first_encounter=True),
        )

        x402_kwargs: dict[str, Any] | None = None
        x402_network = self._x402_base_network
        if self.x402_server is not None and x402_network:
            from agentscore_commerce.payment.x402_server import build_x402_accepts_for_402

            base_spec = emit_rails.get("x402_base")
            if isinstance(base_spec, X402BaseRailSpec):
                recipient = await _resolve_recipient_value(base_spec.recipient)
                x402_kwargs = {
                    "x402_version": 2,
                    "accepts": build_x402_accepts_for_402(
                        self.x402_server,
                        network=x402_network,
                        price=f"${ctx.pricing.amount_usd}",
                        pay_to=recipient,
                        max_timeout_seconds=300,
                    ),
                    "resource": {"url": ctx.request.url, "mimeType": "application/json"},
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
        return CheckoutResult(
            status=200,
            body=body,
            headers=headers,
            reference_id=ctx.reference_id,
            settled=True,
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
    passed through unchanged (no on-chain recipient — they use ``profile_id``).
    """
    if not overrides:
        return rails
    out: dict[str, CheckoutRailSpec] = {}
    for key, spec in rails.items():
        override = overrides.get(key)
        if override is None or isinstance(spec, StripeRailSpec):
            out[key] = spec
            continue
        from dataclasses import replace

        out[key] = replace(spec, recipient=override)
    return out


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
]
