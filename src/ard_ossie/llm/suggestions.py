from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, field_validator

from ard_ossie.docling_parser import Evidence
from ard_ossie.ir import ProductFactKind
from ard_ossie.models import StrictModel


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
