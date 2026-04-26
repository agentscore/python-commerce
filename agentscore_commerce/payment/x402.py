"""x402 v1+v2 dual-register helper.

The @x402/core HTTP parser hardcodes `x402Version === 1`, while the client's `.register()` defaults
to v2. Without registering on both versions, a merchant emitting a v1 response gets
"No client registered for x402 version: 1" even though the scheme handler is identical between
versions. Every merchant trips on this; the helper hides the workaround.

Note: most Python merchants integrate x402 by responding with the right HTTP shape and validating
via the facilitator's HTTP API. The TS-specific scheme registration is exposed here for projects
that bind to a Python wrapper around @x402/core (e.g., via subprocess or a future native port).
"""

from typing import Protocol


class X402ServerLike(Protocol):
    def register(self, network: str, scheme: object) -> None: ...

    def register_v1(self, network: str, scheme: object) -> None: ...


def register_x402_schemes_v1_v2(server: object, network: str, scheme: object) -> None:
    """Register an x402 scheme on both v1 and v2 of the protocol."""
    register = getattr(server, "register", None)
    if not callable(register):
        raise TypeError("server has no callable `register` method")
    register(network, scheme)
    register_v1 = getattr(server, "register_v1", None)
    if callable(register_v1):
        register_v1(network, scheme)
