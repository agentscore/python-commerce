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


def test_corpus_covers_canonical_scenarios() -> None:
    names = {p.name for p in FIXTURES}
    generators = {json.loads(p.read_text())["generator"] for p in FIXTURES}
    assert "node" in generators
    assert "python" in generators
    # `emoji-keys` exercises non-ASCII object keys with codepoints that genuinely
    # distinguish UTF-16 first-unit sort from Unicode codepoint sort: BMP private use
    # (U+E000) ranks BEFORE supplementary plane (U+1F377) by codepoint but AFTER it by
    # UTF-16 first unit (because the high surrogate 55356 < 57344). Both repos ship the
    # node and python emoji-keys fixtures so a regression in either language's key sort
    # surfaces here.
    for lang in ("node", "py"):
        for scenario in (
            "minimal",
            "es256-rails",
            "extras-int",
            "capability",
            "unicode",
            "multikey",
            "emoji-keys",
            "int-boundary",
            # `agentscore-gate-full` exercises a fully-populated merchant policy
            # (require_kyc + require_sanctions_clear + min_age + allowed_jurisdictions).
            # `agentscore-gate-blocked` exercises blocked_jurisdictions (the inverse
            # jurisdiction policy). Both languages serialize the merchant gate config
            # identically; a drift in either's JCS-canonical output breaks cross-lang
            # verify silently.
            "agentscore-gate-full",
            "agentscore-gate-blocked",
        ):
            assert f"{lang}-{scenario}.json" in names, f"missing fixture {lang}-{scenario}.json"
