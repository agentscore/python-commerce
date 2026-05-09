"""One-shot generator for the data-driven-claims cross-lang fixture (Python side).

Writes ``tests/fixtures/cross-lang/py-data-driven-claims.json``. Unlike the
other cross-lang fixtures (which hand-craft the ``agentscore-identity``
capability), this one EXERCISES ``build_ucp_profile``'s data path: it
constructs a synthetic ``AssessResult`` with the API-shape "missing" sentinels
(empty string for kyc_level, None for age_bracket / jurisdiction /
verified_at) and lets the builder coalesce them. Both languages MUST emit
identical canonical bytes for this input or cross-lang verify drifts silently
in production.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentscore_commerce.identity import (
    AssessResult,
    UCPService,
    UCPSigningKey,
    build_ucp_profile,
)
from agentscore_commerce.identity.ucp_jwks import (
    build_jwks_response,
    generate_ucp_signing_key,
    sign_ucp_profile,
)

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cross-lang" / "py-data-driven-claims.json"

KID = "py-data-driven-claims-EdDSA"


def main() -> None:
    key = generate_ucp_signing_key(kid=KID)

    result = AssessResult(
        allow=True,
        resolved_operator="op_data_driven",
        verify_url="https://agentscore.sh/verify/op_data_driven",
        raw={
            "account_verification": {
                # Empty string is the API's "set but unknown" shape for some
                # columns; None is the shape for others. The builder must
                # coerce both to the schema default identically across node
                # and python.
                "kyc_level": "",
                "sanctions_clear": False,
                "age_bracket": None,
                "jurisdiction": None,
                "verified_at": None,
            },
        },
    )

    profile = build_ucp_profile(
        name="Data Driven Claims Merchant",
        services=[UCPService(type="rest", url="https://d.example.com")],
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
