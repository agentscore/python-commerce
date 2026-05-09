"""One-shot generator for the typed-claims cross-lang fixture (Python side).

Writes ``tests/fixtures/cross-lang/py-typed-claims.json``. Sibling to
``generate_data_driven_claims_fixture.py`` but exercises the **typed**
``AssessResult.account_verification`` / ``AssessResult.operator_verification``
read path (with ``raw=None``) instead of the raw-dict fallback. This catches
drift in typed-field-only callers — production code populates both, but a
hand-constructed AssessResult with only typed fields must produce a profile
that the Node sibling verifies byte-for-byte, since Node's
``buildUCPProfile`` reads the typed fields directly without ever consulting
``raw``.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentscore_commerce.identity import (
    AssessResult,
    OperatorVerification,
    UCPService,
    UCPSigningKey,
    build_ucp_profile,
)
from agentscore_commerce.identity.ucp_jwks import (
    build_jwks_response,
    generate_ucp_signing_key,
    sign_ucp_profile,
)

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cross-lang" / "py-typed-claims.json"

KID = "py-typed-claims-EdDSA"


def main() -> None:
    key = generate_ucp_signing_key(kid=KID)

    result = AssessResult(
        allow=True,
        resolved_operator="op_typed_claims",
        verify_url="https://agentscore.sh/verify/op_typed_claims",
        operator_verification=OperatorVerification(
            level="enhanced",
            operator_type="api",
            verified_at="2026-04-01T00:00:00Z",
        ),
        account_verification={
            "kyc_level": "enhanced",
            "sanctions_clear": True,
            "age_bracket": "21+",
            "jurisdiction": "US",
            "verified_at": "2026-04-01T00:00:00Z",
        },
        raw=None,
    )

    profile = build_ucp_profile(
        name="Typed Claims Merchant",
        services=[UCPService(type="rest", url="https://t.example.com")],
        payment_handlers=[],
        signing_keys=[UCPSigningKey.from_jwk(key.public_jwk)],
        data=result,
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
