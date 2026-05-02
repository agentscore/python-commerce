# Vulture whitelist — false positives

# Middleware __call__ ASGI signature
scope  # noqa: F821
receive  # noqa: F821
send  # noqa: F821

# Redis SET kwarg in _RedisLike Protocol (structural type for redis.asyncio.Redis)
ex  # noqa: F821

# Public API exports
Activity  # noqa: F821
AgentScoreGate  # noqa: F821
AssessResult  # noqa: F821
Classification  # noqa: F821
DenialReason  # noqa: F821
Grade  # noqa: F821
Identity  # noqa: F821
Reputation  # noqa: F821
ScoreDetail  # noqa: F821
ScoreStatus  # noqa: F821
OperatorVerification  # noqa: F821

# TYPE_CHECKING imports referenced inside string-literal cast() calls
DecisionPolicy  # noqa: F821
ResolveSigner  # noqa: F821
