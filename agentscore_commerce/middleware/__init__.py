"""Framework-specific middleware helpers (rate-limit and friends).

Import per-framework: ``from agentscore_commerce.middleware.fastapi import rate_limit_fastapi``.
Each adapter shares the framework-agnostic ``create_rate_limiter`` core so multiple
adapter instances in the same process don't share state unless they're pointed at the
same Redis with the same ``key_prefix``.
"""
