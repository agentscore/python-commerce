"""``compute_first_checkout`` — variable-cost pay-per-result merchant helper.

Mirrors node-commerce ``src/checkout_compute_first.ts``. Uses compute-first +
exact-x402 (no upto, no Permit2, no Settlement-Overrides).

Flow (per request):

1. PROBE leg (no payment header)
   - Validate input
   - Look up cache by content-hash of the request body
   - On cache miss: run ``run_work(body, ctx)``
     - 0 results → return 200 immediately with ``no_charge`` envelope (no 402)
     - Else → cache ``{body, price_cents}`` keyed by body hash → emit 402 with
       EXACT price (``actual_results * unit_price_cents``) on every advertised rail
   - On cache hit: emit 402 with cached price

2. SETTLE leg (``X-Payment`` / ``Authorization: Payment`` header attached)
   - Look up cache by re-hashing the same body
   - Cache miss → 400 ``stale_quote`` with ``next_steps.action: "re_probe"``
   - x402 path → :func:`verify_x402_request` + :func:`process_x402_settle` with
     ``scheme="exact"``
   - MPP path → ``compose_mppx`` callback runs the settle compose
   - Return cached result body in the canonical 200 envelope

Works on every exact-mode rail today (x402-exact Base, ``tempo/charge``,
``solana/charge``, Stripe SPT). The tradeoff vs. upto is that the work runs on
the unpaid probe leg — so rate-limiting is load-bearing (use
``agentscore_commerce.middleware.fastapi.RateLimitMiddleware`` or the
per-framework equivalent).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from agentscore_commerce._mppx_receipt import derive_mppx_receipt_method
from agentscore_commerce.challenge import (
    build_402_body,
    build_accepted_methods,
    build_agent_instructions,
    build_how_to_pay,
    build_pricing_block,
    first_encounter_agent_memory,
)
from agentscore_commerce.discovery import build_success_next_steps
from agentscore_commerce.errors import CheckoutValidationError
from agentscore_commerce.payment.amounts import format_usd_cents
from agentscore_commerce.payment.constants import STRIPE_MIN_CHARGE_USD
from agentscore_commerce.payment.payment_header import has_mppx_header, has_x402_header
from agentscore_commerce.payment.rail_spec import (
    SolanaMppRailSpec,
    StripeRailSpec,
    TempoRailSpec,
    X402BaseRailSpec,
    resolve_recipient,
)
from agentscore_commerce.payment.signer import extract_payment_signer, read_x402_payment_header
from agentscore_commerce.payment.wwwauthenticate import payment_required_header
from agentscore_commerce.payment.x402_server import build_x402_accepts_for_402
from agentscore_commerce.payment.x402_settle import ProcessX402SettleSuccess, process_x402_settle
from agentscore_commerce.payment.x402_validation import VerifyX402RequestSuccess, verify_x402_request
from agentscore_commerce.quote_cache import DEFAULT_TTL_MS, QuoteCache, create_quote_cache

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class WorkOutcome:
    """Output of the per-request work hook.

    ``result_count`` is the number of billable units (results, tokens, bytes,
    …) used to compute the exact price ``unit_price_cents * result_count``
    advertised in the 402. Zero short-circuits the probe to 200 no-charge.

    ``body`` is the response payload returned to the buyer on the settle leg.
    Cached verbatim and served on retry; the merchant does NOT re-run work.
    """

    result_count: int
    body: dict[str, Any]


@dataclass
class MintedRecipients:
    """Per-rail recipient addresses minted by the merchant for this request."""

    tempo: str | None = None
    x402_base: str | None = None
    solana_mpp: str | None = None

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.tempo:
            out["tempo"] = self.tempo
        if self.x402_base:
            out["x402_base"] = self.x402_base
        if self.solana_mpp:
            out["solana_mpp"] = self.solana_mpp
        return out


@dataclass
class ComputeFirstRequest:
    """Framework-neutral HTTP request input.

    Built by the per-framework adapter method (``handle_fastapi`` /
    ``handle_flask`` / …).
    """

    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    raw: Any = None


@dataclass
class ComputeFirstWorkContext:
    request: ComputeFirstRequest


@dataclass
class ComputeFirstMintContext:
    request: ComputeFirstRequest
    body: dict[str, Any]
    price_cents: int


@dataclass
class ComputeFirstMppContext:
    request: ComputeFirstRequest
    cached_body: dict[str, Any]
    price_cents: int
    price_usd: str
    recipients: MintedRecipients


@dataclass
class ComputeFirstSettledContext:
    request: ComputeFirstRequest
    rail: str  # 'x402' | 'mpp'
    cached_body: dict[str, Any]
    price_cents: int
    price_usd: str
    recipients: MintedRecipients
    mpp_method: str | None = None
    signer_address: str | None = None
    signer_network: str | None = None  # 'evm' | 'solana'
    payment_intent_id: str | None = None


@dataclass
class ComputeFirstMppResult:
    """Return shape for the ``compose_mppx`` callback.

    On 200, set ``raw`` to the mppx compose result so the helper can extract
    the receipt method. On 402, set ``headers`` to mppx's challenge headers
    (typically ``mppx_challenge_headers(result)``).
    """

    status: int
    raw: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    tx_hash: str | None = None
    signer_address: str | None = None
    signer_network: str | None = None  # 'evm' | 'solana'


@dataclass
class SuccessBodyArgs:
    reference_id: str
    endpoint: str
    charged_usd: str
    rail: str
    cached_body: dict[str, Any]
    payment_intent_id: str | None = None
    signer_address: str | None = None
    signer_network: str | None = None


@dataclass
class ComputeFirstRails:
    tempo: TempoRailSpec | None = None
    x402_base: X402BaseRailSpec | None = None
    solana_mpp: SolanaMppRailSpec | None = None
    stripe: StripeRailSpec | None = None


def _decimals_for_unit(unit_price_cents: float) -> int:
    """Auto-derive dollar precision from the unit price's fractional digits."""
    if float(unit_price_cents).is_integer():
        return 2
    s = repr(unit_price_cents)
    dot = s.find(".")
    if dot == -1:
        return 2
    frac = len(s) - dot - 1
    return 2 + frac


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_success_body(app_url: str) -> Callable[[SuccessBodyArgs], dict[str, Any]]:
    def _build(args: SuccessBodyArgs) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": args.reference_id,
            "endpoint": args.endpoint,
            "created_at": _iso_now(),
            "payment_status": "completed",
            "charged_usd": args.charged_usd,
            "rail": args.rail,
        }
        if args.payment_intent_id:
            out["payment_intent_id"] = args.payment_intent_id
        if args.signer_address and args.signer_network:
            out["signer"] = {"address": args.signer_address, "network": args.signer_network}
        out["result"] = args.cached_body
        out["next_steps"] = build_success_next_steps(order_status_url=f"{app_url}/health")
        out["agent_memory"] = first_encounter_agent_memory(first_encounter=True)
        return out

    return _build


# Module-level so the missing-API-key warning fires at most once across all
# ComputeFirstCheckout instances in a process — matches the Checkout class's
# `_WARNED_NO_API_KEY`. Multi-endpoint apps would otherwise log the same
# warning N times on first traffic.
_WARNED_NO_API_KEY = False


class ComputeFirstCheckout:
    """Variable-cost pay-per-result orchestrator.

    See the module docstring for the full flow.

    Construct once at module load with merchant config + hooks; reuse across
    requests. Each request runs through :meth:`handle` (framework-neutral)
    or one of the per-framework adapters (``handle_fastapi``, ``handle_flask``,
    ``handle_aiohttp``, ``handle_sanic``, ``handle_django``).
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        unit_price_cents: float,
        rails: ComputeFirstRails,
        x402_server: Any,
        run_work: Callable[[dict[str, Any], ComputeFirstWorkContext], Awaitable[WorkOutcome]],
        decimals: int | None = None,
        compose_mppx: Callable[[ComputeFirstMppContext], Awaitable[ComputeFirstMppResult]] | None = None,
        on_settled: Callable[[ComputeFirstSettledContext], Awaitable[None]] | None = None,
        validate_input: Callable[[dict[str, Any]], None] | None = None,
        mint_recipients: Callable[[ComputeFirstMintContext], Awaitable[MintedRecipients]] | None = None,
        cache: QuoteCache | None = None,
        cache_ttl_ms: int = DEFAULT_TTL_MS,
        app_url: str | None = None,
        build_success_body: Callable[[SuccessBodyArgs], dict[str, Any]] | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.unit_price_cents = unit_price_cents
        self.decimals = decimals if decimals is not None else _decimals_for_unit(unit_price_cents)
        self.rails = rails
        self.x402_server = x402_server
        self.compose_mppx = compose_mppx
        self.on_settled = on_settled
        self.validate_input = validate_input
        self.run_work = run_work
        self.mint_recipients = mint_recipients
        self.cache = cache or create_quote_cache(ttl_ms=cache_ttl_ms)
        # Derive app URL from the endpoint's origin if not provided.
        if app_url is not None:
            self.app_url = app_url
        else:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            self.app_url = f"{parsed.scheme}://{parsed.netloc}"
        self._build_success_body = build_success_body or _default_success_body(self.app_url)

    # ── core handlers ────────────────────────────────────────────────────────

    async def _mint_and_resolve_recipients(
        self,
        request: ComputeFirstRequest,
        body: dict[str, Any],
        price_cents: int,
    ) -> dict[str, str]:
        minted = MintedRecipients()
        if self.mint_recipients is not None:
            minted = await self.mint_recipients(
                ComputeFirstMintContext(request=request, body=body, price_cents=price_cents)
            )
        out: dict[str, str] = {}
        tempo = minted.tempo or (await resolve_recipient(self.rails.tempo.recipient) if self.rails.tempo else None)
        x402_base = minted.x402_base or (
            await resolve_recipient(self.rails.x402_base.recipient) if self.rails.x402_base else None
        )
        solana = minted.solana_mpp or (
            await resolve_recipient(self.rails.solana_mpp.recipient) if self.rails.solana_mpp else None
        )
        if tempo:
            out["tempo"] = tempo
        if x402_base:
            out["x402_base"] = x402_base
        if solana:
            out["solana_mpp"] = solana
        return out

    async def _emit_402(
        self,
        request: ComputeFirstRequest,
        body: dict[str, Any],
        price_cents: int,
        recipients: dict[str, str],
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        total_usd = format_usd_cents(price_cents, decimals=self.decimals)
        tempo_recipient = recipients.get("tempo")
        x402_base_recipient = recipients.get("x402_base")
        solana_recipient = recipients.get("solana_mpp")

        accepted_rails: dict[str, Any] = {}
        if tempo_recipient and self.rails.tempo is not None:
            accepted_rails["tempo"] = _replace_recipient(self.rails.tempo, tempo_recipient)
        if solana_recipient and self.rails.solana_mpp is not None:
            accepted_rails["solana_mpp"] = _replace_recipient(self.rails.solana_mpp, solana_recipient)
        # Auto-drop stripe when the computed price is below Stripe's $0.50 USD
        # minimum so accepted_methods stays consistent with what build_mppx_compose_rails
        # actually composes (see agentscore_commerce.payment.constants).
        if self.rails.stripe is not None and Decimal(total_usd) >= STRIPE_MIN_CHARGE_USD:
            accepted_rails["stripe"] = self.rails.stripe
        accepted = await build_accepted_methods(**accepted_rails)

        if x402_base_recipient and self.rails.x402_base is not None:
            try:
                resolved = await resolve_recipient(x402_base_recipient)
                x402_entries = build_x402_accepts_for_402(
                    self.x402_server,
                    network=self.rails.x402_base.network or "eip155:8453",
                    price=f"${total_usd}",
                    pay_to=resolved,
                    max_timeout_seconds=300,
                )
                accepted.extend(x402_entries)
            except Exception as exc:
                log.warning(
                    "[%s.compute_first] build_x402_accepts_for_402 failed; dropping x402 from accepts: %s",
                    self.name,
                    exc,
                )

        how_to_pay_rails: dict[str, Any] = {}
        if tempo_recipient and self.rails.tempo is not None:
            how_to_pay_rails["tempo"] = _replace_recipient(self.rails.tempo, tempo_recipient)
        if x402_base_recipient and self.rails.x402_base is not None:
            how_to_pay_rails["x402_base"] = _replace_recipient(self.rails.x402_base, x402_base_recipient)
        if solana_recipient and self.rails.solana_mpp is not None:
            how_to_pay_rails["solana_mpp"] = _replace_recipient(self.rails.solana_mpp, solana_recipient)
        if self.rails.stripe is not None:
            how_to_pay_rails["stripe"] = self.rails.stripe
        how_to_pay = await build_how_to_pay(
            url=self.url,
            retry_body_json=_json_dumps(body),
            total_usd=total_usd,
            decimals=self.decimals,
            rails=how_to_pay_rails,
        )

        pricing = build_pricing_block(subtotal_cents=price_cents, currency="USD", decimals=self.decimals)
        agent_instructions = build_agent_instructions(
            how_to_pay=how_to_pay,
            warnings=[
                (
                    "The quoted price is exact: it was derived from the actual "
                    "number of results returned by the work on the probe leg."
                ),
                (
                    "The merchant cached the result against a hash of this request body. "
                    "Retry with the same body within the quote TTL (default 5 min) to "
                    "settle and receive the cached results; if the quote expires, "
                    "re-probe."
                ),
            ],
        )

        # MPP probe leg: ask compose_mppx to produce mppx's challenge headers
        # (with per-rail `request=<base64 intent>` directives the agent needs
        # to sign). x402-exact still uses PAYMENT-REQUIRED only.
        mpp_challenge_headers: dict[str, str] = {}
        if self.compose_mppx is not None:
            try:
                mpp_recipients = MintedRecipients(
                    tempo=tempo_recipient, x402_base=x402_base_recipient, solana_mpp=solana_recipient
                )
                mpp_result = await self.compose_mppx(
                    ComputeFirstMppContext(
                        request=request,
                        cached_body=body,
                        price_cents=price_cents,
                        price_usd=total_usd,
                        recipients=mpp_recipients,
                    )
                )
                if mpp_result.status == 402 and mpp_result.headers:
                    mpp_challenge_headers = dict(mpp_result.headers)
            except Exception as exc:
                log.warning(
                    "[%s.compute_first] compose_mppx probe-leg failed; dropping MPP rails from 402 challenge: %s",
                    self.name,
                    exc,
                )

        body_402 = build_402_body(
            product={"id": self.name, "name": self.name},
            accepted_methods=accepted,
            pricing=pricing,
            agent_instructions=agent_instructions,
            amount_usd=total_usd,
            currency="USD",
            order_id=None,
            retry_body=body,
        )

        headers = {"Content-Type": "application/json"}
        headers.update(mpp_challenge_headers)
        headers["PAYMENT-REQUIRED"] = payment_required_header(
            x402_version=2, accepts=accepted, resource={"url": self.url}
        )
        return 402, body_402, headers

    async def _enforce_wallet_sanctions(
        self,
        request: ComputeFirstRequest,
        reference_id: str,
    ) -> tuple[int, dict[str, Any], dict[str, str]] | None:
        """Always-on wallet OFAC SDN enforcement for compute-first merchants.

        Mirrors :meth:`Checkout._run_wallet_sanctions_only`. Resolves API key
        from ``AGENTSCORE_API_KEY``, optional base URL from
        ``AGENTSCORE_BASE_URL``, extracts the signer from the payment header,
        calls ``/v1/assess`` with the signer block (no policy). Denies on SDN
        hit or unavailable lookup. Skips silently for Stripe SPT (no wallet
        signer) and when no API key is set (log-once warning).
        """
        import os

        api_key = os.environ.get("AGENTSCORE_API_KEY")
        if not api_key:
            global _WARNED_NO_API_KEY
            if not _WARNED_NO_API_KEY:
                import logging

                logging.getLogger(__name__).warning(
                    f"[{self.name}.compute_first] AGENTSCORE_API_KEY is not set — wallet OFAC SDN "
                    "sanctions are NOT being enforced. Set the env var to enable strict-liability "
                    "protection on settle."
                )
                _WARNED_NO_API_KEY = True
            return None

        from agentscore_commerce.payment.signer import extract_payment_signer, read_x402_payment_header

        x402_header = read_x402_payment_header(request.headers)
        authorization_header: str | None = None
        for header_key, header_value in request.headers.items():
            if header_key.lower() == "authorization":
                authorization_header = header_value
                break
        signer = extract_payment_signer(x402_header, authorization_header=authorization_header)
        if signer is None:
            return None  # Stripe SPT — no wallet signer to screen

        from agentscore_commerce.api import AgentScore

        base_url = os.environ.get("AGENTSCORE_BASE_URL")
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = AgentScore(**client_kwargs)

        try:
            result = await client.aassess(
                address=signer.address,
                signer={"address": signer.address, "network": signer.network},
            )
        except Exception:
            # API outage or network failure — fail-closed (strict-liability).
            return (
                503,
                {
                    "id": reference_id,
                    "endpoint": self.name,
                    "created_at": datetime.now(UTC).isoformat(),
                    "payment_status": "failed",
                    "error": {
                        "code": "api_error",
                        "message": "AgentScore /v1/assess unavailable; settle blocked (strict-liability fail-closed).",
                    },
                },
                {"Content-Type": "application/json"},
            )

        decision = result.get("decision") if isinstance(result, dict) else None
        if decision == "deny":
            return (
                403,
                {
                    "id": reference_id,
                    "endpoint": self.name,
                    "created_at": datetime.now(UTC).isoformat(),
                    "payment_status": "failed",
                    "error": {
                        "code": "wallet_not_trusted",
                        "message": "Payment signer wallet failed OFAC SDN screening; settle blocked.",
                        "reasons": list(result.get("decision_reasons") or []) if isinstance(result, dict) else [],
                    },
                },
                {"Content-Type": "application/json"},
            )
        return None

    async def _handle_x402_settle(
        self,
        request: ComputeFirstRequest,
        reference_id: str,
        cached_body: dict[str, Any],
        price_cents: int,
        recipients: dict[str, str],
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        verified = await verify_x402_request(
            headers=request.headers,
            is_cached_address=lambda _addr: _true(),
            accepted_network=(self.rails.x402_base.network if self.rails.x402_base else "eip155:8453"),
        )
        if not isinstance(verified, VerifyX402RequestSuccess):
            return verified.status, verified.body, {"Content-Type": "application/json"}

        actual_usd = format_usd_cents(price_cents, decimals=self.decimals)
        settle_result = await process_x402_settle(
            x402_server=self.x402_server,
            payload=verified.payload,
            resource_config={
                "scheme": "exact",
                "network": verified.signed_network,
                "price": f"${actual_usd}",
                "payTo": verified.signed_pay_to,
                "maxTimeoutSeconds": 300,
            },
            resource_meta={
                "url": request.url,
                "description": f"Agent purchase via x402-exact ({self.name})",
                "mimeType": "application/json",
            },
        )
        if not isinstance(settle_result, ProcessX402SettleSuccess):
            detail = getattr(getattr(settle_result, "error", None), "args", ("unknown",))
            return (
                502,
                {
                    "id": reference_id,
                    "endpoint": self.name,
                    "created_at": _iso_now(),
                    "payment_status": "failed",
                    "charged_usd": "0.00",
                    "rail": f"x402-base ({verified.signed_network})",
                    "error": {
                        "code": "settle_failed",
                        "message": "Facilitator rejected the exact settle; no on-chain capture occurred.",
                        "detail": str(detail[0]) if detail else "unknown",
                    },
                },
                {"Content-Type": "application/json"},
            )

        x402_header = read_x402_payment_header(request.headers)
        signer = extract_payment_signer(x402_header) if x402_header else None
        rail_label = f"Base ({verified.signed_network})"

        if self.on_settled is not None:
            try:
                await self.on_settled(
                    ComputeFirstSettledContext(
                        request=request,
                        rail="x402",
                        cached_body=cached_body,
                        price_cents=price_cents,
                        price_usd=actual_usd,
                        recipients=MintedRecipients(
                            tempo=recipients.get("tempo"),
                            x402_base=recipients.get("x402_base"),
                            solana_mpp=recipients.get("solana_mpp"),
                        ),
                        signer_address=signer.address if signer else None,
                        signer_network=signer.network if signer else None,
                    )
                )
            except Exception as exc:
                log.warning("[%s.compute_first.on_settled] x402 side-effect failed: %s", self.name, exc)

        body = self._build_success_body(
            SuccessBodyArgs(
                reference_id=reference_id,
                endpoint=self.name,
                charged_usd=actual_usd,
                rail=rail_label,
                cached_body=cached_body,
                signer_address=signer.address if signer else None,
                signer_network=signer.network if signer else None,
            )
        )
        return 200, body, {"Content-Type": "application/json"}

    def _mpp_rail_label(self, method: str | None) -> str:
        # Receipt.method ships as either the bare scheme (``"tempo"``) or the
        # full directive (``"tempo/charge"``). Strip the suffix to match both.
        scheme = method.split("/", 1)[0] if method else None
        if scheme == "tempo":
            network_name = (
                "tempo-testnet"
                if (self.rails.tempo and self.rails.tempo.testnet)
                else (self.rails.tempo.network if self.rails.tempo and self.rails.tempo.network else "tempo-mainnet")
            )
            return f"Tempo ({network_name})"
        if scheme == "solana":
            network_name = (
                self.rails.solana_mpp.network if self.rails.solana_mpp and self.rails.solana_mpp.network else "solana"
            )
            return f"Solana ({network_name})"
        if scheme == "stripe":
            return "Stripe (card+link)"
        return "MPP"

    async def _handle_mpp_settle(
        self,
        request: ComputeFirstRequest,
        reference_id: str,
        cached_body: dict[str, Any],
        price_cents: int,
        recipients: dict[str, str],
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if self.compose_mppx is None:
            return (
                503,
                {
                    "id": reference_id,
                    "endpoint": self.name,
                    "created_at": _iso_now(),
                    "payment_status": "failed",
                    "charged_usd": "0.00",
                    "error": {
                        "code": "mpp_unavailable",
                        "message": "MPP settle hook not configured on this endpoint.",
                    },
                },
                {"Content-Type": "application/json"},
            )

        price_usd = format_usd_cents(price_cents, decimals=self.decimals)
        mpp_recipients = MintedRecipients(
            tempo=recipients.get("tempo"),
            x402_base=recipients.get("x402_base"),
            solana_mpp=recipients.get("solana_mpp"),
        )
        result = await self.compose_mppx(
            ComputeFirstMppContext(
                request=request,
                cached_body=cached_body,
                price_cents=price_cents,
                price_usd=price_usd,
                recipients=mpp_recipients,
            )
        )
        if result.status != 200:
            return (
                400,
                {
                    "id": reference_id,
                    "endpoint": self.name,
                    "created_at": _iso_now(),
                    "payment_status": "failed",
                    "charged_usd": "0.00",
                    "error": {
                        "code": "mpp_settle_failed",
                        "message": "MPP compose did not return 200; credential rejected.",
                    },
                },
                {"Content-Type": "application/json", **(result.headers or {})},
            )

        if result.signer_address:
            signer_address = result.signer_address
            signer_network = result.signer_network or "evm"
        else:
            x402_header = read_x402_payment_header(request.headers)
            signer = extract_payment_signer(x402_header) if x402_header else None
            signer_address = signer.address if signer else None
            signer_network = signer.network if signer else None

        method = derive_mppx_receipt_method(result.raw)
        rail_label = self._mpp_rail_label(method)

        if self.on_settled is not None:
            try:
                await self.on_settled(
                    ComputeFirstSettledContext(
                        request=request,
                        rail="mpp",
                        cached_body=cached_body,
                        price_cents=price_cents,
                        price_usd=price_usd,
                        recipients=mpp_recipients,
                        mpp_method=method,
                        signer_address=signer_address,
                        signer_network=signer_network,
                        payment_intent_id=result.tx_hash,
                    )
                )
            except Exception as exc:
                log.warning("[%s.compute_first.on_settled] MPP side-effect failed: %s", self.name, exc)

        body = self._build_success_body(
            SuccessBodyArgs(
                reference_id=reference_id,
                endpoint=self.name,
                charged_usd=price_usd,
                rail=rail_label,
                cached_body=cached_body,
                payment_intent_id=result.tx_hash,
                signer_address=signer_address,
                signer_network=signer_network,
            )
        )
        return 200, body, {"Content-Type": "application/json"}

    async def handle(self, request: ComputeFirstRequest) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Framework-neutral entry point. Returns ``(status, body, headers)``."""
        reference_id = f"{self.name}_{uuid.uuid4()}"
        body = request.body or {}

        if self.validate_input is not None:
            try:
                self.validate_input(body)
            except CheckoutValidationError as err:
                envelope: dict[str, Any] = {"error": {"code": err.code, "message": err.message}}
                if err.action:
                    envelope["next_steps"] = {"action": err.action, "user_message": err.message}
                envelope.update(err.extra or {})
                return err.status, envelope, {"Content-Type": "application/json"}

        cache_key = self.cache.body_hash_key(self.name, body)

        if has_x402_header(request.headers) or has_mppx_header(request.headers):
            quote = await self.cache.read(cache_key)
            if quote is None:
                return (
                    400,
                    {
                        "id": reference_id,
                        "endpoint": self.name,
                        "created_at": _iso_now(),
                        "payment_status": "failed",
                        "charged_usd": "0.00",
                        "error": {
                            "code": "stale_quote",
                            "message": (
                                "No active quote for this request body. The quote may have "
                                "expired or the body changed since the probe."
                            ),
                        },
                        "next_steps": {
                            "action": "re_probe",
                            "suggestion": (
                                "Send the same body without a payment header to get a fresh "
                                "402 quote, then retry with the payment credential."
                            ),
                        },
                    },
                    {"Content-Type": "application/json"},
                )
            recipients = quote.recipients if hasattr(quote, "recipients") else {}
            # Wallet OFAC SDN enforcement (always-on default — mirrors
            # Checkout._run_wallet_sanctions_only). Strict-liability check
            # before the rail-specific settle so funds don't move (x402) or
            # order doesn't fulfill (MPP) for a sanctioned wallet.
            ofac_denial = await self._enforce_wallet_sanctions(request, reference_id)
            if ofac_denial is not None:
                return ofac_denial
            if has_x402_header(request.headers):
                return await self._handle_x402_settle(
                    request, reference_id, quote.body, int(quote.price_cents), recipients
                )
            return await self._handle_mpp_settle(request, reference_id, quote.body, int(quote.price_cents), recipients)

        # Probe leg
        quote = await self.cache.read(cache_key)
        if quote is None:
            try:
                outcome = await self.run_work(body, ComputeFirstWorkContext(request=request))
            except Exception:
                # Suppress the upstream exception detail in the wire response —
                # merchant errors may carry stack traces or internal state. The
                # merchant's own logger is the right channel for the full exception.
                return (
                    200,
                    {
                        "id": reference_id,
                        "endpoint": self.name,
                        "created_at": _iso_now(),
                        "payment_status": "no_charge",
                        "charged_usd": "0.00",
                        "result": {"matches": [], "total": 0},
                        "error": {
                            "code": "upstream_failed",
                            "message": "The wrapped endpoint failed; no charge was applied.",
                        },
                    },
                    {"Content-Type": "application/json"},
                )

            if outcome.result_count == 0:
                return (
                    200,
                    {
                        "id": reference_id,
                        "endpoint": self.name,
                        "created_at": _iso_now(),
                        "payment_status": "no_charge",
                        "charged_usd": "0.00",
                        "result": outcome.body,
                    },
                    {"Content-Type": "application/json"},
                )

            price_cents = int(self.unit_price_cents * outcome.result_count)
            recipients = await self._mint_and_resolve_recipients(request, body, price_cents)
            await self.cache.write(cache_key, outcome.body, price_cents, recipients=recipients)
            return await self._emit_402(request, body, price_cents, recipients)

        recipients = quote.recipients if hasattr(quote, "recipients") else {}
        return await self._emit_402(request, body, int(quote.price_cents), recipients)

    # ── per-framework adapters ───────────────────────────────────────────────

    async def handle_fastapi(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        from fastapi.responses import JSONResponse

        if body is None:
            try:
                parsed_body = await request.json()
            except (ValueError, TypeError):
                parsed_body = {}
        else:
            parsed_body = body
        status, response_body, headers = await self.handle(
            ComputeFirstRequest(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=parsed_body,
                raw=request,
            )
        )
        return JSONResponse(content=response_body, status_code=status, headers=headers)

    async def handle_aiohttp(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        from aiohttp import web

        if body is None:
            try:
                parsed_body = await request.json()
            except (ValueError, TypeError):
                parsed_body = {}
        else:
            parsed_body = body
        status, response_body, headers = await self.handle(
            ComputeFirstRequest(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=parsed_body,
                raw=request,
            )
        )
        # web.json_response sets Content-Type itself; strip ours to avoid conflict.
        filtered_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        return web.json_response(data=response_body, status=status, headers=filtered_headers)

    async def handle_sanic(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        from sanic import response as sanic_response

        if body is None:
            try:
                parsed_body = request.json or {}
            except (ValueError, TypeError):
                parsed_body = {}
        else:
            parsed_body = body
        status, response_body, headers = await self.handle(
            ComputeFirstRequest(
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=parsed_body,
                raw=request,
            )
        )
        return sanic_response.json(response_body, status=status, headers=headers)

    def handle_flask(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        """Flask adapter — synchronous-callable that runs the async handle.

        Returns a Flask Response.
        """
        import asyncio

        from flask import jsonify

        if body is None:
            try:
                parsed_body = request.get_json(force=True) or {}
            except Exception:
                parsed_body = {}
        else:
            parsed_body = body

        async def _run() -> tuple[int, dict[str, Any], dict[str, str]]:
            return await self.handle(
                ComputeFirstRequest(
                    method=request.method,
                    url=request.url,
                    headers=dict(request.headers.items()),
                    body=parsed_body,
                    raw=request,
                )
            )

        loop = asyncio.new_event_loop()
        try:
            status, response_body, headers = loop.run_until_complete(_run())
        finally:
            loop.close()
        response = jsonify(response_body)
        response.status_code = status
        for k, v in headers.items():
            response.headers[k] = v
        return response

    def handle_django(self, request: Any, *, body: dict[str, Any] | None = None) -> Any:
        """Django adapter — synchronous-callable matching ``Checkout.handle_django``.

        Returns a ``JsonResponse``.
        """
        import asyncio
        import json

        from django.http import JsonResponse

        if body is None:
            try:
                parsed_body = json.loads(request.body or b"{}")
            except (ValueError, TypeError):
                parsed_body = {}
        else:
            parsed_body = body

        async def _run() -> tuple[int, dict[str, Any], dict[str, str]]:
            return await self.handle(
                ComputeFirstRequest(
                    method=request.method,
                    url=request.build_absolute_uri(),
                    headers=dict(request.headers.items()),
                    body=parsed_body,
                    raw=request,
                )
            )

        loop = asyncio.new_event_loop()
        try:
            status, response_body, headers = loop.run_until_complete(_run())
        finally:
            loop.close()
        response = JsonResponse(response_body, status=status)
        for k, v in headers.items():
            response[k] = v
        return response


def compute_first_checkout(**kwargs: Any) -> ComputeFirstCheckout:
    """Factory wrapper for parity with node's ``computeFirstCheckout(opts)``.

    Equivalent to constructing :class:`ComputeFirstCheckout` directly.
    """
    return ComputeFirstCheckout(**kwargs)


# ── helpers ──────────────────────────────────────────────────────────────────


def _replace_recipient(spec: Any, recipient: str) -> Any:
    """Return a shallow copy of ``spec`` with ``recipient`` swapped."""
    from dataclasses import replace as dc_replace

    try:
        return dc_replace(spec, recipient=recipient)
    except TypeError:
        return spec


def _json_dumps(body: dict[str, Any]) -> str:
    import json

    return json.dumps(body, separators=(",", ":"), sort_keys=True)


async def _true() -> bool:
    return True
