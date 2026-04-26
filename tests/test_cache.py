import threading
import time

from agentscore_commerce.identity.cache import TTLCache


def test_get_returns_none_for_missing_key():
    cache: TTLCache[str] = TTLCache(default_ttl_seconds=60)
    assert cache.get("nonexistent") is None


def test_set_and_get():
    cache: TTLCache[int] = TTLCache(default_ttl_seconds=60)
    cache.set("key", 42)
    assert cache.get("key") == 42


def test_entry_expires(monkeypatch):
    real_monotonic = time.monotonic

    cache: TTLCache[str] = TTLCache(default_ttl_seconds=1)
    cache.set("key", "value")
    assert cache.get("key") == "value"

    # Fast-forward time by patching monotonic.
    base = real_monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 2)
    assert cache.get("key") is None


def test_custom_ttl(monkeypatch):
    real_monotonic = time.monotonic

    cache: TTLCache[str] = TTLCache(default_ttl_seconds=60)
    cache.set("key", "value", ttl=1)

    base = real_monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 2)
    assert cache.get("key") is None


def test_overwrite_value():
    cache: TTLCache[str] = TTLCache(default_ttl_seconds=60)
    cache.set("key", "a")
    cache.set("key", "b")
    assert cache.get("key") == "b"


def test_max_size_evicts_oldest():
    """When cache exceeds max_size, oldest entries (by expiry) are evicted."""
    cache: TTLCache[str] = TTLCache(default_ttl_seconds=60, max_size=5)

    # Fill cache to capacity
    for i in range(5):
        cache.set(f"key-{i}", f"value-{i}")

    # All 5 entries should be present
    for i in range(5):
        assert cache.get(f"key-{i}") == f"value-{i}"

    # Adding a 6th entry should evict the oldest (key-0, earliest expiry)
    cache.set("key-5", "value-5")
    assert cache.get("key-5") == "value-5"

    # key-0 should have been evicted (it had the earliest expiry)
    assert cache.get("key-0") is None

    # Remaining keys should still be present
    for i in range(1, 6):
        assert cache.get(f"key-{i}") == f"value-{i}"


def test_max_size_sweeps_expired_first(monkeypatch):
    """Expired entries are swept before evicting by age."""
    real_monotonic = time.monotonic

    cache: TTLCache[str] = TTLCache(default_ttl_seconds=60, max_size=5)

    # Add 5 entries, 2 with short TTL
    cache.set("short-1", "v", ttl=1)
    cache.set("short-2", "v", ttl=1)
    cache.set("long-1", "v")
    cache.set("long-2", "v")
    cache.set("long-3", "v")

    # Advance time so short-TTL entries expire
    base = real_monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 2)

    # Adding a new entry should sweep expired entries, not evict long-lived ones
    cache.set("new", "v")
    assert cache.get("new") == "v"
    assert cache.get("long-1") == "v"
    assert cache.get("long-2") == "v"
    assert cache.get("long-3") == "v"


def test_overwrite_resets_ttl(monkeypatch):
    real_monotonic = time.monotonic

    cache: TTLCache[str] = TTLCache(default_ttl_seconds=2)
    cache.set("key", "first")

    base = real_monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 1.5)

    cache.set("key", "second")

    monkeypatch.setattr(time, "monotonic", lambda: base + 3.0)
    assert cache.get("key") == "second"


def test_concurrent_access():
    """Concurrent reads and writes should not corrupt cache state."""
    cache: TTLCache[int] = TTLCache(default_ttl_seconds=60)
    errors: list[Exception] = []
    num_threads = 10
    ops_per_thread = 200

    def writer(thread_id: int) -> None:
        try:
            for i in range(ops_per_thread):
                cache.set(f"key-{thread_id}-{i}", i)
        except Exception as exc:
            errors.append(exc)

    def reader(thread_id: int) -> None:
        try:
            for i in range(ops_per_thread):
                cache.get(f"key-{thread_id}-{i}")
        except Exception as exc:
            errors.append(exc)

    threads = []
    for t in range(num_threads):
        threads.append(threading.Thread(target=writer, args=(t,)))
        threads.append(threading.Thread(target=reader, args=(t,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent access caused errors: {errors}"

    # Verify all written values are readable.
    for t in range(num_threads):
        for i in range(ops_per_thread):
            val = cache.get(f"key-{t}-{i}")
            assert val == i
