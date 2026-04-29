"""Tests for the lifted ``build_validation_error`` helper."""

from agentscore_commerce.challenge import (
    BuildValidationErrorInput,
    build_validation_error,
)


def test_minimal_body_only_code_and_message() -> None:
    body = build_validation_error(BuildValidationErrorInput(code="bad_request", message="Missing fields"))
    assert body == {"error": {"code": "bad_request", "message": "Missing fields"}}
    assert "required_fields" not in body
    assert "next_steps" not in body
    assert "example_body" not in body


def test_includes_required_fields_and_example_body() -> None:
    body = build_validation_error(
        BuildValidationErrorInput(
            code="bad_request",
            message="product_id and email are required",
            required_fields={"product_id": "uuid", "email": "string"},
            example_body={"product_id": "abc", "email": "a@b.c"},
            has_example_body=True,
        )
    )
    assert body["required_fields"] == {"product_id": "uuid", "email": "string"}
    assert body["example_body"] == {"product_id": "abc", "email": "a@b.c"}


def test_includes_next_steps_with_arbitrary_keys() -> None:
    body = build_validation_error(
        BuildValidationErrorInput(
            code="not_found",
            message="Product not found",
            next_steps={"action": "fetch_catalog", "catalog_url": "https://example.com/catalog"},
        )
    )
    assert body["next_steps"] == {"action": "fetch_catalog", "catalog_url": "https://example.com/catalog"}


def test_merges_extra_top_level_fields() -> None:
    body = build_validation_error(
        BuildValidationErrorInput(
            code="out_of_stock",
            message="Insufficient quantity",
            extra={"available": 3, "max_length": 300},
        )
    )
    assert body["available"] == 3
    assert body["max_length"] == 300


def test_omits_example_body_when_has_example_body_false_default() -> None:
    body = build_validation_error(BuildValidationErrorInput(code="x", message="y"))
    assert "example_body" not in body


def test_emits_explicit_null_example_body_when_has_example_body_true() -> None:
    body = build_validation_error(
        BuildValidationErrorInput(code="x", message="y", example_body=None, has_example_body=True)
    )
    assert "example_body" in body
    assert body["example_body"] is None
