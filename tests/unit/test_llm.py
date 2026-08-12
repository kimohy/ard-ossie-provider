from __future__ import annotations

import traceback
from types import SimpleNamespace

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from pydantic import SecretStr

from ard_ossie.docling_parser import Evidence
from ard_ossie.llm import (
    AISuggestion,
    OpenAICompatibleProvider,
    ProviderExecutionError,
    ProviderFailureKind,
    semantic_extraction_schema,
    validate_semantic_suggestions,
)


class FakeCompletions:
    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.last_request: dict[str, object] | None = None

    def create(self, **kwargs: object) -> object:
        self.last_request = kwargs
        if self.error is not None:
            raise self.error
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self.completions = FakeCompletions(content, error)
        self.chat = SimpleNamespace(completions=self.completions)


class EmptyChoicesCompletions:
    def create(self, **kwargs: object) -> object:
        return SimpleNamespace(choices=[])


def status_error(error_type, status: int, code: str = "provider_error"):
    request = httpx.Request("POST", "https://llm.example.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return error_type(
        "sentinel-exception-message",
        response=response,
        body={"error": {"code": code, "message": "sentinel-response-body"}},
    )


def evidence() -> Evidence:
    return Evidence(
        source_hash="a" * 64,
        role="semantic_document",
        locator={"page": 2},
        excerpt="Net revenue excludes tax",
    )


def test_provider_validates_structured_response_against_local_schema() -> None:
    client = FakeClient('{"terms":["net revenue"]}')
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=client,
    )
    schema = {
        "type": "object",
        "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
        "required": ["terms"],
        "additionalProperties": False,
    }

    result = provider.generate_structured(
        schema=schema, messages=[{"role": "user", "content": "x"}]
    )

    assert result == {"terms": ["net revenue"]}
    assert client.completions.last_request["model"] == "example-model"
    assert "secret-value" not in repr(provider)


def test_provider_omits_sampling_parameters_from_structured_request() -> None:
    client = FakeClient('{"terms":["net revenue"]}')
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=client,
    )

    provider.generate_structured(
        schema={
            "type": "object",
            "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
            "required": ["terms"],
            "additionalProperties": False,
        },
        messages=[{"role": "user", "content": "extract"}],
    )

    assert client.completions.last_request is not None
    assert {"temperature", "top_p"}.isdisjoint(client.completions.last_request)


def test_provider_rejects_schema_invalid_response() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=FakeClient('{"terms":"sentinel-response-payload"}'),
    )

    with pytest.raises(ProviderExecutionError, match="LLM_SCHEMA_VIOLATION") as captured:
        provider.generate_structured(
            schema={
                "type": "object",
                "properties": {"terms": {"type": "array"}},
                "required": ["terms"],
            },
            messages=[{"role": "user", "content": "x"}],
        )

    assert captured.value.kind is ProviderFailureKind.OUTPUT
    assert "sentinel-response-payload" not in "".join(
        traceback.format_exception(captured.value)
    )


def test_provider_normalizes_client_construction_failure(monkeypatch) -> None:
    def fail_client(**kwargs):
        raise ValueError("sentinel-client-configuration")

    monkeypatch.setattr("ard_ossie.llm._new_client", fail_client)

    with pytest.raises(
        ProviderExecutionError,
        match="LLM_PROVIDER_CONFIGURATION_FAILED",
    ) as captured:
        OpenAICompatibleProvider(
            base_url="https://llm.example.com/v1",
            api_key=SecretStr("secret-value"),
            model="example-model",
        )

    assert captured.value.kind is ProviderFailureKind.CONFIGURATION
    rendered = "".join(traceback.format_exception(captured.value))
    assert "sentinel-client-configuration" not in rendered
    assert "secret-value" not in rendered


@pytest.mark.parametrize(
    ("provider_error", "expected_code", "expected_kind"),
    [
        pytest.param(
            status_error(AuthenticationError, 401),
            "LLM_PROVIDER_AUTHENTICATION_FAILED",
            ProviderFailureKind.CONFIGURATION,
            id="authentication",
        ),
        pytest.param(
            status_error(PermissionDeniedError, 403),
            "LLM_PROVIDER_PERMISSION_DENIED",
            ProviderFailureKind.CONFIGURATION,
            id="permission",
        ),
        pytest.param(
            status_error(NotFoundError, 404),
            "LLM_PROVIDER_MODEL_NOT_FOUND",
            ProviderFailureKind.CONFIGURATION,
            id="model-not-found",
        ),
        pytest.param(
            status_error(BadRequestError, 400),
            "LLM_PROVIDER_REQUEST_REJECTED",
            ProviderFailureKind.CONFIGURATION,
            id="bad-request",
        ),
        pytest.param(
            status_error(UnprocessableEntityError, 422),
            "LLM_PROVIDER_REQUEST_REJECTED",
            ProviderFailureKind.CONFIGURATION,
            id="unprocessable-request",
        ),
        pytest.param(
            status_error(RateLimitError, 429, "credit_balance_exhausted"),
            "LLM_PROVIDER_QUOTA_EXHAUSTED",
            ProviderFailureKind.CONFIGURATION,
            id="quota",
        ),
        pytest.param(
            status_error(RateLimitError, 429, "rate_limit_exceeded"),
            "LLM_PROVIDER_RATE_LIMITED",
            ProviderFailureKind.TRANSIENT,
            id="rate-limit",
        ),
        pytest.param(
            APITimeoutError(
                httpx.Request("POST", "https://llm.example.com/v1/chat/completions")
            ),
            "LLM_PROVIDER_TIMEOUT",
            ProviderFailureKind.TRANSIENT,
            id="timeout",
        ),
        pytest.param(
            APIConnectionError(
                message="sentinel-exception-message",
                request=httpx.Request(
                    "POST", "https://llm.example.com/v1/chat/completions"
                ),
            ),
            "LLM_PROVIDER_CONNECTION_FAILED",
            ProviderFailureKind.TRANSIENT,
            id="connection",
        ),
        pytest.param(
            status_error(InternalServerError, 500),
            "LLM_PROVIDER_SERVER_ERROR",
            ProviderFailureKind.TRANSIENT,
            id="server",
        ),
        pytest.param(
            status_error(APIStatusError, 408),
            "LLM_PROVIDER_TIMEOUT",
            ProviderFailureKind.TRANSIENT,
            id="generic-http-408",
        ),
        pytest.param(
            status_error(ConflictError, 409),
            "LLM_PROVIDER_CONFLICT",
            ProviderFailureKind.TRANSIENT,
            id="conflict",
        ),
        pytest.param(
            APIError(
                "sentinel-exception-message",
                httpx.Request(
                    "POST", "https://llm.example.com/v1/chat/completions"
                ),
                body={"message": "sentinel-response-body"},
            ),
            "LLM_PROVIDER_FAILURE",
            ProviderFailureKind.TRANSIENT,
            id="generic-api-error",
        ),
    ],
)
def test_provider_classifies_sdk_failures_without_leaking_provider_details(
    provider_error: Exception,
    expected_code: str,
    expected_kind: ProviderFailureKind,
) -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=FakeClient(error=provider_error),
    )

    with pytest.raises(ProviderExecutionError) as captured:
        provider.generate_structured(
            schema={"type": "object"},
            messages=[{"role": "user", "content": "sentinel-prompt"}],
        )

    assert captured.value.code == expected_code
    assert captured.value.kind is expected_kind
    rendered = f"{captured.value!s} {captured.value!r}"
    assert rendered == f"{expected_code} ProviderExecutionError('{expected_code}')"
    assert "sentinel" not in rendered
    assert "secret-value" not in rendered
    traceback_rendered = "".join(traceback.format_exception(captured.value))
    assert "sentinel" not in traceback_rendered
    assert "secret-value" not in traceback_rendered


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        pytest.param(None, "LLM_EMPTY_RESPONSE", id="empty"),
        pytest.param("not-json", "LLM_INVALID_JSON", id="invalid-json"),
        pytest.param("[]", "LLM_RESPONSE_NOT_OBJECT", id="non-object"),
    ],
)
def test_provider_classifies_invalid_structured_output(
    content: str | None,
    expected_code: str,
) -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=FakeClient(content),
    )

    with pytest.raises(ProviderExecutionError) as captured:
        provider.generate_structured(
            schema={"type": "object"},
            messages=[{"role": "user", "content": "sentinel-prompt"}],
        )

    assert captured.value.code == expected_code
    assert captured.value.kind is ProviderFailureKind.OUTPUT


def test_provider_classifies_missing_choice_as_invalid_output() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=EmptyChoicesCompletions())
    )
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=client,
    )

    with pytest.raises(ProviderExecutionError) as captured:
        provider.generate_structured(
            schema={"type": "object"},
            messages=[{"role": "user", "content": "sentinel-prompt"}],
        )

    assert captured.value.code == "LLM_EMPTY_RESPONSE"
    assert captured.value.kind is ProviderFailureKind.OUTPUT


def test_semantic_suggestion_requires_evidence_and_rejects_physical_fields() -> None:
    semantic = AISuggestion(
        field_path="business_terms.net_revenue.description",
        value="Revenue excluding tax",
        confidence=0.91,
        evidence=[evidence()],
    )
    physical = AISuggestion(
        field_path="tables.orders.columns.order_id.data_type",
        value="INT64",
        confidence=0.99,
        evidence=[evidence()],
    )

    assert validate_semantic_suggestions([semantic]) == [semantic]
    with pytest.raises(ValueError, match="LLM_PHYSICAL_FIELD_FORBIDDEN"):
        validate_semantic_suggestions([physical])


def test_semantic_schema_is_closed_and_openai_strict_compatible() -> None:
    schema = semantic_extraction_schema()

    def assert_closed(node: object) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            if node_type == "object" or (
                isinstance(node_type, list) and "object" in node_type
            ):
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for value in node.values():
                assert_closed(value)
        elif isinstance(node, list):
            for value in node:
                assert_closed(value)

    assert_closed(schema)
    assert "$defs" not in schema
