"""Tests for build_pricing_block + PricingBlock.to_dict()."""

from agentscore_commerce.challenge import build_pricing_block
from agentscore_commerce.challenge.pricing import PricingBlock


def test_formats_cents_to_dollar_strings():
    block = build_pricing_block(subtotal_cents=25000)
    assert block.subtotal == "250.00"
    assert block.tax == "0.00"
    assert block.total == "250.00"


def test_computes_total_from_subtotal_plus_tax_plus_shipping():
    block = build_pricing_block(subtotal_cents=25000, tax_cents=1875, shipping_cents=999)
    assert block.total == "278.74"


def test_respects_explicit_total_override():
    block = build_pricing_block(subtotal_cents=25000, tax_cents=1875, total_cents=50000)
    assert block.total == "500.00"


def test_omits_shipping_when_not_provided():
    block = build_pricing_block(subtotal_cents=1000)
    assert block.shipping is None


def test_includes_shipping_zero_when_explicitly_set():
    block = build_pricing_block(subtotal_cents=1000, shipping_cents=0)
    assert block.shipping == "0.00"


def test_passes_through_tax_rate_state_currency():
    block = build_pricing_block(
        subtotal_cents=1000,
        tax_rate=0.0775,
        tax_state="CA",
        currency="USD",
    )
    assert block.tax_rate == 0.0775
    assert block.tax_state == "CA"
    assert block.currency == "USD"


def test_handles_fractional_cents_to_dollar_correctly():
    block = build_pricing_block(subtotal_cents=1, tax_cents=1, shipping_cents=1)
    assert block.subtotal == "0.01"
    assert block.tax == "0.01"
    assert block.shipping == "0.01"
    assert block.total == "0.03"


def test_to_dict_omits_none_optional_fields():
    block = PricingBlock(subtotal="10.00", tax="0.00", total="10.00")
    d = block.to_dict()
    assert d == {"subtotal": "10.00", "tax": "0.00", "total": "10.00"}


def test_to_dict_includes_all_fields_when_present():
    block = PricingBlock(
        subtotal="10.00",
        tax="0.80",
        total="15.79",
        shipping="4.99",
        tax_rate=0.08,
        tax_state="CA",
        currency="USD",
    )
    assert block.to_dict() == {
        "subtotal": "10.00",
        "tax": "0.80",
        "shipping": "4.99",
        "total": "15.79",
        "tax_rate": 0.08,
        "tax_state": "CA",
        "currency": "USD",
    }
