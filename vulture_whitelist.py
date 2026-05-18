# Vulture whitelist — false positives

# Middleware __call__ ASGI signature
scope  # noqa: F821
receive  # noqa: F821
send  # noqa: F821

# Redis kwargs / Protocol method params (structural typing for redis.asyncio.Redis)
ex  # noqa: F821
seconds  # noqa: F821

# Public API exports
AgentScoreGate  # noqa: F821
AssessResult  # noqa: F821
DenialReason  # noqa: F821
OperatorVerification  # noqa: F821

# TYPE_CHECKING imports referenced inside string-literal cast() calls
DecisionPolicy  # noqa: F821
Signer  # noqa: F821
