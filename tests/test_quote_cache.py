"""Tests for ``agentscore_commerce.quote_cache``."""

import pytest

from agentscore_commerce.quote_cache import CachedQuote, create_quote_cache


def test_body_hash_key_stable_across_key_order() -> None:
    cache = create_quote_cache()
    a = cache.body_hash_key("search", {"q": "x", "limit": 5})
    b = cache.body_hash_key("search", {"limit": 5, "q": "x"})
    assert a == b


def test_body_hash_key_changes_on_value_change() -> None:
    cache = create_quote_cache()
    a = cache.body_hash_key("search", {"q": "x"})
    b = cache.body_hash_key("search", {"q": "y"})
    assert a != b


def test_body_hash_key_prefix_isolates_namespaces() -> None:
    cache = create_quote_cache()
    a = cache.body_hash_key("search", {"q": "x"})
    b = cache.body_hash_key("enrich", {"q": "x"})
    assert a != b


def test_body_hash_key_canonicalizes_nested_lists() -> None:
    """List values are canonicalized element-wise; list order is significant
    (exercises the list branch of ``_canonicalize``)."""
    cache = create_quote_cache()
    # Nested dicts inside a list get key-sorted, so reordered inner keys hash equal.
    a = cache.body_hash_key("search", {"items": [{"a": 1, "b": 2}]})
    b = cache.body_hash_key("search", {"items": [{"b": 2, "a": 1}]})
    assert a == b
    # But list element ORDER matters.
    c = cache.body_hash_key("search", {"items": [{"a": 1}, {"a": 2}]})
    d = cache.body_hash_key("search", {"items": [{"a": 2}, {"a": 1}]})
    assert c != d


@pytest.mark.asyncio
async def test_write_then_read_returns_cached_quote() -> None:
    cache = create_quote_cache()
    key = cache.body_hash_key("search", {"q": "x"})
    await cache.write(key, {"matches": [1, 2]}, 3, recipients={"tempo": "0xabc"})
    quote = await cache.read(key)
    assert isinstance(quote, CachedQuote)
    assert quote.body == {"matches": [1, 2]}
    assert quote.price_cents == 3
    assert quote.recipients == {"tempo": "0xabc"}


@pytest.mark.asyncio
async def test_read_returns_none_for_missing_key() -> None:
    cache = create_quote_cache()
    assert await cache.read("missing") is None


@pytest.mark.asyncio
async def test_clear_drops_entries() -> None:
    cache = create_quote_cache()
    key = cache.body_hash_key("search", {"q": "x"})
    await cache.write(key, {}, 1)
    await cache.clear()
    assert await cache.read(key) is None


@pytest.mark.asyncio
async def test_default_recipients_empty_when_omitted() -> None:
    cache = create_quote_cache()
    key = cache.body_hash_key("search", {"q": "x"})
    await cache.write(key, {"r": 1}, 5)
    quote = await cache.read(key)
    assert quote is not None
    assert quote.recipients == {}
