import httpx
import pytest
import respx

from agentscore_commerce.stripe_multichain import (
    CreateMultichainPaymentIntentInput,
    SimulateCryptoDepositInput,
    create_multichain_payment_intent,
    get_deposit_address,
    simulate_crypto_deposit,
)


class _FakeAPI:
    def __init__(self, response):
        self._response = response
        self.last_params = None
        self.last_idem = None

    def create(self, params, idempotency_key=None):
        self.last_params = params
        self.last_idem = idempotency_key
        return self._response


class _FakeClient:
    def __init__(self, api):
        self.payment_intents = api


def test_create_multichain_payment_intent_extracts_addresses():
    response = {
        "id": "pi_123",
        "next_action": {
            "crypto_display_details": {
                "deposit_addresses": {
                    "tempo": {"address": "0xtempo"},
                    "base": {"address": "0xbase"},
                    "solana": {"address": "solanaaddr"},
                }
            }
        },
    }
    api = _FakeAPI(response)
    result = create_multichain_payment_intent(
        CreateMultichainPaymentIntentInput(stripe=_FakeClient(api), amount=10000, idempotency_key="k1")
    )
    assert result.payment_intent_id == "pi_123"
    assert result.deposit_addresses == {"tempo": "0xtempo", "base": "0xbase", "solana": "solanaaddr"}
    assert api.last_idem == "k1"
    assert api.last_params["amount"] == 10000


def test_create_multichain_payment_intent_raises_when_no_addresses():
    response = {"id": "pi_x", "next_action": None}
    with pytest.raises(RuntimeError, match="No deposit addresses"):
        create_multichain_payment_intent(
            CreateMultichainPaymentIntentInput(stripe=_FakeClient(_FakeAPI(response)), amount=100)
        )


def test_get_deposit_address_returns_per_network():
    from agentscore_commerce.stripe_multichain.payment_intent import MultichainPaymentIntentResult

    r = MultichainPaymentIntentResult(payment_intent_id="pi", deposit_addresses={"tempo": "0xT"})
    assert get_deposit_address(r, "tempo") == "0xT"
    assert get_deposit_address(r, "base") is None


@respx.mock
async def test_simulate_crypto_deposit_calls_test_helpers_endpoint():
    route = respx.post("https://api.stripe.com/v1/test_helpers/payment_intents/pi_1/simulate_crypto_deposit").mock(
        return_value=httpx.Response(200, text="{}")
    )
    await simulate_crypto_deposit(
        SimulateCryptoDepositInput(
            payment_intent_id="pi_1", network="base", stripe_secret_key="sk_test_x", token_currency="usdc"
        )
    )
    assert route.called


@respx.mock
async def test_simulate_crypto_deposit_raises_on_non_2xx():
    respx.post("https://api.stripe.com/v1/test_helpers/payment_intents/pi_2/simulate_crypto_deposit").mock(
        return_value=httpx.Response(400, text='{"error":"bad"}')
    )
    with pytest.raises(RuntimeError, match="failed: 400"):
        await simulate_crypto_deposit(
            SimulateCryptoDepositInput(payment_intent_id="pi_2", network="base", stripe_secret_key="sk_test_x")
        )
