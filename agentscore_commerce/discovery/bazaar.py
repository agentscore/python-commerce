"""Bazaar discovery extension wrapper.

Bazaar is currently TS-only (`@x402/extensions/bazaar`). Python merchants who want to participate
in the Bazaar discovery should serve the Bazaar JSON document themselves at the appropriate URL.
This helper documents the expected shape and is a placeholder for a future Python-native binding.
"""

from typing import Any


def build_bazaar_discovery_payload(
    *,
    body_type: str | None = None,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON document a Bazaar discovery endpoint should serve."""
    out: dict[str, Any] = {}
    if body_type:
        out["bodyType"] = body_type
    if input is not None:
        out["input"] = input
    if output is not None:
        out["output"] = output
    if extra:
        out.update(extra)
    return out
