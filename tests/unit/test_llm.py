from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from ard_ossie.docling_parser import Evidence
from ard_ossie.llm import (
    AISuggestion,
    OpenAICompatibleProvider,
    semantic_extraction_schema,
    validate_semantic_suggestions,
)


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_request: dict[str, object] | None = None

    def create(self, **kwargs: object) -> object:
        self.last_request = kwargs
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content: str) -> None:
        self.completions = FakeCompletions(content)
        self.chat = SimpleNamespace(completions=self.completions)


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


def test_provider_rejects_schema_invalid_response() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://llm.example.com/v1",
        api_key=SecretStr("secret-value"),
        model="example-model",
        client=FakeClient('{"terms":42}'),
    )

    with pytest.raises(ValueError, match="LLM_SCHEMA_VIOLATION"):
        provider.generate_structured(
            schema={
                "type": "object",
                "properties": {"terms": {"type": "array"}},
                "required": ["terms"],
            },
            messages=[{"role": "user", "content": "x"}],
        )


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
