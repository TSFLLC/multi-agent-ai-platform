"""Provider Adapter abstraction — Section 13.1/13.5, MA2.

Generic adapter contract, OpenRouter normalization (against canned fixture
payloads — never live network in the deterministic suite), provider
failure, authentication failure, and malformed/partial metadata handling.
"""

from decimal import Decimal

import httpx
import pytest

from app.db.enums import PricingClassification
from app.domain.pricing import classify_pricing
from app.providers.base import (
    InvokeRequest,
    ModelDescriptor,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderInvalidResponseError,
    ProviderTimeoutError,
)
from app.providers.openrouter import OpenRouterAdapter, normalize_model

# --- fixture payloads, shaped like OpenRouter's real /models entries -------

FREE_MODEL_PAYLOAD = {
    "id": "some-org/some-model:free",
    "name": "Some Free Model",
    "context_length": 32768,
    "architecture": {"modality": "text->text"},
    "pricing": {"prompt": "0", "completion": "0"},
    "supported_parameters": ["max_tokens", "temperature"],
}

PAID_MODEL_PAYLOAD = {
    "id": "anthropic/claude-3.5-sonnet",
    "name": "Claude 3.5 Sonnet",
    "context_length": 200000,
    "architecture": {"modality": "text+image->text"},
    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    "supported_parameters": ["tools", "tool_choice", "response_format", "temperature"],
}

MISSING_PRICING_PAYLOAD = {
    "id": "some-org/mystery-model",
    "name": "Mystery Model",
    "context_length": 8192,
    "architecture": {"modality": "text->text"},
    "pricing": {},
    "supported_parameters": [],
}

NO_ARCHITECTURE_PAYLOAD = {
    "id": "some-org/bare-model",
    "name": "Bare Model",
    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
}


# --- normalization (pure function, no I/O) ---------------------------------


def test_normalize_free_model():
    descriptor = normalize_model(FREE_MODEL_PAYLOAD)
    assert descriptor.provider_model_id == "some-org/some-model:free"
    assert descriptor.context_window == 32768
    assert descriptor.input_modalities == ["text"]
    assert descriptor.output_modalities == ["text"]
    assert descriptor.cost_input_per_mtok == Decimal(0)
    assert descriptor.cost_output_per_mtok == Decimal(0)
    assert classify_pricing(descriptor.cost_input_per_mtok, descriptor.cost_output_per_mtok) == (
        PricingClassification.FREE
    )


def test_normalize_paid_model_converts_per_token_to_per_mtok():
    descriptor = normalize_model(PAID_MODEL_PAYLOAD)
    # OpenRouter prices per single token in USD; the adapter converts to
    # per-million-token at the boundary.
    assert descriptor.cost_input_per_mtok == Decimal("0.000003") * Decimal(1_000_000)
    assert descriptor.cost_output_per_mtok == Decimal("0.000015") * Decimal(1_000_000)
    assert descriptor.tool_calling_support == "basic"
    assert descriptor.structured_output_support is True
    assert descriptor.input_modalities == ["text", "image"]


def test_normalize_missing_pricing_is_unknown_not_zero():
    descriptor = normalize_model(MISSING_PRICING_PAYLOAD)
    assert descriptor.cost_input_per_mtok is None
    assert descriptor.cost_output_per_mtok is None
    assert (
        classify_pricing(descriptor.cost_input_per_mtok, descriptor.cost_output_per_mtok)
        == PricingClassification.UNKNOWN
    )
    # supported_parameters present but empty -> genuinely no signal either way
    assert descriptor.tool_calling_support is None


def test_normalize_negative_sentinel_pricing_is_unknown_not_a_negative_cost():
    """Regression: discovered against the live OpenRouter catalog.
    "openrouter/auto-beta" (and other meta/auto-routing models) prices
    with a negative sentinel (observed: "-1") meaning "variable, not a
    fixed price" — not a literal negative cost. Passing it through as-is
    would violate provider_models' non-negative pricing CHECK constraint
    and misrepresent the model."""
    payload = {
        "id": "openrouter/auto-beta",
        "name": "Auto (beta)",
        "pricing": {"prompt": "-1", "completion": "-1"},
    }
    descriptor = normalize_model(payload)
    assert descriptor.cost_input_per_mtok is None
    assert descriptor.cost_output_per_mtok is None


def test_normalize_no_architecture_field_leaves_modalities_none():
    descriptor = normalize_model(NO_ARCHITECTURE_PAYLOAD)
    assert descriptor.input_modalities is None
    assert descriptor.output_modalities is None
    assert descriptor.context_window is None


def test_normalize_never_guesses_tool_calling_when_field_absent():
    payload = {"id": "x/y", "name": "X", "pricing": {}}
    descriptor = normalize_model(payload)
    assert descriptor.tool_calling_support is None
    assert descriptor.structured_output_support is None


def test_normalize_reports_explicit_no_tool_support():
    payload = {
        "id": "x/y",
        "name": "X",
        "pricing": {},
        "supported_parameters": ["max_tokens", "temperature"],
    }
    descriptor = normalize_model(payload)
    assert descriptor.tool_calling_support == "none"


# --- adapter behavior over HTTP (mocked transport, no live network) --------


def _adapter_with_transport(handler) -> OpenRouterAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://openrouter.ai/api/v1")
    return OpenRouterAdapter(api_key="sk-test", http_client=client)


def test_list_models_success():
    def handler(request):
        return httpx.Response(200, json={"data": [FREE_MODEL_PAYLOAD, PAID_MODEL_PAYLOAD]})

    adapter = _adapter_with_transport(handler)
    descriptors = adapter.list_models()
    assert len(descriptors) == 2
    assert all(isinstance(d, ModelDescriptor) for d in descriptors)


def test_list_models_sends_bearer_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": []})

    adapter = _adapter_with_transport(handler)
    adapter.list_models()
    assert seen["auth"] == "Bearer sk-test"


def test_authentication_failure_raises_specific_error():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid key"})

    adapter = _adapter_with_transport(handler)
    with pytest.raises(ProviderAuthenticationError):
        adapter.list_models()


def test_server_error_raises_connection_error_not_auth_error():
    def handler(request):
        return httpx.Response(500, json={"error": "server exploded"})

    adapter = _adapter_with_transport(handler)
    with pytest.raises(ProviderConnectionError):
        adapter.list_models()


def test_network_failure_raises_connection_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    adapter = _adapter_with_transport(handler)
    with pytest.raises(ProviderConnectionError):
        adapter.list_models()


def test_malformed_entry_is_skipped_not_fatal():
    """One bad entry (missing required "id") must not take down the whole
    catalog fetch."""

    def handler(request):
        return httpx.Response(
            200,
            json={"data": [{"name": "no id field"}, FREE_MODEL_PAYLOAD]},
        )

    adapter = _adapter_with_transport(handler)
    descriptors = adapter.list_models()
    assert len(descriptors) == 1
    assert descriptors[0].provider_model_id == FREE_MODEL_PAYLOAD["id"]


def test_health_check_up_on_success():
    def handler(request):
        return httpx.Response(200, json={"data": []})

    adapter = _adapter_with_transport(handler)
    health = adapter.health_check()
    assert health.status == "up"


def test_health_check_down_distinguishes_auth_failure():
    def handler(request):
        return httpx.Response(401, json={})

    adapter = _adapter_with_transport(handler)
    health = adapter.health_check()
    assert health.status == "down"
    assert "authentication_failed" in health.detail


def test_health_check_down_distinguishes_network_failure():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    adapter = _adapter_with_transport(handler)
    health = adapter.health_check()
    assert health.status == "down"
    assert "connection_failed" in health.detail


def test_get_model_finds_by_id():
    def handler(request):
        return httpx.Response(200, json={"data": [FREE_MODEL_PAYLOAD, PAID_MODEL_PAYLOAD]})

    adapter = _adapter_with_transport(handler)
    found = adapter.get_model("anthropic/claude-3.5-sonnet")
    assert found is not None
    assert found.name == "Claude 3.5 Sonnet"


def test_get_model_returns_none_when_absent():
    def handler(request):
        return httpx.Response(200, json={"data": [FREE_MODEL_PAYLOAD]})

    adapter = _adapter_with_transport(handler)
    assert adapter.get_model("nonexistent/model") is None


# --- invoke() — real model execution (MA3) ---------------------------------


def test_invoke_success_returns_normalized_response():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "id": "gen-abc123",
                "choices": [{"message": {"role": "assistant", "content": "MA3_EXECUTION_OK"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    adapter = _adapter_with_transport(handler)
    response = adapter.invoke(
        InvokeRequest(provider_model_id="some-org/some-model:free", user_prompt="say the magic words")
    )
    assert response.text == "MA3_EXECUTION_OK"
    assert response.tokens_in == 12
    assert response.tokens_out == 4
    assert response.provider_request_id == "gen-abc123"
    assert response.cost_amount is None  # never fabricated; the caller prices it


def test_invoke_sends_system_and_user_messages():
    seen = {}

    def handler(request):
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    adapter = _adapter_with_transport(handler)
    adapter.invoke(
        InvokeRequest(
            provider_model_id="some-org/some-model:free",
            system_prompt="You are terse.",
            user_prompt="Hello",
        )
    )
    messages = seen["body"]["messages"]
    assert messages[0] == {"role": "system", "content": "You are terse."}
    assert messages[1] == {"role": "user", "content": "Hello"}


def test_invoke_no_choices_raises_invalid_response():
    def handler(request):
        return httpx.Response(200, json={"choices": []})

    adapter = _adapter_with_transport(handler)
    with pytest.raises(ProviderInvalidResponseError):
        adapter.invoke(InvokeRequest(provider_model_id="x/y", user_prompt="hi"))


def test_invoke_authentication_failure_raises_specific_error():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid key"})

    adapter = _adapter_with_transport(handler)
    with pytest.raises(ProviderAuthenticationError):
        adapter.invoke(InvokeRequest(provider_model_id="x/y", user_prompt="hi"))


def test_invoke_timeout_raises_provider_timeout_error():
    def handler(request):
        raise httpx.TimeoutException("timed out")

    adapter = _adapter_with_transport(handler)
    with pytest.raises(ProviderTimeoutError):
        adapter.invoke(InvokeRequest(provider_model_id="x/y", user_prompt="hi", timeout_seconds=0.01))


def test_invoke_network_failure_raises_connection_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    adapter = _adapter_with_transport(handler)
    with pytest.raises(ProviderConnectionError):
        adapter.invoke(InvokeRequest(provider_model_id="x/y", user_prompt="hi"))
