from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import validate

from ard_ossie.ir import ColumnIR, MetricIR, ProductIR, TableIR

_PACKAGE_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "assets"
    / "schemas"
    / "ossie"
    / "0.1.1"
    / "osi-schema.json"
)
_REPOSITORY_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "ossie" / "0.1.1" / "osi-schema.json"
)
_SCHEMA_PATH = (
    _PACKAGE_SCHEMA_PATH if _PACKAGE_SCHEMA_PATH.is_file() else _REPOSITORY_SCHEMA_PATH
)


def load_ossie_011_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def compile_ossie(product: ProductIR) -> dict[str, Any]:
    tables = sorted(product.tables, key=lambda item: item.table_id)
    table_names = {table.table_id: table.dataset_name for table in tables}
    relationships = []
    for relationship in sorted(
        product.relationships,
        key=lambda item: item.relationship_id,
    ):
        if (
            relationship.from_table_id not in table_names
            or relationship.to_table_id not in table_names
        ):
            raise ValueError(f"OSSIE_UNRESOLVED_REFERENCE: {relationship.relationship_id}")
        relationship_payload: dict[str, Any] = {
            "name": relationship.name,
            "from": table_names[relationship.from_table_id],
            "to": table_names[relationship.to_table_id],
            "from_columns": relationship.from_columns,
            "to_columns": relationship.to_columns,
            "custom_extensions": [_extension("relationship", relationship.relationship_id)],
        }
        if relationship.description:
            relationship_payload["ai_context"] = {"instructions": relationship.description}
        relationships.append(relationship_payload)

    semantic_model: dict[str, Any] = {
        "name": product.product_key,
        "datasets": [_compile_dataset(table) for table in tables],
        "relationships": relationships,
        "metrics": [
            _compile_metric(metric)
            for metric in sorted(product.metrics, key=lambda item: item.metric_id)
        ],
        "custom_extensions": [_extension("product", product.product_id, version=product.version)],
    }
    if product.description:
        semantic_model["description"] = product.description
    product_context = _ai_context(
        synonyms=product.synonyms,
        instructions=product.instructions,
        examples=product.examples,
    )
    if product_context:
        semantic_model["ai_context"] = product_context

    compiled = {
        "version": "0.1.1",
        "dialects": ["ANSI_SQL"],
        "vendors": ["COMMON"],
        "semantic_model": [semantic_model],
    }
    validate(instance=compiled, schema=load_ossie_011_schema())
    return compiled


def _compile_dataset(table: TableIR) -> dict[str, Any]:
    primary_key = [column.name for column in table.columns if column.primary_key]
    payload: dict[str, Any] = {
        "name": table.dataset_name,
        "source": table.source,
        "fields": [
            _compile_field(column)
            for column in sorted(
                table.columns,
                key=lambda item: (item.ordinal, item.column_id),
            )
        ],
        "custom_extensions": [_extension("table", table.table_id, version=table.table_version)],
    }
    if primary_key:
        payload["primary_key"] = primary_key
    if table.unique_keys:
        payload["unique_keys"] = table.unique_keys
    if table.description:
        payload["description"] = table.description
    context = _ai_context(synonyms=table.synonyms)
    if context:
        payload["ai_context"] = context
    return payload


def _compile_field(column: ColumnIR) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": column.name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": column.name}]},
        "custom_extensions": [
            _extension(
                "column",
                column.column_id,
                attributes={
                    "data_type": column.data_type,
                    "nullable": column.nullable,
                },
            )
        ],
    }
    if column.logical_name:
        payload["label"] = column.logical_name
    if column.description:
        payload["description"] = column.description
    context = _ai_context(synonyms=column.synonyms)
    if context:
        payload["ai_context"] = context
    return payload


def _compile_metric(metric: MetricIR) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": metric.name,
        "expression": {"dialects": [{"dialect": metric.dialect, "expression": metric.expression}]},
        "custom_extensions": [_extension("metric", metric.metric_id)],
    }
    if metric.description:
        payload["description"] = metric.description
    context = _ai_context(
        synonyms=metric.synonyms,
        instructions=metric.instructions,
        examples=metric.examples,
    )
    if context:
        payload["ai_context"] = context
    return payload


def _extension(
    kind: str,
    object_id: str,
    *,
    version: int | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, str]:
    data: dict[str, Any] = {
        "id": object_id,
        "kind": kind,
        "namespace": "ai_ready_data",
    }
    if version is not None:
        data["version"] = version
    if attributes:
        data.update(attributes)
    return {
        "vendor_name": "COMMON",
        "data": json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    }


def _ai_context(
    *,
    synonyms: list[str] | None = None,
    instructions: str | None = None,
    examples: list[str] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if synonyms:
        context["synonyms"] = synonyms
    if instructions:
        context["instructions"] = instructions
    if examples:
        context["examples"] = examples
    return context
