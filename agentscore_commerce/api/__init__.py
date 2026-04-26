"""Re-exports the AgentScore SDK so vendors install one package for both gating + raw API access."""

from agentscore import AgentScore, AgentScoreError

__all__ = ["AgentScore", "AgentScoreError"]
