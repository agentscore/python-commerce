"""Canonical Checkout hook implementations for the common merchant patterns.

The hand-written ``compose_mppx`` / signer-extraction / etc. boilerplate every
merchant otherwise repeats; collapsed into ready-to-use factories that wrap
the underlying SDK helpers. Merchants compose them into ``Checkout(...)``
instead of writing 25-line closures.

These are deliberately small, focused factories. Anything more opinionated
(e.g. "what should on_settled do for goods sellers vs API sellers") stays
merchant-side; Checkout's hooks are the boundary, not the business logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentscore_commerce.checkout import MppxComposeOutcome
from agentscore_commerce.identity.address import normalize_address
from agentscore_commerce.payment.signer import extract_payment_signer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentscore_commerce.checkout import CheckoutContext, ComposeMppxFn


def make_mppx_compose_hook(
    *,
    server_getter: Callable[[], Awaitable[Any]],
) -> ComposeMppxFn:
    """Return the canonical ``compose_mppx`` hook for pympp-backed MPP rails.

    The hook:

    * Lazily resolves the pympp ``Mpp`` server via the supplied ``server_getter``
      (typically the output of :func:`lazy_mppx_server`).
    * Forwards the request's ``Authorization: Payment`` header (or ``None`` on
      the discovery leg) and the current pricing amount to ``mpp.charge``.
    * Maps pympp's three outcomes to :class:`MppxComposeOutcome`:

      - ``Challenge`` (no/invalid credential) → ``status=402`` with the
        ``www-authenticate`` header pympp issued.
      - ``(Credential, Receipt)`` tuple → ``status=200`` with the tx hash
        lifted from ``receipt.reference``/``receipt.transaction`` and the
        signer lifted from the credential's ``did:pkh:...`` source.
      - Any unexpected exception (pympp internal error) → ``status=402``
        (no headers; Checkout falls back to its standard 402 emit).

    Stripe SPT and Solana MPP can use the same hook; the ``Mpp`` instance is
    rail-agnostic. For multi-intent setups, build a separate hook per ``Mpp``
    and dispatch by the merchant's own routing logic.
    """

    async def hook(ctx: CheckoutContext) -> MppxComposeOutcome:
        if ctx.pricing is None:
            return MppxComposeOutcome(status=402)
        mpp = await server_getter()
        authorization = ctx.request.headers.get("authorization")
        amount_str = f"{ctx.pricing.amount_usd:.2f}"
        try:
            result = await mpp.charge(authorization=authorization, amount=amount_str)
        except Exception:
            return MppxComposeOutcome(status=402)

        if not isinstance(result, tuple):
            to_www = getattr(result, "to_www_authenticate", None)
            realm = getattr(mpp, "realm", "")
            headers: dict[str, str] = {"www-authenticate": to_www(realm)} if callable(to_www) else {}
            return MppxComposeOutcome(status=402, headers=headers)

        credential, receipt = result
        tx_hash = getattr(receipt, "reference", None) or getattr(receipt, "transaction", None)
        signer_address: str | None = None
        signer_network: str | None = None
        cred_source = getattr(credential, "source", None)
        if isinstance(cred_source, str):
            signer = extract_payment_signer(authorization_header=f"Payment {cred_source}")
            if signer is None:
                # `extract_payment_signer` expects a base64'd credential, not
                # a raw DID; fall back to parsing the DID directly when pympp
                # gives us the typed source string.
                parts = cred_source.split(":")
                if len(parts) >= 4 and parts[0] == "did" and parts[1] == "pkh":
                    family = parts[2]
                    addr = parts[-1]
                    if family == "eip155":
                        signer_address = normalize_address(addr)
                        signer_network = "evm"
                    elif family == "solana":
                        signer_address = normalize_address(addr)
                        signer_network = "solana"
            else:
                signer_address = signer.address
                signer_network = signer.network

        return MppxComposeOutcome(
            status=200,
            tx_hash=tx_hash,
            signer_address=signer_address,
            signer_network=signer_network,
            raw={"credential": credential, "receipt": receipt},
        )

    return hook


__all__ = ["make_mppx_compose_hook"]
