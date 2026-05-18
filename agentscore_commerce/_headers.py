"""Internal header helpers — case-normalization for HTTP headers.

Replaces hand-rolled ``{k.lower(): v for k, v in headers.items()}`` loops in
``checkout``, ``signer`` and ``challenge.respond_402``. Mirrors node-commerce
``src/_headers.ts``.

Not part of the public API; consumed by SDK internals only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def normalize_headers_to_lowercase(headers: Mapping[str, str]) -> dict[str, str]:
    """Lowercase every header key, preserve values. Idempotent."""
    return {k.lower(): v for k, v in headers.items()}
