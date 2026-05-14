"""Public-API surface tests.

Locks the documented public surface so a future helper that lands in a module
but is forgotten in the submodule barrel re-export
(``agentscore_commerce.<submodule>.__init__``) fails CI. Mirrors the node-commerce
sibling at ``node-commerce/tests/public-surface.test.ts``.

The trigger was a Node-side gap on ``loadUCPSigningKeyFromEnv`` during the TEC-302
lift-up: the helper was defined in ``src/identity/ucp-jwks.ts`` but never
re-exported from ``src/index.ts``. Python had the helper barrel-exported correctly
the first time, but the same gap could hit any future helper; assert every
TEC-302 lift-up entry is importable from its documented path.
"""

from __future__ import annotations


def test_identity_barrel_exports_hash_operator_token() -> None:
    """``hash_operator_token`` is importable from ``agentscore_commerce.identity``."""
    from agentscore_commerce import identity as barrel
    from agentscore_commerce.identity import tokens as module

    assert barrel.hash_operator_token is module.hash_operator_token


def test_identity_barrel_exports_ucp_env_loader() -> None:
    """``load_ucp_signing_key_from_env`` + ``LoadUCPSigningKeyOptions`` reachable from the identity barrel."""
    from agentscore_commerce import identity as barrel
    from agentscore_commerce.identity import ucp_jwks as module

    assert barrel.load_ucp_signing_key_from_env is module.load_ucp_signing_key_from_env
    assert barrel.LoadUCPSigningKeyOptions is module.LoadUCPSigningKeyOptions


def test_payment_barrel_exports_detect_rail_zero_settle_usd_to_atomic() -> None:
    from agentscore_commerce.payment import (
        detect_rail_from_headers,
        usd_to_atomic,
        zero_amount_carve_out,
    )

    assert callable(detect_rail_from_headers)
    assert callable(zero_amount_carve_out)
    assert callable(usd_to_atomic)


def test_payment_barrel_exports_classify_helpers() -> None:
    from agentscore_commerce.payment import classify_orchestration_error, classify_x402_settle_result

    assert callable(classify_orchestration_error)
    assert callable(classify_x402_settle_result)


def test_payment_barrel_exports_signer_helpers() -> None:
    from agentscore_commerce.payment import extract_payment_signer, read_x402_payment_header

    assert callable(extract_payment_signer)
    assert callable(read_x402_payment_header)
