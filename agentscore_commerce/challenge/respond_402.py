"""``respond_402`` — single-call 402 emit for merchants who use both pympp + x402.

Pympp handles tempo + stripe MPP rails; x402 handles Base + Solana.

The seam is fiddly enough to get wrong by hand:

- pympp's compose returns a 402 response with WWW-Authenticate directives whose ids
  pympp's server-side validator REMEMBERS — they round-trip in client credentials.
  Overwriting that header (e.g. with a freshly-built directive) breaks the round-trip.
- x402 needs the binary-friendly ``PAYMENT-REQUIRED`` header (base64-encoded JSON of
  ``{x402Version, accepts, resource}``) — pympp doesn't emit it.
- Merchants want a richer JSON body (pricing, identity metadata, agent_instructions,
  agent_memory, retry_body, accepted_methods cross-reference) than the bare pympp body.

``respond_402`` composes all three and returns a framework-neutral ``Respond402Result``
(body + headers + status) that the merchant wraps in their framework's response shape.

Usage::

    from agentscore_commerce.challenge import respond_402

    result = respond_402(
        mppx_challenge_headers=dict(challenge_response.headers),
        body={"accepted_methods": ..., ...},
        x402={"x402_version": 2, "accepts": [...], "resource": {...}},
    )
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)
"""

from dataclasses import dataclass
from typing import Any

from agentscore_commerce.payment.wwwauthenticate import payment_required_header


@dataclass
class Respond402Result:
    """Framework-neutral 402 response shape — body + headers + status."""

    body: dict[str, object]
    headers: dict[str, str]
    status: int = 402


def respond_402(
    *,
    mppx_challenge_headers: dict[str, str],
    body: dict[str, Any],
    x402: dict[str, Any] | None = None,
) -> Respond402Result:
    """Compose the rich body + preserved-mppx WWW-Auth + optional x402 PAYMENT-REQUIRED.

    The merchant wraps the returned ``Respond402Result`` in their framework's response
    shape (``JSONResponse`` for FastAPI, ``flask.Response`` for Flask, etc.).

    ``body`` is the already-built dict from :func:`build_402_body`. ``x402``, when
    set, carries the PAYMENT-REQUIRED header inputs (``x402_version``, ``accepts``,
    ``resource``); omit for merchants that don't accept x402 (Base / Solana) — pympp-only
    setups.
    """
    headers = {k.lower(): v for k, v in mppx_challenge_headers.items()}
    headers["content-type"] = "application/json"
    if x402 is not None:
        headers["payment-required"] = payment_required_header(**x402)
    return Respond402Result(body=body, headers=headers, status=402)
