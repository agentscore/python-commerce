"""AIP IdP key discovery (trusted-issuer enforcement + JWKS fetch/cache/select).

Ports node-commerce ``tests/aip_jwks.test.ts``. The Python ``JwksCache`` takes an async
``fetch_impl(url, headers) -> FetchResponse`` (node injects a fetch mock) and an injectable
``now`` clock in **milliseconds** (matching node's ``Date.now``). ``get_key`` is async.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentscore_commerce.aip import (
    DEFAULT_CACHE_SECONDS,
    HARD_MAX_CACHE_SECONDS,
    JwksCache,
    canonicalize_issuer,
    resolve_cache_seconds,
)

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


def _pub_jwk(kid: str, use: str = "sig") -> dict:
    from joserfc.jwk import OKPKey

    pub = OKPKey.import_key(Ed25519PrivateKey.generate()).as_dict(private=False)
    return {**pub, "kid": kid, "use": use}


KEY_A = _pub_jwk("key-A")
KEY_B = _pub_jwk("key-B")
TRUSTED = ["https://issuer.example"]


class _FakeHeaders:
    def __init__(self, cache_control: str | None) -> None:
        self._cc = cache_control

    def get(self, name: str, default: Any = None) -> Any:
        return self._cc if name.lower() == "cache-control" else default


class _FakeResponse:
    def __init__(
        self,
        keys: list[dict],
        *,
        cache_control: str | None = None,
        ok: bool = True,
        status: int = 200,
        body: Any = None,
    ) -> None:
        self._keys = keys
        self._cc = cache_control
        self._ok = ok
        self._status = status
        self._body = body

    @property
    def ok(self) -> bool:
        return self._ok

    @property
    def status(self) -> int:
        return self._status

    @property
    def headers(self) -> _FakeHeaders:
        return _FakeHeaders(self._cc)

    def json(self) -> Any:
        return self._body if self._body is not None else {"keys": self._keys}


def _make_fetch(keys: list[dict], **opts: Any):
    """A counting async fetch impl that returns a fixed response. ``calls`` records invocations."""
    calls: list[tuple[str, dict]] = []

    async def impl(url: str, headers: dict) -> _FakeResponse:
        calls.append((url, headers))
        return _FakeResponse(keys, **opts)

    impl.calls = calls  # type: ignore[attr-defined]
    return impl


# ── canonicalize_issuer ──


class TestCanonicalizeIssuer:
    def test_lowercases_scheme_and_host(self) -> None:
        assert canonicalize_issuer("HTTPS://Issuer.EXAMPLE") == "https://issuer.example"

    def test_drops_a_trailing_slash(self) -> None:
        assert canonicalize_issuer("https://issuer.example/") == "https://issuer.example"

    def test_drops_the_default_https_port(self) -> None:
        assert canonicalize_issuer("https://issuer.example:443") == "https://issuer.example"

    def test_keeps_a_non_default_port(self) -> None:
        assert canonicalize_issuer("https://issuer.example:8443") == "https://issuer.example:8443"

    def test_preserves_a_non_root_path(self) -> None:
        assert canonicalize_issuer("https://idp.example.com/tenant1/") == "https://idp.example.com/tenant1"

    def test_returns_none_for_non_urls(self) -> None:
        assert canonicalize_issuer("not a url") is None

    def test_returns_none_for_malformed_authorities_instead_of_raising(self) -> None:
        # ``iss`` comes from the UNVERIFIED JWT payload — these used to raise ValueError
        # (urlsplit / .port) and crash the verifier with a 500.
        assert canonicalize_issuer("https://host:abc") is None
        assert canonicalize_issuer("https://host:99999999") is None
        assert canonicalize_issuer("https://[::1") is None

    def test_makes_issuer_and_issuer_slash_compare_equal(self) -> None:
        assert canonicalize_issuer("https://issuer.example") == canonicalize_issuer("https://issuer.example/")


# ── resolve_cache_seconds ──


class TestResolveCacheSeconds:
    def test_defaults_when_no_header(self) -> None:
        assert resolve_cache_seconds(None) == DEFAULT_CACHE_SECONDS

    def test_honors_a_reasonable_max_age(self) -> None:
        assert resolve_cache_seconds("max-age=600") == 600

    def test_clamps_to_the_hard_cap(self) -> None:
        assert resolve_cache_seconds("max-age=31536000") == HARD_MAX_CACHE_SECONDS

    def test_falls_back_to_default_on_no_store_no_cache(self) -> None:
        assert resolve_cache_seconds("no-store") == DEFAULT_CACHE_SECONDS
        assert resolve_cache_seconds("no-cache, max-age=999") == DEFAULT_CACHE_SECONDS

    def test_falls_back_to_default_on_a_zero_or_junk_max_age(self) -> None:
        assert resolve_cache_seconds("max-age=0") == DEFAULT_CACHE_SECONDS
        assert resolve_cache_seconds("max-age=abc") == DEFAULT_CACHE_SECONDS


# ── JwksCache.is_trusted ──


class TestIsTrusted:
    def test_matches_canonicalized_issuers(self) -> None:
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=_make_fetch([KEY_A]))
        assert c.is_trusted("https://issuer.example/") is True
        assert c.is_trusted("https://ISSUER.example") is True
        assert c.is_trusted("https://evil.com") is False

    def test_always_trusts_agentscores_canonical_issuer_even_with_no_or_empty_list(self) -> None:
        no_list = JwksCache(fetch_impl=_make_fetch([KEY_A]))
        assert no_list.is_trusted("https://www.agentscore.com") is True
        assert no_list.is_trusted("https://www.agentscore.com/") is True
        assert no_list.is_trusted("https://issuer.example") is False

        empty_list = JwksCache(trusted_issuers=[], fetch_impl=_make_fetch([KEY_A]))
        assert empty_list.is_trusted("https://www.agentscore.com") is True

        with_external = JwksCache(trusted_issuers=["https://issuer.example"], fetch_impl=_make_fetch([KEY_A]))
        assert with_external.is_trusted("https://www.agentscore.com") is True
        assert with_external.is_trusted("https://issuer.example") is True


# ── JwksCache.get_key ──


class TestGetKey:
    async def test_rejects_an_untrusted_issuer_without_fetching(self) -> None:
        fetch = _make_fetch([KEY_A])
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch)
        r = await c.get_key("https://evil.com", "key-A")
        assert (r.ok, r.reason) == (False, "untrusted_issuer")
        assert fetch.calls == []  # type: ignore[attr-defined]

    async def test_rejects_an_http_insecure_trusted_issuer(self) -> None:
        c = JwksCache(trusted_issuers=["http://issuer.example"], fetch_impl=_make_fetch([KEY_A]))
        r = await c.get_key("http://issuer.example", "key-A")
        assert (r.ok, r.reason) == (False, "insecure_issuer")

    async def test_fetches_and_returns_a_key_by_kid(self) -> None:
        fetch = _make_fetch([KEY_A, KEY_B])
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch)
        r = await c.get_key("https://issuer.example", "key-B")
        assert r.ok is True
        assert r.key is not None and r.key["kid"] == "key-B"
        assert len(fetch.calls) == 1  # type: ignore[attr-defined]
        assert fetch.calls[0][0] == "https://issuer.example/.well-known/agent-identity/jwks.json"  # type: ignore[attr-defined]

    async def test_serves_a_second_lookup_from_cache_no_second_fetch(self) -> None:
        fetch = _make_fetch([KEY_A], cache_control="max-age=600")
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch)
        await c.get_key("https://issuer.example", "key-A")
        await c.get_key("https://issuer.example", "key-A")
        assert len(fetch.calls) == 1  # type: ignore[attr-defined]

    async def test_refetches_once_on_a_kid_miss_past_the_cooldown(self) -> None:
        # A kid-miss forces one refetch (rotation may have published the key) — but only ONCE the
        # per-issuer refetch cooldown has elapsed. WITHIN the cooldown a kid-miss is suppressed (the
        # DoS guard); past it, a single refetch is allowed and the rotated key resolves.
        from agentscore_commerce.aip import JWKS_REFETCH_COOLDOWN_SECONDS

        state = {"n": 0}
        now_ms = {"t": 1_000_000}

        async def fetch(url: str, headers: dict) -> _FakeResponse:
            state["n"] += 1
            keys = [KEY_A] if state["n"] == 1 else [KEY_A, KEY_B]
            return _FakeResponse(keys, cache_control="max-age=600")

        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch, now=lambda: now_ms["t"])
        await c.get_key("https://issuer.example", "key-A")  # populates cache (call 1), stamps cooldown
        # Within the cooldown: the key-B miss is suppressed, NO refetch.
        suppressed = await c.get_key("https://issuer.example", "key-B")
        assert (suppressed.ok, suppressed.reason) == (False, "key_not_found")
        assert state["n"] == 1
        # Past the cooldown: one forced refetch picks up the rotated key set.
        now_ms["t"] += JWKS_REFETCH_COOLDOWN_SECONDS * 1000 + 1
        r = await c.get_key("https://issuer.example", "key-B")  # miss → forced refetch (call 2)
        assert r.ok is True
        assert state["n"] == 2

    async def test_returns_key_not_found_when_the_kid_is_absent_after_refresh(self) -> None:
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=_make_fetch([KEY_A]))
        r = await c.get_key("https://issuer.example", "nonexistent")
        assert (r.ok, r.reason) == (False, "key_not_found")

    async def test_unknown_kid_flood_is_suppressed_by_the_refetch_cooldown(self) -> None:
        """DoS guard: a repeated unknown-kid lookup within the cooldown does NOT refetch the JWKS.

        First lookup of an unknown kid warms the cache (one fetch) and stamps the per-issuer
        cooldown; subsequent lookups of the same unknown kid within the cooldown window
        short-circuit to key_not_found WITHOUT another upstream fetch — bounding an attacker's
        unknown-kid flood to ~one fetch per issuer per cooldown, mirroring the API verifier. (The
        distinct-kid variant is covered by test_distinct_unknown_kid_flood_is_bounded_*.)
        """
        from agentscore_commerce.aip import JWKS_REFETCH_COOLDOWN_SECONDS

        now_ms = {"t": 1_000_000}
        # Stable key set across fetches (rotation never adds the requested kid).
        fetch = _make_fetch([KEY_A], cache_control="max-age=600")
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch, now=lambda: now_ms["t"])

        # 1st unknown-kid lookup: warms the cache (one fetch), stamps the cooldown.
        r1 = await c.get_key("https://issuer.example", "attacker-kid")
        assert (r1.ok, r1.reason) == (False, "key_not_found")
        assert len(fetch.calls) == 1  # type: ignore[attr-defined]

        # 2nd + 3rd lookups for the SAME unknown kid within the cooldown: NO additional fetch.
        await c.get_key("https://issuer.example", "attacker-kid")
        r3 = await c.get_key("https://issuer.example", "attacker-kid")
        assert (r3.ok, r3.reason) == (False, "key_not_found")
        assert len(fetch.calls) == 1  # type: ignore[attr-defined]

        # Past the cooldown window: the memo expires and a refetch is allowed again.
        now_ms["t"] += JWKS_REFETCH_COOLDOWN_SECONDS * 1000 + 1
        await c.get_key("https://issuer.example", "attacker-kid")
        assert len(fetch.calls) == 2  # type: ignore[attr-defined]

    async def test_distinct_unknown_kid_flood_is_bounded_to_one_fetch_per_cooldown(self) -> None:
        """CRITICAL DoS guard: a flood of DISTINCT unknown kids is bounded to ~1 fetch per cooldown.

        Regression for the per-(issuer,kid) memo: it only suppressed a REPEAT of the SAME kid, so
        an attacker streaming 500 tokens with 500 DISTINCT unknown kids for the always-trusted
        issuer forced one upstream JWKS GET EACH (unbounded amplification). The per-ISSUER cooldown
        suppresses ALL kid-miss refetches for the issuer for the cooldown window regardless of kid,
        so the whole flood collapses to a single fetch (the one that warmed the cache).
        """
        from agentscore_commerce.aip import JWKS_REFETCH_COOLDOWN_SECONDS

        now_ms = {"t": 1_000_000}
        fetch = _make_fetch([KEY_A], cache_control="max-age=600")
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch, now=lambda: now_ms["t"])

        # 500 DISTINCT unknown kids, all within the cooldown window. The 1st warms the cache (1
        # fetch + stamps the cooldown); the remaining 499 hit the fresh cache, miss, and are
        # suppressed by the cooldown → NO further fetches.
        for i in range(500):
            r = await c.get_key("https://issuer.example", f"attacker-kid-{i}")
            assert (r.ok, r.reason) == (False, "key_not_found")
        assert len(fetch.calls) == 1  # type: ignore[attr-defined]  # ~1 fetch, not 500

        # Sanity: once the cooldown lapses, a single refetch is permitted again (one per window).
        now_ms["t"] += JWKS_REFETCH_COOLDOWN_SECONDS * 1000 + 1
        await c.get_key("https://issuer.example", "attacker-kid-fresh")
        assert len(fetch.calls) == 2  # type: ignore[attr-defined]

    async def test_concurrent_cold_lookups_coalesce_to_one_fetch_single_flight(self) -> None:
        """Single-flight: a concurrent burst of lookups on a COLD cache issues ONE upstream GET.

        Without single-flight, N concurrent get_key calls on a cold/expired issuer each reach
        _refresh before any has populated the cache → N parallel JWKS GETs (the CONCURRENT-refetch
        amplification the per-issuer cooldown, which only bounds SEQUENTIAL refetches, can't stop).
        """
        import asyncio

        fetch_started = asyncio.Event()
        release = asyncio.Event()
        calls = {"n": 0}

        async def slow_fetch(url: str, headers: dict) -> _FakeResponse:
            calls["n"] += 1
            fetch_started.set()
            await release.wait()  # hold the fetch open so all callers pile onto the in-flight one
            return _FakeResponse([KEY_A], cache_control="max-age=600")

        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=slow_fetch)

        # Fire 50 concurrent lookups for the same (cold) issuer + kid.
        tasks = [asyncio.create_task(c.get_key("https://issuer.example", "key-A")) for _ in range(50)]
        await fetch_started.wait()  # the first caller's fetch is now in flight
        release.set()
        results = await asyncio.gather(*tasks)

        assert all(r.ok for r in results)
        assert calls["n"] == 1  # all 50 coalesced onto a single upstream GET

    async def test_kid_published_after_a_miss_recovers_once_the_cooldown_lapses(self) -> None:
        """The negative memo bounds (not strands) a kid published after an earlier miss.

        A miss memoizes the kid and suppresses the immediate next refetch (DoS bound), but once the
        cooldown lapses a fresh fetch picks up the now-rotated key and the stale memo is dropped so
        the key serves normally thereafter.
        """
        from agentscore_commerce.aip import JWKS_REFETCH_COOLDOWN_SECONDS

        now_ms = {"t": 5_000_000}
        state = {"n": 0}

        async def fetch(url: str, headers: dict) -> _FakeResponse:
            state["n"] += 1
            # First fetch: only KEY_A (kid "key-A"); after "rotation": KEY_A + KEY_B.
            keys = [KEY_A] if state["n"] == 1 else [KEY_A, KEY_B]
            return _FakeResponse(keys, cache_control="max-age=600")

        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch, now=lambda: now_ms["t"])
        # 1st lookup of key-B: fetch 1 returns only key-A → miss → memoized.
        miss = await c.get_key("https://issuer.example", "key-B")
        assert (miss.ok, miss.reason) == (False, "key_not_found")
        assert state["n"] == 1
        # Immediate retry within the cooldown is suppressed (no 2nd fetch) — still key_not_found.
        suppressed = await c.get_key("https://issuer.example", "key-B")
        assert (suppressed.ok, suppressed.reason) == (False, "key_not_found")
        assert state["n"] == 1
        # After the cooldown lapses: fetch 2 returns key-A + key-B → resolves, memo dropped.
        now_ms["t"] += JWKS_REFETCH_COOLDOWN_SECONDS * 1000 + 1
        hit = await c.get_key("https://issuer.example", "key-B")
        assert hit.ok is True
        assert hit.key is not None and hit.key["kid"] == "key-B"
        assert state["n"] == 2

    async def test_refetches_after_the_cache_expires_hard_clock_advance(self) -> None:
        now_ms = {"t": 1_000_000}
        fetch = _make_fetch([KEY_A], cache_control="max-age=300")
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch, now=lambda: now_ms["t"])
        await c.get_key("https://issuer.example", "key-A")
        now_ms["t"] += 301_000  # past the 300s window
        await c.get_key("https://issuer.example", "key-A")
        assert len(fetch.calls) == 2  # type: ignore[attr-defined]

    async def test_selects_the_sole_key_when_the_token_header_has_no_kid(self) -> None:
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=_make_fetch([KEY_A]))
        r = await c.get_key("https://issuer.example", None)
        assert r.ok is True

    async def test_refuses_a_no_kid_lookup_when_multiple_keys_exist_ambiguous(self) -> None:
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=_make_fetch([KEY_A, KEY_B]))
        r = await c.get_key("https://issuer.example", None)
        assert (r.ok, r.reason) == (False, "key_not_found")

    async def test_ignores_non_signing_use_keys(self) -> None:
        enc_key = {**KEY_A, "kid": "key-A", "use": "enc"}
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=_make_fetch([enc_key]))
        r = await c.get_key("https://issuer.example", "key-A")
        assert (r.ok, r.reason) == (False, "key_not_found")

    async def test_reports_fetch_failed_on_a_non_ok_response(self) -> None:
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=_make_fetch([KEY_A], ok=False, status=503))
        r = await c.get_key("https://issuer.example", "key-A")
        assert (r.ok, r.reason) == (False, "fetch_failed")

    async def test_reports_malformed_jwks_when_the_body_has_no_keys_array(self) -> None:
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=_make_fetch([], body={"notkeys": 1}))
        r = await c.get_key("https://issuer.example", "key-A")
        assert (r.ok, r.reason) == (False, "malformed_jwks")

    async def test_reports_fetch_failed_when_fetch_throws(self) -> None:
        async def fetch(url: str, headers: dict) -> _FakeResponse:
            raise RuntimeError("network")

        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch)
        r = await c.get_key("https://issuer.example", "key-A")
        assert (r.ok, r.reason) == (False, "fetch_failed")

    async def test_sequential_failures_within_the_cooldown_issue_exactly_one_fetch(self) -> None:
        """Negative cache: a FAILED fetch stamps the refetch cooldown too.

        Without it, a failing issuer leaves no cache entry, so every sequential request refetched
        upstream — the failure path bypassed the DoS cooldown that bounds the success path.
        """
        from agentscore_commerce.aip import JWKS_REFETCH_COOLDOWN_SECONDS

        now_ms = {"t": 1_000_000}
        fetch = _make_fetch([], ok=False, status=503)
        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch, now=lambda: now_ms["t"])

        r1 = await c.get_key("https://issuer.example", "key-A")
        assert (r1.ok, r1.reason) == (False, "fetch_failed")
        # Sequential lookups within the cooldown serve the negative-cached failure, NO refetch.
        r2 = await c.get_key("https://issuer.example", "key-A")
        r3 = await c.get_key("https://issuer.example", "key-B")
        assert (r2.ok, r2.reason) == (False, "fetch_failed")
        assert (r3.ok, r3.reason) == (False, "fetch_failed")
        assert len(fetch.calls) == 1  # type: ignore[attr-defined]

        # Past the cooldown: one refetch is permitted again.
        now_ms["t"] += JWKS_REFETCH_COOLDOWN_SECONDS * 1000 + 1
        await c.get_key("https://issuer.example", "key-A")
        assert len(fetch.calls) == 2  # type: ignore[attr-defined]

    async def test_issuer_recovers_after_a_failure_once_the_cooldown_lapses(self) -> None:
        from agentscore_commerce.aip import JWKS_REFETCH_COOLDOWN_SECONDS

        now_ms = {"t": 1_000_000}
        state = {"n": 0}

        async def fetch(url: str, headers: dict) -> _FakeResponse:
            state["n"] += 1
            if state["n"] == 1:
                return _FakeResponse([], ok=False, status=503)
            return _FakeResponse([KEY_A], cache_control="max-age=600")

        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=fetch, now=lambda: now_ms["t"])
        miss = await c.get_key("https://issuer.example", "key-A")
        assert (miss.ok, miss.reason) == (False, "fetch_failed")
        # Still failing from the negative cache within the cooldown (no upstream call).
        suppressed = await c.get_key("https://issuer.example", "key-A")
        assert (suppressed.ok, suppressed.reason) == (False, "fetch_failed")
        assert state["n"] == 1
        # Cooldown lapses → fresh fetch succeeds → key resolves and the negative cache clears.
        now_ms["t"] += JWKS_REFETCH_COOLDOWN_SECONDS * 1000 + 1
        hit = await c.get_key("https://issuer.example", "key-A")
        assert hit.ok is True
        assert state["n"] == 2
        again = await c.get_key("https://issuer.example", "key-A")
        assert again.ok is True
        assert state["n"] == 2  # served from the now-warm cache

    def test_cold_cache_lookups_across_threads_each_running_asyncio_run_all_succeed(self) -> None:
        """Loop-aware single-flight: threaded WSGI (Flask/Django) runs ``asyncio.run`` per request.

        The old single-flight parked concurrent cold-cache callers on a Future created on ANOTHER
        thread's loop — awaiting it raised RuntimeError → 500. Cross-loop callers must now fetch
        independently instead of awaiting the foreign future; every caller succeeds.
        """
        import asyncio
        import threading

        n_threads = 4
        barrier = threading.Barrier(n_threads)
        results: list[Any] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        async def slow_fetch(url: str, headers: dict) -> _FakeResponse:
            # Hold the fetch open long enough that the other threads' lookups overlap and
            # observe this caller's in-flight entry.
            await asyncio.sleep(0.05)
            return _FakeResponse([KEY_A], cache_control="max-age=600")

        c = JwksCache(trusted_issuers=TRUSTED, fetch_impl=slow_fetch)

        def worker() -> None:
            barrier.wait()
            try:
                r = asyncio.run(c.get_key("https://issuer.example", "key-A"))
                with lock:
                    results.append(r)
            except BaseException as exc:  # the regression raised RuntimeError
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(results) == n_threads
        assert all(r.ok for r in results)


_ = warnings
