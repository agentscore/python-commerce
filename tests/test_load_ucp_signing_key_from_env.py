"""Tests for ``load_ucp_signing_key_from_env`` — env-driven UCP signing-key loader.

Locked behavior contract (shared with the Node sibling at
``node-commerce/tests/identity/load-ucp-signing-key-from-env.test.ts``):

* env JWK present → load + validate kty/crv (OKP+Ed25519 or EC+P-256), project to canonical public JWK
* env JWK absent → generate ephemeral key (logs loud warning)
* malformed JSON → ValueError naming the env var
* unsupported kty/crv → ValueError naming the actual kty/crv
* malformed key material → sanitized ValueError (no key bytes in the message)
* whitespace-only env value → treated as absent
* embedded kid in JWK wins over env kid; empty-string kid falls through to default
* concurrent first-callers see the same cached key (lock-protected)
* different opts get separate cache entries
"""

from __future__ import annotations

import json

import pytest
from joserfc.jwk import ECKey, OKPKey

from agentscore_commerce.identity.ucp_jwks import (
    _reset_ucp_signing_key_cache,
    load_ucp_signing_key_from_env,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    _reset_ucp_signing_key_cache()


def _build_ed25519_jwk() -> dict:
    """Generate an Ed25519 JWK (with the private ``d`` field) for env-loading tests."""
    return OKPKey.generate_key(crv="Ed25519").as_dict(private=True)


def _build_p256_jwk() -> dict:
    """Generate a P-256 JWK (with the private ``d`` field) for env-loading tests."""
    return ECKey.generate_key(crv="P-256").as_dict(private=True)


# ─── env JWK present: happy paths ────────────────────────────────────────────


def test_loads_ed25519_jwk_from_env(monkeypatch) -> None:
    private_jwk = _build_ed25519_jwk()
    private_jwk["kid"] = "test-ed25519-key"
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", json.dumps(private_jwk))

    result = load_ucp_signing_key_from_env()

    assert result.public_jwk["kty"] == "OKP"
    assert result.public_jwk["crv"] == "Ed25519"
    assert result.public_jwk["alg"] == "EdDSA"
    assert result.public_jwk["use"] == "sig"
    assert result.public_jwk["kid"] == "test-ed25519-key"
    assert "d" not in result.public_jwk  # private field stripped


def test_loads_es256_jwk_from_env(monkeypatch) -> None:
    private_jwk = _build_p256_jwk()
    private_jwk["kid"] = "test-p256-key"
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", json.dumps(private_jwk))

    result = load_ucp_signing_key_from_env()

    assert result.public_jwk["kty"] == "EC"
    assert result.public_jwk["crv"] == "P-256"
    assert result.public_jwk["alg"] == "ES256"
    assert result.public_jwk["kid"] == "test-p256-key"
    assert "d" not in result.public_jwk


# ─── kid precedence ──────────────────────────────────────────────────────────


def test_embedded_kid_wins_over_env_kid_default(monkeypatch) -> None:
    private_jwk = _build_ed25519_jwk()
    private_jwk["kid"] = "embedded-kid"
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", json.dumps(private_jwk))
    monkeypatch.setenv("UCP_SIGNING_KEY_KID", "env-kid")

    result = load_ucp_signing_key_from_env()
    assert result.public_jwk["kid"] == "embedded-kid"


def test_empty_string_embedded_kid_falls_through_to_env(monkeypatch) -> None:
    """An env JWK with ``kid: ""`` would publish empty kid; helper falls back to env kid."""
    private_jwk = _build_ed25519_jwk()
    private_jwk["kid"] = ""
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", json.dumps(private_jwk))
    monkeypatch.setenv("UCP_SIGNING_KEY_KID", "fallback-kid")

    result = load_ucp_signing_key_from_env()
    assert result.public_jwk["kid"] == "fallback-kid"


def test_missing_embedded_kid_falls_through_to_default(monkeypatch) -> None:
    private_jwk = _build_ed25519_jwk()
    private_jwk.pop("kid", None)
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", json.dumps(private_jwk))
    monkeypatch.delenv("UCP_SIGNING_KEY_KID", raising=False)

    result = load_ucp_signing_key_from_env(default_kid="opts-default")
    assert result.public_jwk["kid"] == "opts-default"


# ─── canonical public JWK projection ─────────────────────────────────────────


def test_unknown_env_jwk_fields_dropped_from_public_jwk(monkeypatch) -> None:
    """``key_ops``, ``x5c``, ``x5t``, ``x5u`` etc. on the env JWK don't leak into JWKS."""
    private_jwk = _build_ed25519_jwk()
    private_jwk["kid"] = "test-kid"
    private_jwk["key_ops"] = ["sign", "verify"]
    private_jwk["x5c"] = ["fake-cert"]
    private_jwk["x5t"] = "fake-thumbprint"
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", json.dumps(private_jwk))

    result = load_ucp_signing_key_from_env()

    assert "key_ops" not in result.public_jwk
    assert "x5c" not in result.public_jwk
    assert "x5t" not in result.public_jwk


# ─── env JWK absent: ephemeral fallback ──────────────────────────────────────


def test_generates_ephemeral_key_when_env_jwk_missing(monkeypatch) -> None:
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    monkeypatch.delenv("UCP_SIGNING_KEY_KID", raising=False)

    result = load_ucp_signing_key_from_env()

    assert result.public_jwk["kty"] == "OKP"  # default alg is EdDSA
    assert result.public_jwk["alg"] == "EdDSA"
    assert result.public_jwk["kid"] == "merchant-default"  # default kwarg


def test_ephemeral_respects_default_alg_options(monkeypatch) -> None:
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    result = load_ucp_signing_key_from_env(default_alg="ES256")
    assert result.public_jwk["alg"] == "ES256"
    assert result.public_jwk["kty"] == "EC"


def test_env_alg_overrides_default_in_ephemeral_path(monkeypatch) -> None:
    """When env JWK is absent, env alg (case-insensitive) is honored."""
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    monkeypatch.setenv("UCP_SIGNING_KEY_ALG", "es256")  # lowercase
    result = load_ucp_signing_key_from_env()
    assert result.public_jwk["alg"] == "ES256"


def test_env_alg_case_insensitive_strict_match(monkeypatch) -> None:
    """Unrecognized env alg falls back to default."""
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    monkeypatch.setenv("UCP_SIGNING_KEY_ALG", "rs256")  # not supported
    result = load_ucp_signing_key_from_env()
    assert result.public_jwk["alg"] == "EdDSA"  # falls to default


# ─── whitespace handling ─────────────────────────────────────────────────────


def test_whitespace_only_env_treated_as_absent(monkeypatch) -> None:
    """``aws secretsmanager get-secret-value | xargs`` appends ``\\n``; helper trims it."""
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", "   \n\t  ")
    result = load_ucp_signing_key_from_env()
    # Falls through to ephemeral
    assert result.public_jwk["alg"] == "EdDSA"


def test_env_kid_whitespace_trimmed(monkeypatch) -> None:
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    monkeypatch.setenv("UCP_SIGNING_KEY_KID", "  trimmed-kid  ")
    result = load_ucp_signing_key_from_env()
    assert result.public_jwk["kid"] == "trimmed-kid"


# ─── error paths ─────────────────────────────────────────────────────────────


def test_malformed_json_raises_naming_env_var(monkeypatch) -> None:
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", "{not valid json")
    with pytest.raises(ValueError, match="UCP_SIGNING_KEY_JWK_PRIVATE is not valid JSON"):
        load_ucp_signing_key_from_env()


def test_unsupported_kty_crv_raises_naming_actual(monkeypatch) -> None:
    monkeypatch.setenv(
        "UCP_SIGNING_KEY_JWK_PRIVATE",
        json.dumps({"kty": "RSA", "n": "abc", "e": "AQAB"}),
    )
    with pytest.raises(ValueError, match=r"unsupported kty/crv.*kty='RSA'"):
        load_ucp_signing_key_from_env()


def test_jwk_not_an_object_raises(monkeypatch) -> None:
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", "[1, 2, 3]")
    with pytest.raises(ValueError, match="must be a non-empty JWK object"):
        load_ucp_signing_key_from_env()


def test_empty_jwk_object_raises(monkeypatch) -> None:
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", "{}")
    with pytest.raises(ValueError, match="must be a non-empty JWK object"):
        load_ucp_signing_key_from_env()


def test_malformed_key_material_sanitizes_underlying_error(monkeypatch) -> None:
    """Underlying joserfc exception is replaced with a class-only message; key bytes never leak."""
    bad_jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": "this-is-not-base64-key-material",
        "d": "leaked-secret-should-not-appear",
    }
    monkeypatch.setenv("UCP_SIGNING_KEY_JWK_PRIVATE", json.dumps(bad_jwk))
    with pytest.raises(ValueError) as exc_info:
        load_ucp_signing_key_from_env()
    # The secret in `d` must NOT appear in the surfaced error message
    assert "leaked-secret-should-not-appear" not in str(exc_info.value)
    assert "Underlying details suppressed to avoid leaking key bytes" in str(exc_info.value)


# ─── caching + concurrency ───────────────────────────────────────────────────


def test_repeated_calls_return_cached_key(monkeypatch) -> None:
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    first = load_ucp_signing_key_from_env()
    second = load_ucp_signing_key_from_env()
    assert first is second


def test_different_opts_get_separate_cache_entries(monkeypatch) -> None:
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    first = load_ucp_signing_key_from_env(default_kid="kid-a")
    second = load_ucp_signing_key_from_env(default_kid="kid-b")
    assert first is not second
    assert first.public_jwk["kid"] == "kid-a"
    assert second.public_jwk["kid"] == "kid-b"


def test_concurrent_first_callers_share_same_key(monkeypatch) -> None:
    """Lock prevents two concurrent ephemeral generations from racing."""
    import threading

    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    results: list = []

    def call() -> None:
        results.append(load_ucp_signing_key_from_env())

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All 8 callers received the same key object (lock-protected single generation)
    assert all(r is results[0] for r in results)


def test_reset_cache_clears_entries(monkeypatch) -> None:
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)
    first = load_ucp_signing_key_from_env()
    _reset_ucp_signing_key_cache()
    second = load_ucp_signing_key_from_env()
    # New ephemeral key generated after reset (not the same object)
    assert first is not second


def test_env_var_overridable_via_opts(monkeypatch) -> None:
    """A merchant running multiple keys from different env namespaces sees separate state."""
    private_jwk = _build_ed25519_jwk()
    private_jwk["kid"] = "prod-key"
    monkeypatch.setenv("PROD_UCP_JWK", json.dumps(private_jwk))
    monkeypatch.delenv("UCP_SIGNING_KEY_JWK_PRIVATE", raising=False)

    result = load_ucp_signing_key_from_env(env_jwk_var="PROD_UCP_JWK")
    assert result.public_jwk["kid"] == "prod-key"
