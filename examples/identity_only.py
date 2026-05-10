"""Example: compliance gate without payment

Scenario: you sell something where the gating is the whole product — your service handles
its own billing (Stripe, invoice, prepaid credit, etc.) but you need to verify the agent
operator is KYC'd, age-verified, sanctions-clear, and in an allowed jurisdiction before
delivering.

This is the smallest possible commerce integration. Mount the gate, write your route,
done. No 402 logic, no payment plumbing — just identity gating.

Peer deps:
    pip install agentscore-commerce[fastapi]

Env vars:
    AGENTSCORE_API_KEY — get one at agentscore.sh/dashboard

Run: uvicorn examples.identity_only:app --port 3000
"""

from fastapi import Depends, FastAPI

from agentscore_commerce.identity.fastapi import AgentScoreGate, get_agentscore_data

app = FastAPI()

gate = AgentScoreGate(
    api_key="ask_...",  # use os.environ["AGENTSCORE_API_KEY"] in prod
    require_kyc=True,
    require_sanctions_clear=True,
    min_age=21,
    allowed_jurisdictions=["US"],
)


@app.post("/deliver", dependencies=[Depends(gate)])
async def deliver(assess: dict = Depends(get_agentscore_data)):
    """Gated route — only reached when the agent passes the compliance policy.

    `assess` is the raw `/v1/assess` response. Use it for downstream business logic that
    depends on the verified identity (audit trail, per-operator pricing, etc.).
    """
    return {"status": "delivered", "operator": assess.get("resolved_operator")}
