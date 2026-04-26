def test_api_reexports_agentscore_class():
    from agentscore_commerce.api import AgentScore, AgentScoreError

    assert AgentScore is not None
    assert AgentScoreError is not None
