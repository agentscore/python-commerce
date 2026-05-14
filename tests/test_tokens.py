"""Tests for ``agentscore_commerce.identity.tokens.hash_operator_token``.

The expected digests below are hardcoded — locked as the cross-language
contract with the Node sibling at ``node-commerce/tests/identity/tokens.test.ts``.
Both files reference the same fixture inputs and the same expected output bytes.
A drift in either language (algorithm swap, encoding change, accidental truncation)
fails that language's test against the locked digest.
"""

from __future__ import annotations

import pytest

from agentscore_commerce.identity import hash_operator_token

# Cross-language fixture inputs + expected digests. The digests are
# sha256(<input>.encode("utf-8")).hexdigest() computed once and locked here so
# the Python and Node sibling tests assert against identical bytes.
#
# Parametrized so each fixture gets its own test invocation: if multiple
# fixtures drift simultaneously, every failure is reported (a for-loop inside
# one test would short-circuit on the first failure).
_FIXTURES = [
    ("opc_test", "97c30e2a512b5968772c2930705bdafff4831d672556dce26c92b83f7e58508d"),
    ("opc_cross_lang_fixture", "96690dd2659bc1e33227e943d5f8a526c7c95a0ede5775a1573abab6578ca8ec"),
    ("opc_anything", "e6ba517ac96ee39190c4d703b2d968fec96e87827374e56095a2f443d870730d"),
    ("opc_42", "731985dd676ea0702b3e6f6cbb107eaf467319e2801e6f953f08cbcc7dd71684"),
    # Non-ASCII fixture — UTF-8 encoding of "é" is 0xC3 0xA9; locks the encoding
    # contract so a future implementation that drops the explicit "utf-8" arg
    # still produces the same bytes.
    ("opc_é", "c1dba11d60cbfc1264d115e07a74a0355b6a66ded4ee3f930024a1733ba6942f"),
    # Empty-string sha256 is a canonical value documented in many specs; locking
    # it here catches an implementation that silently rejects or transforms "".
    ("", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
]


@pytest.mark.parametrize(("plaintext", "expected"), _FIXTURES, ids=[repr(p) for p, _ in _FIXTURES])
def test_known_digest_locked(plaintext: str, expected: str) -> None:
    """Each fixture input maps to the locked cross-language digest."""
    assert hash_operator_token(plaintext) == expected


def test_output_is_64_char_lowercase_hex() -> None:
    """sha256 hex digests are always 64 characters of lowercase hex."""
    out = hash_operator_token("opc_anything")
    assert len(out) == 64
    assert out == out.lower()
    assert all(c in "0123456789abcdef" for c in out)


def test_deterministic_across_calls() -> None:
    """Same input always yields the same digest (no salt, no nonce)."""
    assert hash_operator_token("opc_42") == hash_operator_token("opc_42")


def test_distinct_inputs_distinct_outputs() -> None:
    """Different plaintexts produce different digests."""
    assert hash_operator_token("opc_a") != hash_operator_token("opc_b")
