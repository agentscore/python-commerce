"""Bazaar discovery extension wrapper.

Bazaar is currently TS-only (`@x402/extensions/bazaar`). Python merchants who want to participate
in the Bazaar discovery should serve the Bazaar JSON document themselves at the appropriate URL.
This helper documents the expected shape and is a placeholder for a future Python-native binding.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BazaarDiscoveryConfig:
    body_type: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_bazaar_discovery_payload(config: BazaarDiscoveryConfig) -> dict[str, Any]:
    """Build the JSON document a Bazaar discovery endpoint should serve."""
    out: dict[str, Any] = {}
    if config.body_type:
        out["bodyType"] = config.body_type
    if config.input is not None:
        out["input"] = config.input
    if config.output is not None:
        out["output"] = config.output
    out.update(config.extra)
    return out
