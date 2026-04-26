from __future__ import annotations

import threading
import time
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Simple in-memory cache with per-entry TTL expiry."""

    def __init__(self, default_ttl_seconds: float, max_size: int = 10000) -> None:
        self._store: dict[str, tuple[T, float]] = {}
        self._default_ttl = default_ttl_seconds
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, key: str) -> T | None:
        """Return the cached value, or ``None`` if missing or expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: T, ttl: float | None = None) -> None:
        """Store *value* under *key* with an optional custom TTL (seconds)."""
        with self._lock:
            if len(self._store) >= self._max_size and key not in self._store:
                self._sweep_expired()
                if len(self._store) >= self._max_size:
                    self._evict_oldest()
            self._store[key] = (value, time.monotonic() + (ttl if ttl is not None else self._default_ttl))

    def _sweep_expired(self) -> None:
        """Remove all expired entries. Must be called with ``_lock`` held."""
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]

    def _evict_oldest(self) -> None:
        """Evict entries with the earliest expiry until under max_size. Must be called with ``_lock`` held."""
        entries = sorted(self._store.items(), key=lambda item: item[1][1])
        while len(self._store) >= self._max_size and entries:
            del self._store[entries.pop(0)[0]]
