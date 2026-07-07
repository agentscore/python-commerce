"""Build a VerifyRequestContext from a framework request / raw parts.

Ports node-commerce ``tests/aip_request.test.ts``. Node's ``buildVerifyContextFromRequest`` takes a
WHATWG ``Request``; the Python analog accepts any object exposing ``method`` / ``url`` / ``headers``
(a Starlette/FastAPI-style request). We use a tiny ``_FakeRequest`` with a Starlette ``Headers``
multidict (which supports ``getlist`` for repeated ``Agent-Identity`` headers) to mirror node's
``make()`` helper. ``buildVerifyContextFromParts`` takes a raw header map + method + url.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.datastructures import Headers

from agentscore_commerce.aip import (
    build_verify_context_from_parts,
    build_verify_context_from_request,
    has_agent_identity_header,
)


@dataclass
class _FakeRequest:
    method: str
    url: str
    headers: Headers


def _make(
    headers: dict[str, str] | list[tuple[bytes, bytes]],
    url: str = "https://wine-merchant.com/checkout",
    method: str = "POST",
) -> _FakeRequest:
    h = Headers(raw=headers) if isinstance(headers, list) else Headers(headers)
    return _FakeRequest(method=method, url=url, headers=h)


# ── build_verify_context_from_request ──


class TestBuildVerifyContextFromRequest:
    def test_extracts_method_path_and_authority_from_the_host_header(self) -> None:
        ctx = build_verify_context_from_request(
            _make(
                {
                    "host": "wine-merchant.com",
                    "agent-identity": "jwt.a.b",
                    "signature-input": "si",
                    "signature": "sig",
                }
            )
        )
        assert ctx.method == "POST"
        assert ctx.path == "/checkout"
        assert ctx.authority == "wine-merchant.com"
        assert ctx.agent_identity_headers == ["jwt.a.b"]
        assert ctx.signature_input == "si"
        assert ctx.signature == "sig"

    def test_falls_back_to_the_url_host_when_no_host_header_is_set(self) -> None:
        ctx = build_verify_context_from_request(_make({"agent-identity": "x.y.z"}, "https://api.example.com:3003/path"))
        assert ctx.authority == "api.example.com:3003"
        assert ctx.path == "/path"

    def test_drops_the_query_string_from_the_path(self) -> None:
        ctx = build_verify_context_from_request(_make({"agent-identity": "a.b.c"}, "https://m.com/wines?sort=price"))
        assert ctx.path == "/wines"

    def test_returns_empty_headers_when_none_present(self) -> None:
        ctx = build_verify_context_from_request(_make({}))
        assert ctx.agent_identity_headers == []
        assert ctx.signature_input is None
        assert ctx.signature is None

    def test_splits_multiple_comma_folded_agent_identity_headers_into_separate_tokens(self) -> None:
        # Repeated headers (folded by the proxy or sent separately) split back into individual AITs.
        ctx = build_verify_context_from_request(
            _make(
                [(b"agent-identity", b"aaa.bbb.ccc"), (b"agent-identity", b"ddd.eee.fff")],
                "https://m.com/x",
            )
        )
        assert ctx.agent_identity_headers == ["aaa.bbb.ccc", "ddd.eee.fff"]


# ── has_agent_identity_header ──


class TestHasAgentIdentityHeader:
    def test_true_when_an_agent_identity_header_is_present(self) -> None:
        assert has_agent_identity_header(_make({"agent-identity": "a.b.c"})) is True

    def test_false_when_absent(self) -> None:
        assert has_agent_identity_header(_make({})) is False

    def test_false_for_an_empty_header_value(self) -> None:
        assert has_agent_identity_header(_make({"agent-identity": ""})) is False


# ── build_verify_context_from_parts — @path derivation matches the signer ──


class TestBuildVerifyContextFromPartsPathDerivation:
    @staticmethod
    def _parts(url: str, host: str = "wine.example"):
        return build_verify_context_from_parts(
            {
                "method": "POST",
                "url": url,
                "headers": {"host": host, "agent-identity": "a.b.c", "signature-input": "si", "signature": "sig"},
            }
        )

    def test_derives_the_path_from_an_origin_form_target_and_drops_the_query(self) -> None:
        assert self._parts("/checkout?order=42").path == "/checkout"

    def test_preserves_a_leading_double_slash_in_the_path(self) -> None:
        # The signer signs `URL('https://wine.example//promo//x').pathname` == '//promo//x'; a naive
        # reference-resolve would mis-read '//x' as a protocol-relative authority. Pin the contract.
        assert self._parts("//promo//x").path == "//promo//x"
        assert urlsplit("https://wine.example//promo//x").path == "//promo//x"

    def test_normalizes_dot_segments_identically_to_the_signer(self) -> None:
        assert self._parts("/a/../b").path == urlsplit("https://wine.example/a/../b").path

    def test_handles_an_absolute_url_target(self) -> None:
        assert self._parts("https://wine.example/wines/pinot?x=1").path == "/wines/pinot"
