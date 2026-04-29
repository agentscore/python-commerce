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


# ── create_mppx_stripe: the pympp wrapper ───────────────────────────────────


@pytest.mark.asyncio
async def test_create_mppx_stripe_calls_pympp_charge_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agentscore_commerce.stripe_multichain import mppx_stripe

    fake_charge = MagicMock(return_value="fake-method")
    fake_module = SimpleNamespace(charge=fake_charge)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: fake_module if name == "pympp.methods.stripe" else importlib.__import__(name),
    )
    try:
        result = await mppx_stripe.create_mppx_stripe(profile_id="prof_test", secret_key="sk_test")
        assert result == "fake-method"
        fake_charge.assert_called_once()
        kwargs = fake_charge.call_args.kwargs
        assert kwargs["network_id"] == "prof_test"
        assert kwargs["secret_key"] == "sk_test"
        assert kwargs["payment_method_types"] == ["card", "link"]
    finally:
        sys.modules.pop("pympp.methods.stripe", None)


@pytest.mark.asyncio
async def test_create_mppx_stripe_custom_payment_method_types(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agentscore_commerce.stripe_multichain import mppx_stripe

    fake_charge = MagicMock(return_value=object())
    fake_module = SimpleNamespace(charge=fake_charge)
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: fake_module if name == "pympp.methods.stripe" else importlib.__import__(name),
    )
    try:
        await mppx_stripe.create_mppx_stripe(profile_id="prof", secret_key="sk", payment_method_types=["card"])
        assert fake_charge.call_args.kwargs["payment_method_types"] == ["card"]
    finally:
        sys.modules.pop("pympp.methods.stripe", None)


@pytest.mark.asyncio
async def test_create_mppx_stripe_missing_pympp_raises_guiding_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from agentscore_commerce.stripe_multichain import mppx_stripe

    def _missing(name: str) -> object:
        raise ImportError("no module named " + name)

    monkeypatch.setattr(importlib, "import_module", _missing)
    with pytest.raises(ImportError, match=r"pympp\[stripe\]"):
        await mppx_stripe.create_mppx_stripe(profile_id="prof", secret_key="sk")


@pytest.mark.asyncio
async def test_create_mppx_stripe_missing_charge_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import sys
    from types import SimpleNamespace

    from agentscore_commerce.stripe_multichain import mppx_stripe

    fake_module = SimpleNamespace()
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: fake_module if name == "pympp.methods.stripe" else importlib.__import__(name),
    )
    try:
        with pytest.raises(ImportError, match="charge not found"):
            await mppx_stripe.create_mppx_stripe(profile_id="prof", secret_key="sk")
    finally:
        sys.modules.pop("pympp.methods.stripe", None)
