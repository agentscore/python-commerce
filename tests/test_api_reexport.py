def test_api_reexports_agentscore_class():
    from agentscore_commerce.api import AgentScore, AgentScoreError

    assert AgentScore is not None
    assert AgentScoreError is not None


def test_api_reexports_webhook_helper():
    import agentscore as sdk

    from agentscore_commerce.api import VerifyWebhookSignatureResult, verify_webhook_signature

    assert verify_webhook_signature is sdk.verify_webhook_signature
    assert VerifyWebhookSignatureResult is not None


def test_api_reexports_test_mode_helpers():
    import agentscore as sdk

    from agentscore_commerce.api import AGENTSCORE_TEST_ADDRESSES, is_agentscore_test_address

    assert AGENTSCORE_TEST_ADDRESSES is sdk.AGENTSCORE_TEST_ADDRESSES
    assert is_agentscore_test_address is sdk.is_agentscore_test_address
