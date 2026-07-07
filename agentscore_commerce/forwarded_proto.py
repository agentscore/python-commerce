"""Scheme-correct a resource URL for TLS-terminating edge proxies."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:
    from collections.abc import Mapping


def apply_forwarded_proto(url: str, forwarded_proto: str | None) -> str:
    """Rewrite a URL's scheme to the proxy's original protocol.

    Behind a TLS-terminating edge proxy (ALB / CloudFront / nginx) the inbound
    request arrives as ``http://``, but x402 discovery — and the mppx client's
    resource-match check — require the public ``https://``. Honor
    ``X-Forwarded-Proto`` (the scheme the client actually used) so the emitted
    ``resource.url`` matches the URL the client fetched. ``forwarded_proto`` may
    carry a comma-separated proxy chain (``"https, http"``); the first hop is the
    client-facing scheme. A missing/blank value leaves the URL untouched (direct
    HTTP in local dev stays ``http://``).
    """
    if not forwarded_proto:
        return url
    proto = forwarded_proto.split(",")[0].strip()
    if not proto:
        return url
    try:
        return urlunparse(urlparse(url)._replace(scheme=proto))
    except ValueError:
        return url


def read_forwarded_proto(headers: Mapping[str, str]) -> str | None:
    """Read ``X-Forwarded-Proto`` from a header mapping regardless of casing."""
    return headers.get("x-forwarded-proto") or headers.get("X-Forwarded-Proto")
