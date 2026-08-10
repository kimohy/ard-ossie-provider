from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate
from pydantic import Field, JsonValue, SecretStr

from ard_ossie.docling_parser import Evidence
from ard_ossie.models import StrictModel


class LLMProvider(Protocol):
    def health_check(self) -> bool: ...

    def capabilities(self) -> dict[str, JsonValue]: ...

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> dict[str, object]: ...


class AISuggestion(StrictModel):
    field_path: str
    value: JsonValue
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(min_length=1)
    status: Literal["ai_suggested"] = "ai_suggested"


class MetricSuggestion(StrictModel):
    name: str
    expression: str
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(min_length=1)
    status: Literal["ai_suggested"] = "ai_suggested"


def semantic_extraction_schema() -> dict[str, object]:
    """Return an OpenAI strict-mode compatible, fully closed JSON Schema."""
    nullable_string = {"type": ["string", "null"]}
    nullable_integer = {"type": ["integer", "null"]}
    nullable_number = {"type": ["number", "null"]}
    bbox = {
        "type": ["object", "null"],
        "properties": {
            "left": nullable_number,
            "top": nullable_number,
            "right": nullable_number,
            "bottom": nullable_number,
        },
        "required": ["left", "top", "right", "bottom"],
        "additionalProperties": False,
    }
    locator = {
        "type": "object",
        "properties": {
            "document": nullable_string,
            "item_index": nullable_integer,
            "level": nullable_integer,
            "page": nullable_integer,
            "bbox": bbox,
            "charspan": {
                "type": ["array", "null"],
                "items": {"type": "integer"},
            },
            "sheet": nullable_string,
            "range": nullable_string,
        },
        "required": [
            "document",
            "item_index",
            "level",
            "page",
            "bbox",
            "charspan",
            "sheet",
            "range",
        ],
        "additionalProperties": False,
    }
    evidence = {
        "type": "object",
        "properties": {
            "source_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "role": {
                "type": "string",
                "enum": ["product_html", "semantic_document", "dictionary_excel"],
            },
            "locator": locator,
            "excerpt": nullable_string,
        },
        "required": ["source_hash", "role", "locator", "excerpt"],
        "additionalProperties": False,
    }
    evidence_list = {"type": "array", "minItems": 1, "items": evidence}
    suggestion = {
        "type": "object",
        "properties": {
            "field_path": {"type": "string"},
            "value": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": evidence_list,
            "status": {"type": "string", "const": "ai_suggested"},
        },
        "required": ["field_path", "value", "confidence", "evidence", "status"],
        "additionalProperties": False,
    }
    metric = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "expression": {"type": "string"},
            "description": nullable_string,
            "synonyms": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": evidence_list,
            "status": {"type": "string", "const": "ai_suggested"},
        },
        "required": [
            "name",
            "expression",
            "description",
            "synonyms",
            "confidence",
            "evidence",
            "status",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "suggestions": {"type": "array", "items": suggestion},
            "metrics": {"type": "array", "items": metric},
        },
        "required": ["suggestions", "metrics"],
        "additionalProperties": False,
    }


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        client: Any | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client or _new_client(
            base_url=self.base_url,
            api_key=api_key.get_secret_value(),
            timeout_seconds=timeout_seconds,
        )

    def __repr__(self) -> str:
        return (
            f"OpenAICompatibleProvider(base_url={self.base_url!r}, "
            f"model={self.model!r}, timeout_seconds={self.timeout_seconds})"
        )

    def health_check(self) -> bool:
        self._client.models.list()
        return True

    def capabilities(self) -> dict[str, JsonValue]:
        return {"api_style": "chat_completions", "structured_output": "json_schema"}

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> dict[str, object]:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "ard_semantic_extraction",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise ValueError("LLM_EMPTY_RESPONSE")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError("LLM_INVALID_JSON") from error
        if not isinstance(result, dict):
            raise ValueError("LLM_RESPONSE_NOT_OBJECT")
        try:
            validate(instance=result, schema=schema)
        except JsonSchemaValidationError as error:
            raise ValueError(f"LLM_SCHEMA_VIOLATION: {error.message}") from error
        return result


_PHYSICAL_SEGMENTS = frozenset(
    {
        "column_id",
        "column_name",
        "data_type",
        "foreign_key",
        "locator",
        "nullable",
        "primary_key",
        "table_id",
        "table_name",
        "type",
    }
)


def validate_semantic_suggestions(
    suggestions: list[AISuggestion],
) -> list[AISuggestion]:
    for suggestion in suggestions:
        segments = {part.lower() for part in suggestion.field_path.split(".")}
        if segments & _PHYSICAL_SEGMENTS:
            raise ValueError(f"LLM_PHYSICAL_FIELD_FORBIDDEN: {suggestion.field_path}")
    return suggestions


def _new_client(*, base_url: str, api_key: str, timeout_seconds: int) -> Any:
    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_seconds)
