"""Cross-language UCP signing fixture corpus.

Each fixture file is a ``{profile, jwks, alg, kid, generator}`` envelope. Both
Node and Python check in identical fixtures so a future canonicalization change
in either language fails CI loudly. Without this, cross-language byte parity
drift would silently break verifier-side compatibility in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentscore_commerce.identity import verify_ucp_profile

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "cross-lang"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=[p.name for p in FIXTURES])
def test_verifies_cross_lang_fixture(fixture_path: Path) -> None:
    data = json.loads(fixture_path.read_text())
    assert verify_ucp_profile(data["profile"], data["jwks"]) is True


def test_corpus_contains_both_generators() -> None:
    generators = {json.loads(p.read_text())["generator"] for p in FIXTURES}
    assert "node" in generators
    assert "python" in generators
    assert len(FIXTURES) >= 6
