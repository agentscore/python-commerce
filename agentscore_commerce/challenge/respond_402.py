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

    from agentscore_commerce.challenge import respond_402, Respond402Input

    result = respond_402(Respond402Input(
        mppx_challenge_headers=dict(challenge_response.headers),
        body=Build402BodyInput(accepted_methods=..., ...),
        x402=PaymentRequiredHeaderInput(x402_version=2, accepts=..., resource=...),
    ))
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)
"""

from dataclasses import dataclass

from agentscore_commerce.challenge.body import Build402BodyInput, build_402_body
from agentscore_commerce.payment.wwwauthenticate import (
    PaymentRequiredHeaderInput,
    payment_required_header,
)


@dataclass
class Respond402Input:
    """Input for :func:`respond_402`."""

    #: Headers from the pympp ``compose()`` 402 response. The ``www-authenticate``
    #: header is preserved verbatim — pympp's server-side validator matches credentials
    #: to the directive ids it generated, so overwriting breaks the round-trip.
    mppx_challenge_headers: dict[str, str]
    #: Inputs to :func:`build_402_body` — the rich JSON body sent to the agent.
    body: Build402BodyInput
    #: When set, layers on the x402 PAYMENT-REQUIRED header (base64-encoded JSON).
    #: Omit for merchants that don't accept x402 (Base/Solana) — pympp-only setups.
    x402: PaymentRequiredHeaderInput | None = None


@dataclass
class Respond402Result:
    """Framework-neutral 402 response shape — body + headers + status."""

    body: dict[str, object]
    headers: dict[str, str]
    status: int = 402


def respond_402(input: Respond402Input) -> Respond402Result:
    """Compose the rich body + preserved-mppx WWW-Auth + optional x402 PAYMENT-REQUIRED.

    The merchant wraps the returned ``Respond402Result`` in their framework's response
    shape (``JSONResponse`` for FastAPI, ``flask.Response`` for Flask, etc.).
    """
    body = build_402_body(input.body)
    headers = {k.lower(): v for k, v in input.mppx_challenge_headers.items()}
    headers["content-type"] = "application/json"
    if input.x402 is not None:
        headers["payment-required"] = payment_required_header(input.x402)
    return Respond402Result(body=body, headers=headers, status=402)
