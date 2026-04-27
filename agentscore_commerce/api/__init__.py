"""AgentScore SDK re-export — single import path for the underlying agentscore-py.

Vendors install only ``agentscore-commerce`` and reach everything from the underlying
``agentscore-py`` here. Don't add ``agentscore-py`` as a separate dep; the two can
drift versions and cause subtle type mismatches.

Use this for: programmatic API calls (sessions, credentials, reputation) and the
test-mode address fixtures for integration tests.
"""

from agentscore import (
    AGENTSCORE_TEST_ADDRESSES,
    AgentScore,
    AgentScoreError,
    is_agentscore_test_address,
)

__all__ = [
    "AGENTSCORE_TEST_ADDRESSES",
    "AgentScore",
    "AgentScoreError",
    "is_agentscore_test_address",
]
