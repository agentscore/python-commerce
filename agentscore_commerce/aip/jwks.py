"""AIP IdP key discovery: fetch, cache, and select JWKS signing keys.

Verifiers resolve an IdP's public keys from ``https://{iss}/.well-known/agent-identity/jwks.json``
(the spec's well-known path). This module owns:

* **Trusted-issuer enforcement** — only ``iss`` values on the allowlist are fetched, compared
  after URL canonicalization (lowercase scheme+host, no default port, no trailing slash) so
  ``https://issuer.example`` and ``https://issuer.example/`` match.
* **HTTPS-only** — JWKS over plain HTTP is MITM-vulnerable; we refuse it.
* **Caching with a HARD cap** — we honor ``Cache-Control: max-age`` as advisory but never
  cache longer than :data:`HARD_MAX_CACHE_SECONDS`, regardless of what the IdP sends. A
  compromised IdP can't pin stale keys with ``max-age=31536000``.
* **kid-miss refresh (cooldown-bounded)** — a lookup for a ``kid`` not in the cached set triggers
  one refetch (rotation may have published a new key inside the cache window), but at most once per
  issuer per :data:`JWKS_REFETCH_COOLDOWN_SECONDS` — a per-issuer cooldown so an unknown-``kid``
  flood can't amplify into one upstream JWKS GET per request. Concurrent refreshes single-flight.
* **use:"sig" filtering** — only signing keys are returned.

Pure-ish: the only I/O is the HTTP fetch, injectable for tests via ``fetch_impl``.

This is a behavior-exact port of the reference implementation; the canonicalization,
caching, and selection logic mirror that file line-for-line so an AIT verified by one SDK
resolves the same key set in the other.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

# A JWK is just a JSON object; mirror node's structural `JWK` rather than importing a key class.
Jwk = dict[str, Any]

#: The spec's well-known JWKS path, relative to the issuer origin.
JWKS_WELL_KNOWN_PATH = "/.well-known/agent-identity/jwks.json"

#: Hard ceiling on cache age, regardless of IdP-supplied Cache-Control. (24h)
HARD_MAX_CACHE_SECONDS = 86_400

#: Floor used when the IdP sends no usable cache directive. (5m)
DEFAULT_CACHE_SECONDS = 300

#: Cooldown between forced refetches of an issuer triggered by an unknown ``kid``. A kid-miss
#: normally forces one refetch (rotation may have published a new key inside the cache window), but
#: an unauthenticated attacker can flood unknown-``kid`` tokens for a trusted issuer — ``kid``/``iss``
#: are decoded BEFORE signature verify, so each would otherwise fan out one upstream JWKS fetch. We
#: stamp a per-ISSUER cooldown on every fetch and suppress ALL kid-miss refetches for that issuer
#: while it's warm, bounding the amplification to ~one fetch per issuer per cooldown REGARDLESS of
#: how many DISTINCT unknown kids are streamed. (A per-(issuer,kid) memo would only bound a repeat
#: of the SAME kid — a distinct-kid flood would still fan out one fetch each.) Mirrors the API
#: verifier's jose ``cooldownDuration: 30s`` (``the AgentScore API verifier``
#: ``REFETCH_COOLDOWN_MS``. (30s)
JWKS_REFETCH_COOLDOWN_SECONDS = 30

#: AgentScore's own AIT issuer. ALWAYS trusted by every :class:`JwksCache` (and therefore every
#: gate/adapter built on it) without the merchant listing it — this SDK is the AgentScore
#: verifier, so a merchant can't accidentally fail to trust AgentScore-issued AITs.
#: ``trusted_issuers`` only needs to name ADDITIONAL external issuers.
AGENTSCORE_CANONICAL_ISSUER = "https://www.agentscore.com"

JwksLookupFailure = Literal[
    "untrusted_issuer",
    "insecure_issuer",
    "fetch_failed",
    "malformed_jwks",
    "key_not_found",
]


class FetchResponse(Protocol):
    """Minimal response shape the cache needs — mirrors node's structural ``FetchLike`` return.

    The default fetcher adapts :class:`httpx.Response` to this; injected fetchers (tests)
    implement it directly. ``headers.get`` is case-insensitive for ``cache-control`` lookups,
    matching the Fetch ``Headers`` contract node relies on.
    """

    @property
    def ok(self) -> bool:
        """True for a 2xx status (node's ``Response.ok``)."""
        ...

    @property
    def status(self) -> int:
        """HTTP status code."""
        ...

    @property
    def headers(self) -> Mapping[str, str] | Any:
        """Response headers; ``.get(name)`` returns the header value or ``None``."""
        ...

    def json(self) -> Any:
        """Parsed JSON body. Raises on a non-JSON body (caught → ``malformed_jwks``)."""
        ...


@dataclass(frozen=True)
class JwksLookupResult:
    """Outcome of :meth:`JwksCache.get_key`.

    Discriminated on :attr:`ok`: a hit carries :attr:`key`; a miss carries :attr:`reason`.
    Mirrors the reference ``{ ok: true; key }`` / ``{ ok: false; reason }`` union.
    """

    ok: bool
    key: Jwk | None = None
    reason: JwksLookupFailure | None = None


@dataclass
class _CachedKeys:
    keys: list[Jwk]
    expires_at: float  # ms, comparable against ``now()``


def canonicalize_issuer(iss: str) -> str | None:
    """Canonicalize an issuer URL for trust-list comparison.

    Lowercase scheme + host, drop the default port for the scheme, strip a trailing slash on an
    empty path. Returns ``None`` if the input is not a parseable absolute URL (no scheme/host) or
    has a malformed authority (non-numeric/out-of-range port, unbalanced IPv6 bracket) — matching
    node's try/catch around ``new URL()``.
    """
    # ``iss`` comes from the UNVERIFIED JWT payload: urlsplit / .hostname / .port raise ValueError
    # on malformed authorities ("https://h:abc", "https://h:99999999", "https://[::1"). node's
    # `new URL()` throws there and the caller maps it to untrusted_issuer; any ValueError is
    # likewise "not a URL", never an uncaught crash.
    try:
        parts = urlsplit(iss.strip())
        # node's `new URL(...)` throws for inputs without a scheme+authority (e.g. "not a url",
        # "issuer.example"). Python's urlsplit is lenient and returns empty components, so reject
        # those explicitly to reproduce node's `return null`.
        if not parts.scheme or not parts.hostname:
            return None
        scheme = parts.scheme.lower()
        host = parts.hostname.lower()
        # urlsplit keeps an explicit :443/:80; node's WHATWG URL already dropped it. Mirror node's
        # default-port logic so both collapse to no port. (`port == ""` in node == ``port is None`` here.)
        port = parts.port
    except ValueError:
        return None
    is_default_port = port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    port_part = "" if is_default_port else f":{port}"
    # Drop a trailing slash when the path is just "/". node strips exactly ONE trailing slash via
    # /\/$/ (so "//" -> "/"), so use a single-char slice rather than rstrip (which would over-strip).
    path = parts.path
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path[:-1]
    return f"{scheme}://{host}{port_part}{path}"


_MAX_AGE_RE = re.compile(r"\bmax-age\s*=\s*(\d+)", re.IGNORECASE)
_NO_STORE_RE = re.compile(r"\bno-store\b", re.IGNORECASE)
_NO_CACHE_RE = re.compile(r"\bno-cache\b", re.IGNORECASE)


def resolve_cache_seconds(cache_control: str | None) -> int:
    """Parse ``Cache-Control: max-age=N``, clamped to the hard cap. Returns seconds."""
    if not cache_control:
        return DEFAULT_CACHE_SECONDS
    if _NO_STORE_RE.search(cache_control) or _NO_CACHE_RE.search(cache_control):
        return DEFAULT_CACHE_SECONDS
    m = _MAX_AGE_RE.search(cache_control)
    if not m:
        return DEFAULT_CACHE_SECONDS
    advertised = int(m.group(1))
    # node guards `!Number.isFinite || advertised <= 0`; an int from \d+ is always finite, so only
    # the non-positive check bites here (e.g. "max-age=0").
    if advertised <= 0:
        return DEFAULT_CACHE_SECONDS
    return min(advertised, HARD_MAX_CACHE_SECONDS)


def _signing_keys(keys: list[Jwk]) -> list[Jwk]:
    """Extract ``use: "sig"`` keys (or keys with no ``use``, which default to usable for sig)."""
    return [k for k in keys if k.get("use") is None or k.get("use") == "sig"]


async def _default_fetch(url: str, headers: dict[str, str]) -> FetchResponse:
    """Default fetcher: an httpx GET adapted to the :class:`FetchResponse` shape.

    Imported lazily so importing this module doesn't pull httpx into non-fetching flows and so a
    test that injects ``fetch_impl`` never touches the network.
    """
    import httpx

    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
    return _HttpxResponse(res)


class _HttpxResponse:
    """Adapt :class:`httpx.Response` to :class:`FetchResponse` (node's ``Response`` surface)."""

    def __init__(self, res: Any) -> None:
        self._res = res

    @property
    def ok(self) -> bool:
        return bool(self._res.is_success)

    @property
    def status(self) -> int:
        return int(self._res.status_code)

    @property
    def headers(self) -> Any:
        # httpx.Headers.get is case-insensitive, matching the Fetch Headers contract.
        return self._res.headers

    def json(self) -> Any:
        return self._res.json()


class JwksCache:
    """JWKS resolver bound to a trusted-issuer allowlist.

    One instance can serve many issuers; each issuer's key set is cached independently. Behavior
    mirrors the reference ``JwksCache``.
    """

    def __init__(
        self,
        *,
        trusted_issuers: list[str] | None = None,
        fetch_impl: Callable[[str, dict[str, str]], Awaitable[FetchResponse]] | None = None,
        now: Callable[[], float] | None = None,
        user_agent: str = "agentscore-commerce",
    ) -> None:
        """Build a cache.

        Args:
            trusted_issuers: ADDITIONAL external issuer URLs to trust beyond AgentScore's own
                (compared after canonicalization). AgentScore's canonical issuer is always
                trusted; omit/empty to accept only AgentScore-issued AITs.
            fetch_impl: Injectable async fetcher ``(url, headers) -> FetchResponse`` (defaults to
                an httpx GET). For tests.
            now: Injectable clock returning milliseconds (defaults to wall-clock ms). For tests.
            user_agent: User-Agent for JWKS requests.
        """
        # AgentScore's own issuer is always trusted, plus any additional external issuers.
        # Canonicalize both so a merchant-supplied duplicate (or trailing-slash variant) of the
        # canonical issuer collapses into the same set entry.
        self._trusted: set[str] = {
            canon
            for canon in (canonicalize_issuer(iss) for iss in [AGENTSCORE_CANONICAL_ISSUER, *(trusted_issuers or [])])
            if canon is not None
        }
        self._fetch_impl = fetch_impl if fetch_impl is not None else _default_fetch
        # node uses Date.now (ms); keep ms so injected clocks (and TTL math) match node 1:1.
        self._now = now if now is not None else (lambda: time.time() * 1000)
        self._user_agent = user_agent
        self._cache: dict[str, _CachedKeys] = {}
        # Per-issuer refetch cooldown (ms timestamp), stamped on EVERY fetch — success AND failure.
        # This is the per-ISSUER refetch-amplification / DoS guard: it caps JWKS GETs at ~1 per
        # issuer per cooldown regardless of how many DISTINCT unknown ``kid``s an attacker floods,
        # and (the failure stamp) keeps a failing issuer from being refetched on every sequential
        # request. Mirrors the reference cooldown map.
        self._cooldown: dict[str, float] = {}
        # Negative cache: the failure result of the most recent FAILED fetch, served verbatim to
        # lookups that land within the cooldown window (a failed fetch leaves no ``_cache`` entry,
        # so without this the no-cache path would refetch every request). Cleared on success.
        self._failure: dict[str, JwksLookupResult] = {}
        # Per-issuer in-flight refresh future — coalesces CONCURRENT refreshes to ONE upstream
        # fetch. Without it, a concurrent burst of distinct-kid lookups on a cold/expired cache each
        # call _refresh() before any has populated the cache → N parallel JWKS GETs (refetch
        # amplification). The cooldown only suppresses SEQUENTIAL refetches; single-flight suppresses
        # CONCURRENT ones. Keyed alongside the loop that created the future: sync adapters (Flask /
        # Django) run ``asyncio.run()`` per request on worker threads, and awaiting a future created
        # on ANOTHER thread's loop raises RuntimeError — so coalescing is same-loop only. The entry
        # is cleared once the fetch settles. Mirrors the reference `inflight` map.
        self._inflight: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future[JwksLookupResult]]] = {}

    def is_trusted(self, iss: str) -> bool:
        """Is this issuer on the canonicalized trust list?"""
        canon = canonicalize_issuer(iss)
        return canon is not None and canon in self._trusted

    async def get_key(self, iss: str, kid: str | None) -> JwksLookupResult:
        """Resolve the signing key for ``(iss, kid)``.

        Enforces trust + HTTPS, serves from cache when fresh, and refetches once on a kid-miss
        before giving up.
        """
        canon = canonicalize_issuer(iss)
        if canon is None or canon not in self._trusted:
            return JwksLookupResult(ok=False, reason="untrusted_issuer")
        if not canon.startswith("https://"):
            return JwksLookupResult(ok=False, reason="insecure_issuer")

        cached = self._cache.get(canon)
        if cached is not None and self._now() < cached.expires_at:
            hit = self._select(cached.keys, kid)
            if hit is not None:
                return JwksLookupResult(ok=True, key=hit)
            # kid miss within the cache window. Normally we'd force one refetch (rotation may have
            # published a new key) — but only once the per-ISSUER refetch cooldown has elapsed.
            # WITHIN the cooldown we return key_not_found WITHOUT refetching. This caps JWKS GETs
            # at ~1 per issuer per cooldown regardless of how many DISTINCT unknown-kid tokens an
            # attacker streams (the DoS guard). Once the cooldown passes we fall through to a
            # single refetch, because rotation may have published the kid since.
            if self._now() < self._cooldown.get(canon, 0.0):
                return JwksLookupResult(ok=False, reason="key_not_found")
            # Past the cooldown: fall through to a single forced refetch below.
        elif self._now() < self._cooldown.get(canon, 0.0):
            # No fresh cache but a cooldown is active: a recent fetch FAILED (a failed fetch leaves
            # no cache entry). Serve the negative-cached failure so sequential lookups of a failing
            # issuer collapse to one upstream fetch per cooldown window instead of one per request.
            failure = self._failure.get(canon)
            if failure is not None:
                return failure

        refreshed = await self._refresh(canon)
        if not refreshed.ok:
            # _refresh returns a JwksLookupResult on failure; propagate the reason.
            return refreshed

        # On success _refresh stashed the keys in the cache; re-select from the fresh set.
        fresh = self._cache.get(canon)
        keys = fresh.keys if fresh is not None else []
        hit = self._select(keys, kid)
        if hit is not None:
            return JwksLookupResult(ok=True, key=hit)
        # Still missing after a fresh fetch — the cooldown stamped by that fetch suppresses the
        # next refetch for this issuer anyway.
        return JwksLookupResult(ok=False, reason="key_not_found")

    def _select(self, keys: list[Jwk], kid: str | None) -> Jwk | None:
        candidates = _signing_keys(keys)
        if kid is not None:
            return next((k for k in candidates if k.get("kid") == kid), None)
        # No kid in the token header: only safe when exactly one signing key exists.
        return candidates[0] if len(candidates) == 1 else None

    async def _refresh(self, canon_issuer: str) -> JwksLookupResult:
        """Refresh the cached key set, coalescing CONCURRENT same-loop callers onto ONE fetch.

        The first caller for an issuer with no in-flight refresh kicks off the fetch and registers
        the future; concurrent callers ON THE SAME LOOP await that future instead of issuing their
        own GET. A caller on a DIFFERENT running loop (threaded WSGI: Flask/Django run
        ``asyncio.run()`` per request) must NOT await the foreign future — awaiting a future bound
        to another thread's loop raises RuntimeError — so it performs its own fetch instead
        (correctness over cross-loop dedupe). Each entry is cleared by its creator once the fetch
        settles so the next cold/expired lookup can refresh again. Mirrors the reference ``refresh`` +
        ``inflight`` single-flight.
        """
        # Inside a coroutine there is always a running loop; get_running_loop is the correct call.
        loop = asyncio.get_running_loop()
        existing = self._inflight.get(canon_issuer)
        if existing is not None and existing[0] is loop:
            return await existing[1]
        future: asyncio.Future[JwksLookupResult] = loop.create_future()
        entry = (loop, future)
        self._inflight[canon_issuer] = entry
        try:
            result = await self._fetch_and_cache(canon_issuer)
        except BaseException as exc:
            if self._inflight.get(canon_issuer) is entry:
                self._inflight.pop(canon_issuer, None)
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if self._inflight.get(canon_issuer) is entry:
                self._inflight.pop(canon_issuer, None)
            if not future.done():
                future.set_result(result)
            return result

    async def _fetch_and_cache(self, canon_issuer: str) -> JwksLookupResult:
        """Fetch + cache the issuer's JWKS. Returns ``ok=True`` (keys cached) or a failure reason."""
        url = f"{canon_issuer}{JWKS_WELL_KNOWN_PATH}"
        try:
            res = await self._fetch_impl(
                url,
                {
                    "User-Agent": self._user_agent,
                    "Accept": "application/jwk-set+json, application/json",
                },
            )
        except Exception:
            # node catches every fetch throw as `fetch_failed` (network error, DNS, TLS, ...).
            return self._fetch_failure(canon_issuer, "fetch_failed")
        if not res.ok:
            return self._fetch_failure(canon_issuer, "fetch_failed")

        try:
            body = res.json()
        except Exception:
            # Any JSON-decode failure is `malformed_jwks`, mirroring node's catch on `res.json()`.
            return self._fetch_failure(canon_issuer, "malformed_jwks")
        if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
            return self._fetch_failure(canon_issuer, "malformed_jwks")
        keys: list[Jwk] = body["keys"]

        ttl_seconds = resolve_cache_seconds(_header_get(res.headers, "cache-control"))
        now = self._now()
        # Stamp the cooldown window on every fetch so a kid-miss can't trigger another refetch for
        # JWKS_REFETCH_COOLDOWN_SECONDS.
        self._cache[canon_issuer] = _CachedKeys(keys=keys, expires_at=now + ttl_seconds * 1000)
        self._cooldown[canon_issuer] = now + JWKS_REFETCH_COOLDOWN_SECONDS * 1000
        self._failure.pop(canon_issuer, None)
        return JwksLookupResult(ok=True)

    def _fetch_failure(self, canon_issuer: str, reason: JwksLookupFailure) -> JwksLookupResult:
        """Negative-cache a failed fetch.

        Stamps the cooldown (as on success) and remembers the failure result so sequential
        lookups of a failing issuer within the window are served from the cache instead of
        refetching on every request.
        """
        result = JwksLookupResult(ok=False, reason=reason)
        self._cooldown[canon_issuer] = self._now() + JWKS_REFETCH_COOLDOWN_SECONDS * 1000
        self._failure[canon_issuer] = result
        return result


def _header_get(headers: Any, name: str) -> str | None:
    """Read a header case-insensitively across the dict / httpx.Headers / Fetch-style surfaces."""
    getter = getattr(headers, "get", None)
    if callable(getter):
        return getter(name)
    return None


__all__ = [
    "AGENTSCORE_CANONICAL_ISSUER",
    "DEFAULT_CACHE_SECONDS",
    "HARD_MAX_CACHE_SECONDS",
    "JWKS_REFETCH_COOLDOWN_SECONDS",
    "JWKS_WELL_KNOWN_PATH",
    "FetchResponse",
    "JwksCache",
    "JwksLookupFailure",
    "JwksLookupResult",
    "canonicalize_issuer",
    "resolve_cache_seconds",
]
