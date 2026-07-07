"""Concurrency regression: the signer verdict read by ``get_signer_verdict(request)`` is
REQUEST-SCOPED, so two in-flight requests that claim the SAME wallet but sign with DIFFERENT
wallets each see their OWN verdict.

Before the fix, every adapter's ``get_signer_verdict`` read ``client.get_signer_verdict(addr)``
off the SHARED core, whose ``_last_signer_raw`` slot is keyed by claimed address only. Under
concurrency the slot is last-writer-wins: request A (clean signer) could read request B's verdict
(sanctioned signer) — or, worse, request B (sanctioned) could read A's ``pass``/``clear`` and
settle. The gate now stashes the verdict projected from THIS request's assess response on the
per-request state, which can't be raced.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Request

from agentscore_commerce.identity.fastapi import AgentScoreGate, get_signer_verdict
from agentscore_commerce.identity.types import AssessResult

# Same claimed wallet on BOTH concurrent requests; different payment signers.
CLAIMED = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SIGNER_CLEAN = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SIGNER_SANCTIONED = "0xcccccccccccccccccccccccccccccccccccccccc"


def _raw_for(signer_addr: str) -> dict[str, Any]:
    """A /v1/assess raw response carrying a signer_match keyed to ``signer_addr``."""
    sanctioned = signer_addr == SIGNER_SANCTIONED
    return {
        "decision": "allow",  # signer-match enforcement is separate from the decision
        "decision_reasons": [],
        "resolved_operator": "op_claimed",
        "signer_match": {
            "kind": "wallet_signer_mismatch" if sanctioned else "pass",
            "claimed_operator": "op_claimed",
            "signer_operator": "op_other" if sanctioned else "op_claimed",
            "actual_signer": signer_addr,
            "expected_signer": CLAIMED,
        },
        "signer_sanctions": {"kind": "sdn_hit"} if sanctioned else {"kind": "clear"},
    }


@pytest.mark.asyncio
async def test_concurrent_same_wallet_distinct_signers_get_own_verdict() -> None:
    gate = AgentScoreGate(api_key="ask_test")

    # Barrier: both requests must reach the assess call (and stash into the SHARED core slot)
    # before EITHER proceeds to read its verdict. This forces the worst-case interleaving where
    # the shared _last_signer_raw[CLAIMED] slot is last-writer-wins — exactly the race.
    both_assessed = asyncio.Barrier(2)

    async def fake_acheck_identity(identity: Any, _chain: Any = None, signer: Any = None) -> AssessResult:
        signer_addr = signer["address"] if signer else CLAIMED
        raw = _raw_for(signer_addr)
        # Simulate the (race-prone) shared-core write every real assess does.
        gate._client._last_signer_raw[CLAIMED] = raw
        await both_assessed.wait()
        return AssessResult(allow=True, decision="allow", reasons=[], raw=raw)

    app = FastAPI()

    @app.get("/buy", dependencies=[Depends(gate)])
    async def buy(request: Request) -> dict[str, Any]:
        verdict = get_signer_verdict(request)
        sm = verdict.signer_match if verdict else None
        ss = verdict.signer_sanctions if verdict else None
        return {
            "kind": sm.kind if sm else None,
            "actual_signer": sm.actual_signer if sm and sm.kind == "wallet_signer_mismatch" else None,
            "signer_sanctions": ss,
        }

    # Build a payment header so the gate extracts a signer per request. The signer the gate
    # extracts comes from the X-Payment header; route each request to its own signer.
    def _payment_header(signer_addr: str) -> str:
        import base64
        import json as _json

        # x402 EIP-3009 shape: the gate recovers the signer from payload.authorization.from.
        payload = {"payload": {"authorization": {"from": signer_addr}}}
        return base64.b64encode(_json.dumps(payload).encode()).decode()

    transport = httpx.ASGITransport(app=app)
    with patch_acheck(gate, fake_acheck_identity):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            req_clean = ac.get(
                "/buy",
                headers={"X-Wallet-Address": CLAIMED, "X-Payment": _payment_header(SIGNER_CLEAN)},
            )
            req_sanctioned = ac.get(
                "/buy",
                headers={"X-Wallet-Address": CLAIMED, "X-Payment": _payment_header(SIGNER_SANCTIONED)},
            )
            resp_clean, resp_sanctioned = await asyncio.gather(req_clean, req_sanctioned)

    body_clean = resp_clean.json()
    body_sanctioned = resp_sanctioned.json()

    # The CLEAN request must see its OWN clean verdict — never the sanctioned signer's.
    assert body_clean["kind"] == "pass"
    assert body_clean["signer_sanctions"] == {"kind": "clear"}

    # The SANCTIONED request must see its OWN sanctioned verdict — never riding the clean one.
    assert body_sanctioned["kind"] == "wallet_signer_mismatch"
    assert body_sanctioned["actual_signer"] == SIGNER_SANCTIONED
    assert body_sanctioned["signer_sanctions"] == {"kind": "sdn_hit"}


def patch_acheck(gate: AgentScoreGate, fn: Any) -> Any:
    """Patch the gate's core ``acheck_identity`` with ``fn`` for the duration of a ``with`` block."""
    from unittest.mock import patch

    return patch.object(gate._client, "acheck_identity", side_effect=fn)
