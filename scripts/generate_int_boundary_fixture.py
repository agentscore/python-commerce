"""One-shot generator for the int-boundary cross-lang fixture (Python side).

Writes ``tests/fixtures/cross-lang/py-int-boundary.json``. The fixture exercises
the safe-integer boundary that BOTH languages must round-trip identically:
``Number.MAX_SAFE_INTEGER`` (2**53 - 1), its negative, zero, and small ints.
Lossy values (>2**53) are NOT in the fixture (they're rejected at sign time);
they're unit-tested in each language's signing path.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentscore_commerce.identity import (
    UCPService,
    UCPSigningKey,
    build_ucp_profile,
)
from agentscore_commerce.identity.ucp_jwks import (
    build_jwks_response,
    generate_ucp_signing_key,
    sign_ucp_profile,
)

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cross-lang" / "py-int-boundary.json"

KID = "py-int-boundary-EdDSA"


def main() -> None:
    key = generate_ucp_signing_key(kid=KID)

    profile = build_ucp_profile(
        name="Int Boundary Merchant",
        services=[UCPService(type="rest", url="https://i.example.com")],
        payment_handlers=[],
        signing_keys=[UCPSigningKey.from_jwk(key.public_jwk)],
        extras={
            "max_safe_int": 9007199254740991,
            "min_safe_int": -9007199254740991,
            "small_int": 42,
            "neg_small_int": -42,
            "zero": 0,
        },
    )

    signed = sign_ucp_profile(profile.to_dict(), signing_key=key.private_key, kid=KID)

    fixture = {
        "profile": signed,
        "jwks": build_jwks_response([key.public_jwk]),
        "alg": "EdDSA",
        "kid": KID,
        "generator": "python",
    }

    OUT.write_text(json.dumps(fixture, indent=2) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
