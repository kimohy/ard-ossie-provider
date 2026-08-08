from __future__ import annotations

from typing import Literal

from pydantic import Field

from ard_ossie.docling_parser import Evidence
from ard_ossie.models import (
    ColumnId,
    MetricId,
    ProductId,
    ProductKey,
    RelationshipId,
    Sha256,
    StrictModel,
    TableId,
    Version,
)


class ColumnIR(StrictModel):
    column_id: ColumnId
    ordinal: int = Field(gt=0)
    name: str
    logical_name: str | None = None
    data_type: str
    nullable: bool
    primary_key: bool = False
    unique: bool = False
    description: str | None = None
    foreign_key: str | None = None
    formula: str | None = None
    comment: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)


class TableIR(StrictModel):
    table_id: TableId
    table_version: Version
    dataset_name: str
    source: str
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    columns: list[ColumnIR] = Field(min_length=1)
    unique_keys: list[list[str]] = Field(default_factory=list)


class RelationshipIR(StrictModel):
    relationship_id: RelationshipId
    name: str
    from_table_id: TableId
    to_table_id: TableId
    from_columns: list[str] = Field(min_length=1)
    to_columns: list[str] = Field(min_length=1)
    description: str | None = None
    evidence: list[Evidence] = Field(min_length=1)


class MetricIR(StrictModel):
    metric_id: MetricId
    name: str
    expression: str
    dialect: Literal["ANSI_SQL"] = "ANSI_SQL"
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    instructions: str | None = None
    examples: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)


class ProductIR(StrictModel):
    product_id: ProductId
    product_key: ProductKey
    version: Version
    display_name: str
    description: str | None = None
    product_document_markdown: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    instructions: str | None = None
    examples: list[str] = Field(default_factory=list)
    source_hashes: dict[str, Sha256]
    tables: list[TableIR] = Field(min_length=1)
    relationships: list[RelationshipIR] = Field(default_factory=list)
    metrics: list[MetricIR] = Field(default_factory=list)
