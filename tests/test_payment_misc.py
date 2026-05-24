import base64
import json

from agentscore_commerce.payment import (
    SETTLEMENT_OVERRIDES_HEADER,
    USDC,
    lookup_rail,
    network_family,
    networks,
    payment_required_header,
    rails,
    register_x402_schemes_v1_v2,
    settlement_override_header,
    www_authenticate_header,
)


def test_networks_registry_has_expected_chains():
    assert networks.base.mainnet.caip2 == "eip155:8453"
    assert networks.solana.mainnet.caip2.startswith("solana:")
    assert networks.tempo.mainnet.chain_id == 4217


def test_network_family_routes():
    assert network_family("eip155:8453") == "base"
    assert network_family(networks.solana.devnet.caip2) == "solana"
    assert network_family("eip155:4217") == "tempo"
    assert network_family("solana:anything") == "solana"
    assert network_family("cosmos:1") is None


def test_usdc_registry_has_per_chain_addresses():
    assert USDC.base.mainnet.decimals == 6
    assert USDC.solana.mainnet.mint.startswith("EPj")
    assert USDC.tempo.mainnet.address.startswith("0x")


def test_lookup_rail_returns_rail_definition():
    rail = lookup_rail("tempo-mainnet")
    assert rail is not None
    assert rail.method == "tempo"
    assert rail.decimals == 6


def test_lookup_rail_returns_none_for_unknown():
    assert lookup_rail("not-a-real-rail") is None


def test_rails_includes_upto_variants():
    assert "x402-base-mainnet-upto" in rails
    assert rails["x402-base-mainnet-upto"].method == "x402-upto"


def test_www_authenticate_header_joins_directives():
    out = www_authenticate_header(["Payment a", "Payment b"])
    assert out == "Payment a, Payment b"


def test_payment_required_header_base64_encodes_json():
    h = payment_required_header(x402_version=2, accepts=[{"scheme": "exact"}], resource={"url": "https://x"})
    decoded = json.loads(base64.b64decode(h))
    assert decoded["x402Version"] == 2
    assert decoded["accepts"] == [{"scheme": "exact"}]
    assert decoded["resource"]["url"] == "https://x"


def test_payment_required_header_does_not_alias_accepts():
    """The header must NOT add a v1<->v2 amount alias.

    @x402/core matches a v2 payment by whole-object deepEqual of the echoed
    requirement against the server's rebuilt one, so an extra maxAmountRequired
    on the wire that the rebuild lacks silently fails settle. The header keeps
    accepts byte-identical to build_payment_requirements output.
    """
    h = payment_required_header(
        x402_version=2,
        accepts=[{"scheme": "exact", "network": "eip155:84532", "amount": "110000"}],
    )
    decoded = json.loads(base64.b64decode(h))
    assert decoded["accepts"][0]["amount"] == "110000"
    assert "maxAmountRequired" not in decoded["accepts"][0]


def test_alias_amount_fields_still_available_as_opt_in():
    """alias_amount_fields stays exported for callers with hardcoded-v1 clients."""
    from agentscore_commerce.payment import alias_amount_fields

    # Forward: v2 amount -> adds maxAmountRequired.
    aliased = alias_amount_fields([{"scheme": "exact", "amount": "110000"}])
    assert aliased[0]["amount"] == "110000"
    assert aliased[0]["maxAmountRequired"] == "110000"

    # Reverse: v1 shape gets amount alias added.
    reverse = alias_amount_fields([{"scheme": "exact", "maxAmountRequired": "110000"}])
    assert reverse[0]["amount"] == "110000"
    assert reverse[0]["maxAmountRequired"] == "110000"

    # Idempotent: both already set → unchanged.
    both = alias_amount_fields([{"amount": "1", "maxAmountRequired": "1"}])
    assert both[0] == {"amount": "1", "maxAmountRequired": "1"}

    # Non-dict entries pass through untouched.
    passthrough = alias_amount_fields(["not-a-dict", {"amount": "5"}])
    assert passthrough[0] == "not-a-dict"
    assert passthrough[1]["maxAmountRequired"] == "5"


def test_settlement_override_header_returns_name_value_pair():
    name, value = settlement_override_header(amount="1500")
    assert name == SETTLEMENT_OVERRIDES_HEADER
    assert json.loads(value) == {"amount": "1500"}


def test_register_x402_schemes_v1_v2_calls_both_when_v1_present():
    calls = []

    class Server:
        def register(self, network, scheme):
            calls.append(("v2", network))

        def register_v1(self, network, scheme):
            calls.append(("v1", network))

    register_x402_schemes_v1_v2(Server(), "eip155:8453", object())
    assert ("v2", "eip155:8453") in calls
    assert ("v1", "eip155:8453") in calls


def test_register_x402_schemes_v1_v2_skips_v1_when_absent():
    calls = []

    class Server:
        def register(self, network, scheme):
            calls.append(network)

    register_x402_schemes_v1_v2(Server(), "eip155:1", object())
    assert calls == ["eip155:1"]


def test_register_x402_schemes_v1_v2_raises_when_register_not_callable():
    """A server lacking a callable `register` method raises a clear TypeError."""
    import pytest

    class _NoRegister:
        register = "not-a-method"

    with pytest.raises(TypeError, match="no callable `register` method"):
        register_x402_schemes_v1_v2(_NoRegister(), "eip155:1", object())
