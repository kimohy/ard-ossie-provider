from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ard_ossie.ir import ColumnIR, ProductIR

_PACKAGE_TEMPLATE_ROOT = Path(__file__).resolve().parent / "assets" / "templates"
_REPOSITORY_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates"
_TEMPLATE_ROOT = (
    _PACKAGE_TEMPLATE_ROOT if _PACKAGE_TEMPLATE_ROOT.is_dir() else _REPOSITORY_TEMPLATE_ROOT
)
_ENVIRONMENT = Environment(
    loader=FileSystemLoader(_TEMPLATE_ROOT),
    autoescape=False,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
_PRODUCT_SECTIONS = (
    (
        "Overview",
        (("description", "Description"), ("purpose", "Purpose")),
    ),
    (
        "Data source",
        (
            ("domain", "Domain"),
            ("data_type", "Data type"),
            ("storage_location", "Storage location"),
            ("source_system", "Source system"),
            ("source_name", "Source name"),
        ),
    ),
    ("Tags", (("tag", "Tag"),)),
    (
        "Access and security",
        (("access", "Access"), ("security_classification", "Security classification")),
    ),
    (
        "Ownership",
        (("owner", "Owner"), ("contact", "Contact"), ("consumer", "Consumer")),
    ),
    (
        "Freshness and SLA",
        (
            ("refresh_schedule", "Refresh schedule"),
            ("freshness", "Freshness"),
            ("sla", "SLA"),
        ),
    ),
    (
        "AI readiness and quality",
        (("ai_readiness", "AI readiness"), ("quality", "Quality")),
    ),
    (
        "Constraints and notes",
        (("constraint", "Constraint"), ("related_link", "Related link")),
    ),
)


def render_product_markdown(product: ProductIR) -> str:
    template = _ENVIRONMENT.get_template("data-product.md.j2")
    return (
        template.render(
            product=product,
            sections=_product_sections(product),
            tables=sorted(product.tables, key=lambda item: item.table_id),
        ).strip()
        + "\n"
    )


def _product_sections(product: ProductIR) -> list[dict[str, object]]:
    by_kind: dict[str, list[str]] = {}
    for fact in product.product_facts:
        by_kind.setdefault(fact.kind, []).append(fact.value)

    sections: list[dict[str, object]] = []
    for heading, fields in _PRODUCT_SECTIONS:
        facts = [
            {"label": label, "value": value}
            for kind, label in fields
            for value in sorted(
                by_kind.get(kind, []),
                key=lambda item: (item.casefold(), item),
            )
        ]
        if facts:
            sections.append({"heading": heading, "facts": facts})
    return sections


def render_semantic_markdown(product: ProductIR) -> str:
    template = _ENVIRONMENT.get_template("data-semantic.md.j2")
    return template.render(product=product).strip() + "\n"


def render_dictionary_json(product: ProductIR) -> str:
    payload = {
        "product_id": product.product_id,
        "product_version": product.version,
        "tables": [
            {
                "table_id": table.table_id,
                "table_version": table.table_version,
                "dataset_name": table.dataset_name,
                "source": table.source,
                "description": table.description,
                "columns": [
                    _dictionary_column(column)
                    for column in sorted(
                        table.columns,
                        key=lambda item: (item.ordinal, item.column_id),
                    )
                ],
            }
            for table in sorted(product.tables, key=lambda item: item.table_id)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _dictionary_column(column: ColumnIR) -> dict[str, object]:
    payload: dict[str, object] = {
        "column_id": column.column_id,
        "ordinal": column.ordinal,
        "name": column.name,
        "logical_name": column.logical_name,
        "data_type": column.data_type,
        "nullable": column.nullable,
        "primary_key": column.primary_key,
        "description": column.description,
    }
    for name in ("foreign_key", "formula", "comment"):
        value = getattr(column, name)
        if value is not None:
            payload[name] = value
    return payload
