from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from ard_ossie.llm import (
    AzureOpenAIProvider,
    LLMMetadata,
    LLMResult,
    OpenAICompatibleProvider,
    ProviderExecutionError,
    ProviderFailureKind,
)


class RecordingCreate:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


class FailingModels:
    def list(self) -> None:
        raise RuntimeError("hostile provider body containing a secret")


def chat_client(content: str, *, request_id: str = "chatcmpl_123") -> SimpleNamespace:
    response = SimpleNamespace(
        id=request_id,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
    )
    return SimpleNamespace(
        chat=SimpleNamespace(completions=RecordingCreate(response)),
    )


def responses_client(content: str) -> SimpleNamespace:
    response = SimpleNamespace(
        id="resp_123",
        output_text=content,
        status="completed",
        usage=SimpleNamespace(input_tokens=5, output_tokens=2),
    )
    return SimpleNamespace(responses=RecordingCreate(response))


def test_llm_result_repr_contains_no_generated_body() -> None:
    result = LLMResult(
        text="sensitive generated body",
        structured={"answer": "sensitive generated body"},
        metadata=LLMMetadata(
            profile="safe-profile",
            provider="openai_compatible",
            model="safe-model",
            request_id="req_123",
            input_tokens=4,
            output_tokens=2,
            finish_reason="stop",
            elapsed_ms=8,
        ),
    )

    assert "sensitive generated body" not in repr(result)


def test_openai_chat_generates_text_and_normalizes_metadata() -> None:
    client = chat_client("hello")
    clock = iter((1.0, 1.008)).__next__
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        profile="safe-profile",
        client=client,
        clock=clock,
    )

    result = provider.generate_text(messages=[{"role": "user", "content": "hi"}])

    assert result.text == "hello"
    assert result.structured is None
    assert result.metadata.profile == "safe-profile"
    assert result.metadata.provider == "openai_compatible"
    assert result.metadata.request_id == "chatcmpl_123"
    assert result.metadata.input_tokens == 7
    assert result.metadata.output_tokens == 3
    assert result.metadata.finish_reason == "stop"
    assert result.metadata.elapsed_ms == 8


def test_openai_responses_uses_json_schema_and_normalizes_structured_result() -> None:
    client = responses_client('{"terms":["revenue"]}')
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        profile="responses-profile",
        api="responses",
        client=client,
    )
    schema = {
        "type": "object",
        "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
        "required": ["terms"],
        "additionalProperties": False,
    }

    result = provider.generate_structured(
        schema=schema,
        messages=[{"role": "user", "content": "extract"}],
    )

    assert result.structured == {"terms": ["revenue"]}
    call = client.responses.calls[0]
    assert call["text"] == {
        "format": {
            "type": "json_schema",
            "name": "ard_semantic_extraction",
            "strict": True,
            "schema": schema,
        }
    }


def test_azure_adapter_is_distinct_and_uses_deployment_as_model() -> None:
    client = chat_client("hello")
    provider = AzureOpenAIProvider(
        endpoint="https://resource.openai.azure.com",
        api_key=SecretStr("azure-secret"),
        deployment="deployment-name",
        profile="azure-profile",
        client=client,
    )

    result = provider.generate_text(messages=[{"role": "user", "content": "hi"}])

    assert result.metadata.provider == "azure_openai"
    assert result.metadata.model == "deployment-name"
    assert client.chat.completions.calls[0]["model"] == "deployment-name"
    assert "azure-secret" not in repr(provider)


def test_openai_provider_repr_does_not_expose_secret_endpoint() -> None:
    endpoint = "https://secret-endpoint.example.test/v1"
    provider = OpenAICompatibleProvider(
        base_url=endpoint,
        api_key=SecretStr("secret-value"),
        model="example-model",
        profile="safe-profile",
        client=chat_client("hello"),
    )

    assert endpoint not in repr(provider)
    assert "example-model" in repr(provider)
    assert "safe-profile" in repr(provider)


def test_openai_health_check_normalizes_hostile_sdk_error() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.test/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=SimpleNamespace(models=FailingModels()),
    )

    with pytest.raises(ProviderExecutionError) as captured:
        provider.health_check()

    assert captured.value.code == "LLM_PROVIDER_FAILURE"
    assert captured.value.kind is ProviderFailureKind.TRANSIENT
    assert "hostile" not in repr(captured.value)


def test_hostile_request_id_is_dropped_from_safe_metadata() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=chat_client("hello", request_id="hostile request id\nsecret"),
    )

    result = provider.generate_text(messages=[{"role": "user", "content": "hi"}])

    assert result.metadata.request_id is None
