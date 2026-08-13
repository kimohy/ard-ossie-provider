from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai.errors import ClientError
from pydantic import SecretStr

from ard_ossie.llm import (
    ProviderExecutionError,
    ProviderFailureKind,
    VertexClaudeProvider,
    VertexGeminiProvider,
)


class RecordingGenerateContent:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class RecordingMessages:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class StatusFailure(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("hostile provider body containing a secret")
        self.status_code = status_code


def gemini_client(text: str = "hello") -> SimpleNamespace:
    response = SimpleNamespace(
        text=text,
        response_id="gemini_123",
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=SimpleNamespace(
            prompt_token_count=6,
            candidates_token_count=2,
        ),
    )
    return SimpleNamespace(
        models=RecordingGenerateContent(response=response),
    )


def claude_client(text: str = "hello") -> SimpleNamespace:
    response = SimpleNamespace(
        id="msg_123",
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=8, output_tokens=3),
    )
    return SimpleNamespace(messages=RecordingMessages(response=response))


def test_vertex_gemini_uses_official_client_contract_and_normalizes_result() -> None:
    client = gemini_client()
    provider = VertexGeminiProvider(
        project="gcp-project",
        location="us-central1",
        model="gemini-model",
        credentials_json=SecretStr("unused-by-injected-client"),
        profile="gemini-profile",
        client=client,
    )

    result = provider.generate_text(
        messages=[
            {"role": "system", "content": "system guidance"},
            {"role": "user", "content": "hello"},
        ]
    )

    assert result.text == "hello"
    assert result.metadata.provider == "vertex_gemini"
    assert result.metadata.request_id == "gemini_123"
    assert result.metadata.input_tokens == 6
    assert result.metadata.output_tokens == 2
    assert result.metadata.finish_reason == "stop"
    call = client.models.calls[0]
    assert call["model"] == "gemini-model"
    assert call["config"].system_instruction == "system guidance"


def test_vertex_gemini_passes_native_response_schema() -> None:
    client = gemini_client('{"terms":["revenue"]}')
    provider = VertexGeminiProvider(
        project="gcp-project",
        location="us-central1",
        model="gemini-model",
        credentials_json=SecretStr("unused"),
        profile="gemini-profile",
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
    config = client.models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema


def test_vertex_claude_uses_vertex_messages_and_prompt_json() -> None:
    client = claude_client('{"terms":["revenue"]}')
    provider = VertexClaudeProvider(
        project="gcp-project",
        location="global",
        model="claude-model",
        credentials_json=SecretStr("unused"),
        profile="claude-profile",
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
    assert result.metadata.provider == "vertex_claude"
    assert result.metadata.finish_reason == "end_turn"
    call = client.messages.calls[0]
    assert call["model"] == "claude-model"
    assert "Return only one JSON object" in call["system"]
    assert "additionalProperties" in call["system"]


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (401, "LLM_PROVIDER_AUTHENTICATION_FAILED", ProviderFailureKind.CONFIGURATION),
        (403, "LLM_PROVIDER_PERMISSION_DENIED", ProviderFailureKind.CONFIGURATION),
        (404, "LLM_PROVIDER_MODEL_NOT_FOUND", ProviderFailureKind.CONFIGURATION),
        (408, "LLM_PROVIDER_TIMEOUT", ProviderFailureKind.TRANSIENT),
        (409, "LLM_PROVIDER_CONFLICT", ProviderFailureKind.TRANSIENT),
        (429, "LLM_PROVIDER_RATE_LIMITED", ProviderFailureKind.TRANSIENT),
        (503, "LLM_PROVIDER_SERVER_ERROR", ProviderFailureKind.TRANSIENT),
    ],
)
def test_vertex_status_mapping_is_safe(
    status: int,
    code: str,
    kind: ProviderFailureKind,
) -> None:
    client = SimpleNamespace(
        models=RecordingGenerateContent(error=StatusFailure(status)),
    )
    provider = VertexGeminiProvider(
        project="gcp-project",
        location="us-central1",
        model="gemini-model",
        credentials_json=SecretStr("credential-secret"),
        profile="gemini-profile",
        client=client,
    )

    with pytest.raises(ProviderExecutionError) as captured:
        provider.generate_text(messages=[{"role": "user", "content": "prompt"}])

    assert captured.value.code == code
    assert captured.value.kind is kind
    assert "hostile" not in repr(captured.value)
    assert "credential-secret" not in repr(captured.value)


def test_vertex_gemini_maps_google_genai_code_as_configuration_failure() -> None:
    client = SimpleNamespace(
        models=RecordingGenerateContent(
            error=ClientError(
                401,
                {
                    "error": {
                        "code": 401,
                        "message": "hostile provider body containing a secret",
                        "status": "UNAUTHENTICATED",
                    }
                },
            )
        ),
    )
    provider = VertexGeminiProvider(
        project="gcp-project",
        location="us-central1",
        model="gemini-model",
        credentials_json=SecretStr("credential-secret"),
        profile="gemini-profile",
        client=client,
    )

    with pytest.raises(ProviderExecutionError) as captured:
        provider.generate_text(messages=[{"role": "user", "content": "prompt"}])

    assert captured.value.code == "LLM_PROVIDER_AUTHENTICATION_FAILED"
    assert captured.value.kind is ProviderFailureKind.CONFIGURATION
    assert "hostile" not in repr(captured.value)
    assert "credential-secret" not in repr(captured.value)


def test_invalid_service_account_is_safe_configuration_failure() -> None:
    with pytest.raises(
        ProviderExecutionError,
        match="LLM_PROVIDER_CONFIGURATION_FAILED",
    ) as captured:
        VertexGeminiProvider(
            project="gcp-project",
            location="us-central1",
            model="gemini-model",
            credentials_json=SecretStr("not-json"),
            profile="gemini-profile",
        )

    assert captured.value.kind is ProviderFailureKind.CONFIGURATION
    assert "not-json" not in repr(captured.value)
