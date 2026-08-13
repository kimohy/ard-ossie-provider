from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal, Protocol

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate
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
from pydantic import Field, JsonValue, SecretStr, field_validator

from ard_ossie.docling_parser import Evidence
from ard_ossie.ir import ProductFactKind
from ard_ossie.models import StrictModel

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_QUOTA_CODES = frozenset(
    {
        "credit_balance_exhausted",
        "insufficient_quota",
        "organization_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "project_spend_limit_exceeded",
    }
)


class ProviderFailureKind(StrEnum):
    CONFIGURATION = "configuration"
    TRANSIENT = "transient"
    OUTPUT = "output"


class ProviderExecutionError(RuntimeError):
    def __init__(self, code: str, *, kind: ProviderFailureKind) -> None:
        if _ERROR_CODE.fullmatch(code) is None:
            raise ValueError("INVALID_PROVIDER_ERROR_CODE")
        super().__init__(code)
        self.code = code
        self.kind = kind


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
    dataset_names: list[str] = Field(min_length=1)
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(min_length=1)
    status: Literal["ai_suggested"] = "ai_suggested"


class ProductFactSuggestion(StrictModel):
    kind: ProductFactKind
    value: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: object) -> str:
        normalized = " ".join(str(value).split())
        if not normalized:
            raise ValueError("product fact value must be non-empty")
        return normalized


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
            "dataset_names": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "description": nullable_string,
            "synonyms": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": evidence_list,
            "status": {"type": "string", "const": "ai_suggested"},
        },
        "required": [
            "name",
            "expression",
            "dataset_names",
            "description",
            "synonyms",
            "confidence",
            "evidence",
            "status",
        ],
        "additionalProperties": False,
    }
    product_fact = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [
                    "description",
                    "purpose",
                    "domain",
                    "data_type",
                    "storage_location",
                    "source_system",
                    "source_name",
                    "tag",
                    "access",
                    "security_classification",
                    "owner",
                    "contact",
                    "consumer",
                    "refresh_schedule",
                    "freshness",
                    "sla",
                    "ai_readiness",
                    "quality",
                    "constraint",
                    "related_link",
                ],
            },
            "value": {"type": "string", "minLength": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_ids": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "pattern": "^product-evidence-[0-9]{6}$",
                },
            },
        },
        "required": ["kind", "value", "confidence", "evidence_ids"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "suggestions": {"type": "array", "items": suggestion},
            "metrics": {"type": "array", "items": metric},
            "product_facts": {"type": "array", "items": product_fact},
        },
        "required": ["suggestions", "metrics", "product_facts"],
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
        if client is not None:
            self._client = client
        else:
            try:
                self._client = _new_client(
                    base_url=self.base_url,
                    api_key=api_key.get_secret_value(),
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                raise ProviderExecutionError(
                    "LLM_PROVIDER_CONFIGURATION_FAILED",
                    kind=ProviderFailureKind.CONFIGURATION,
                ) from None

    def __repr__(self) -> str:
        return (
            f"OpenAICompatibleProvider(base_url={self.base_url!r}, "
            f"model={self.model!r}, timeout_seconds={self.timeout_seconds})"
        )

    def health_check(self) -> bool:
        self._client.models.list()
        return True

    def capabilities(self) -> dict[str, JsonValue]:
        return {
            "api_style": "chat_completions",
            "structured_output": "json_schema",
            "provider": "openai_compatible",
            "model": self.model,
        }

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> dict[str, object]:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ard_semantic_extraction",
                        "strict": True,
                        "schema": schema,
                    },
                },
            )
        except Exception as error:
            raise _classify_provider_error(error) from None
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            raise ProviderExecutionError(
                "LLM_EMPTY_RESPONSE",
                kind=ProviderFailureKind.OUTPUT,
            ) from None
        if not isinstance(content, str):
            raise ProviderExecutionError(
                "LLM_EMPTY_RESPONSE",
                kind=ProviderFailureKind.OUTPUT,
            )
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            raise ProviderExecutionError(
                "LLM_INVALID_JSON",
                kind=ProviderFailureKind.OUTPUT,
            ) from None
        if not isinstance(result, dict):
            raise ProviderExecutionError(
                "LLM_RESPONSE_NOT_OBJECT",
                kind=ProviderFailureKind.OUTPUT,
            )
        try:
            validate(instance=result, schema=schema)
        except JsonSchemaValidationError:
            raise ProviderExecutionError(
                "LLM_SCHEMA_VIOLATION",
                kind=ProviderFailureKind.OUTPUT,
            ) from None
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


def _classify_provider_error(error: Exception) -> ProviderExecutionError:
    if isinstance(error, ProviderExecutionError):
        return error
    if isinstance(error, AuthenticationError):
        return ProviderExecutionError(
            "LLM_PROVIDER_AUTHENTICATION_FAILED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, PermissionDeniedError):
        return ProviderExecutionError(
            "LLM_PROVIDER_PERMISSION_DENIED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, NotFoundError):
        return ProviderExecutionError(
            "LLM_PROVIDER_MODEL_NOT_FOUND",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, (BadRequestError, UnprocessableEntityError)):
        return ProviderExecutionError(
            "LLM_PROVIDER_REQUEST_REJECTED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, RateLimitError):
        if _api_error_code(error) in _QUOTA_CODES:
            return ProviderExecutionError(
                "LLM_PROVIDER_QUOTA_EXHAUSTED",
                kind=ProviderFailureKind.CONFIGURATION,
            )
        return ProviderExecutionError(
            "LLM_PROVIDER_RATE_LIMITED",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, APITimeoutError):
        return ProviderExecutionError(
            "LLM_PROVIDER_TIMEOUT",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, APIConnectionError):
        return ProviderExecutionError(
            "LLM_PROVIDER_CONNECTION_FAILED",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, InternalServerError):
        return ProviderExecutionError(
            "LLM_PROVIDER_SERVER_ERROR",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, ConflictError):
        return ProviderExecutionError(
            "LLM_PROVIDER_CONFLICT",
            kind=ProviderFailureKind.TRANSIENT,
        )
    if isinstance(error, APIStatusError):
        status = error.status_code
        if status == 408:
            return ProviderExecutionError(
                "LLM_PROVIDER_TIMEOUT",
                kind=ProviderFailureKind.TRANSIENT,
            )
        if status >= 500:
            return ProviderExecutionError(
                "LLM_PROVIDER_SERVER_ERROR",
                kind=ProviderFailureKind.TRANSIENT,
            )
        return ProviderExecutionError(
            "LLM_PROVIDER_REQUEST_REJECTED",
            kind=ProviderFailureKind.CONFIGURATION,
        )
    if isinstance(error, APIError):
        return ProviderExecutionError(
            "LLM_PROVIDER_FAILURE",
            kind=ProviderFailureKind.TRANSIENT,
        )
    return ProviderExecutionError(
        "LLM_PROVIDER_FAILURE",
        kind=ProviderFailureKind.TRANSIENT,
    )


def _api_error_code(error: APIStatusError) -> str | None:
    body = error.body
    if not isinstance(body, Mapping):
        return None
    nested = body.get("error")
    source = nested if isinstance(nested, Mapping) else body
    code = source.get("code")
    return code if isinstance(code, str) else None
