"""Tests for the lifted ``build_validation_error`` helper."""

from agentscore_commerce.challenge import build_validation_error


def test_minimal_body_only_code_and_message() -> None:
    body = build_validation_error(code="bad_request", message="Missing fields")
    assert body == {"error": {"code": "bad_request", "message": "Missing fields"}}
    assert "required_fields" not in body
    assert "next_steps" not in body
    assert "example_body" not in body


def test_includes_required_fields_and_example_body() -> None:
    body = build_validation_error(
        code="bad_request",
        message="product_id and email are required",
        required_fields={"product_id": "uuid", "email": "string"},
        example_body={"product_id": "abc", "email": "a@b.c"},
    )
    assert body["required_fields"] == {"product_id": "uuid", "email": "string"}
    assert body["example_body"] == {"product_id": "abc", "email": "a@b.c"}


def test_includes_next_steps_with_arbitrary_keys() -> None:
    body = build_validation_error(
        code="not_found",
        message="Product not found",
        next_steps={"action": "fetch_catalog", "catalog_url": "https://example.com/catalog"},
    )
    assert body["next_steps"] == {"action": "fetch_catalog", "catalog_url": "https://example.com/catalog"}


def test_merges_extra_top_level_fields() -> None:
    body = build_validation_error(
        code="out_of_stock",
        message="Insufficient quantity",
        extra={"available": 3, "max_length": 300},
    )
    assert body["available"] == 3
    assert body["max_length"] == 300


def test_omits_example_body_when_not_passed() -> None:
    """When example_body kwarg is omitted, the field is suppressed in the body."""
    body = build_validation_error(code="x", message="y")
    assert "example_body" not in body


def test_emits_explicit_null_example_body_when_passed_as_none() -> None:
    """Passing example_body=None explicitly emits a literal null in the body
    (distinguished from "field omitted" via the sentinel default)."""
    body = build_validation_error(code="x", message="y", example_body=None)
    assert "example_body" in body
    assert body["example_body"] is None
