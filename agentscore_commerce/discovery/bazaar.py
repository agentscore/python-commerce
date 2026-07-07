"""Bazaar discovery extension wrapper.

Bazaar is currently TS-only (`@x402/extensions/bazaar`). Python merchants who want to participate
in the Bazaar discovery should serve the Bazaar JSON document themselves at the appropriate URL.
This helper documents the expected shape and is a placeholder for a future Python-native binding.
"""

import copy
from typing import Any

# The Bazaar discovery extension key (mirrors x402.extensions.bazaar ``BAZAAR.key``).
_BAZAAR_KEY = "bazaar"


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


def enrich_bazaar_discovery_extensions(
    extensions: dict[str, Any] | None,
    *,
    method: str,
    path: str,
) -> dict[str, Any] | None:
    """Fill ``info.input.method`` on a declared Bazaar discovery extension.

    The v2 discovery schema requires ``info.input.method`` in {POST, PUT, PATCH},
    but it is absent at declaration time; the reference x402 flow fills it (and
    ``routeTemplate`` for parameterized routes) from the request. This mirrors
    ``BazaarResourceServerExtension.enrich_declaration`` directly, without importing
    the x402 bazaar package (which pulls in ``jsonschema``). Returns the map
    unchanged when no bazaar declaration is present.
    """
    if extensions is None:
        return extensions
    declaration = extensions.get(_BAZAAR_KEY)
    if not isinstance(declaration, dict):
        return extensions
    info = declaration.get("info")
    input_block = info.get("input") if isinstance(info, dict) else None
    if isinstance(input_block, dict) and input_block.get("type") == "mcp":
        return extensions  # MCP discovery has no HTTP method

    enriched = copy.deepcopy(declaration)
    enriched.setdefault("info", {}).setdefault("input", {})["method"] = method
    schema = enriched.get("schema")
    if isinstance(schema, dict):
        input_schema = schema.setdefault("properties", {}).setdefault("input", {})
        if isinstance(input_schema, dict):
            required = list(input_schema.get("required", []))
            if "method" not in required:
                required.append("method")
            input_schema["required"] = required
    # routeTemplate (the facilitator's catalog key) only applies to parameterized
    # routes; a concrete request path carries no template, so it stays unset.
    if ":" in path or "{" in path:
        enriched["routeTemplate"] = path
    return {**extensions, _BAZAAR_KEY: enriched}
