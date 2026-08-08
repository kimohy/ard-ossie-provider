from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ProductId = Annotated[
    str,
    StringConstraints(
        pattern=r"^prd_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
TableId = Annotated[
    str,
    StringConstraints(
        pattern=r"^tbl_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
LinkId = Annotated[
    str,
    StringConstraints(
        pattern=r"^lnk_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
ColumnId = Annotated[
    str,
    StringConstraints(
        pattern=r"^col_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
MetricId = Annotated[
    str,
    StringConstraints(
        pattern=r"^met_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
RelationshipId = Annotated[
    str,
    StringConstraints(
        pattern=r"^rel_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
ProductKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Version = Annotated[int, Field(ge=1, le=999)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EntityStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class Operation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    RETIRE = "retire"


class TableLocator(StrictModel):
    source_system_id: str
    catalog: str
    schema_name: str
    table_name: str

    @field_validator("source_system_id", "catalog", "schema_name", "table_name", mode="before")
    @classmethod
    def normalize_part(cls, value: object) -> str:
        normalized = str(value).strip().lower()
        if not normalized or "|" in normalized:
            raise ValueError("table locator parts must be non-empty and cannot contain '|'")
        return normalized

    @property
    def key(self) -> str:
        return "|".join((self.source_system_id, self.catalog, self.schema_name, self.table_name))


class ProductRecord(StrictModel):
    product_id: ProductId
    product_key: ProductKey
    version: Version
    status: EntityStatus = EntityStatus.ACTIVE
    display_name: str | None = None
    aliases: list[ProductKey] = Field(default_factory=list)
    canonical_hash: Sha256 | None = None
    metrics: list[MetricRecord] = Field(default_factory=list)
    relationships: list[RelationshipRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_semantic_objects(self) -> ProductRecord:
        _validate_named_ids(self.metrics, "metric")
        _validate_named_ids(self.relationships, "relationship")
        return self


class MetricRecord(StrictModel):
    metric_id: MetricId
    name: str
    aliases: list[str] = Field(default_factory=list)
    status: EntityStatus = EntityStatus.ACTIVE


class RelationshipRecord(StrictModel):
    relationship_id: RelationshipId
    name: str
    aliases: list[str] = Field(default_factory=list)
    status: EntityStatus = EntityStatus.ACTIVE


class ColumnRecord(StrictModel):
    column_id: ColumnId
    name: str
    aliases: list[str] = Field(default_factory=list)
    status: EntityStatus = EntityStatus.ACTIVE

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("column name must be non-empty")
        return normalized


class TableRecord(StrictModel):
    table_id: TableId
    locator: TableLocator
    version: Version
    status: EntityStatus = EntityStatus.ACTIVE
    aliases: list[str] = Field(default_factory=list)
    columns: list[ColumnRecord] = Field(default_factory=list)
    schema_hash: Sha256 | None = None
    canonical_hash: Sha256 | None = None

    @model_validator(mode="after")
    def validate_columns(self) -> TableRecord:
        column_ids = [column.column_id for column in self.columns]
        column_names = [column.name.casefold() for column in self.columns]
        if len(column_ids) != len(set(column_ids)) or len(column_names) != len(set(column_names)):
            raise ValueError("column names and IDs must be unique within a table")
        return self


class ProductTableRef(StrictModel):
    link_id: LinkId
    product_id: ProductId
    table_id: TableId
    table_version: Version
    usage: Literal["SOURCE", "OUTPUT", "REFERENCE"]
    required: bool = True
    semantic_dataset: str | None = None


class CandidateChange(StrictModel):
    operation: Operation
    product: ProductRecord
    tables: list[TableRecord] = Field(default_factory=list)
    mappings: list[ProductTableRef] = Field(default_factory=list)
    base_product_version: Version | None = None
    proposed_product_version: Version | None = None
    base_table_versions: dict[TableId, Version] = Field(default_factory=dict)
    proposed_table_versions: dict[TableId, Version] = Field(default_factory=dict)
    source_hashes: dict[str, Sha256] = Field(default_factory=dict)
    changeset_id: str | None = None

    @model_validator(mode="after")
    def validate_references(self) -> CandidateChange:
        table_ids = {table.table_id for table in self.tables}
        for mapping in self.mappings:
            if mapping.product_id != self.product.product_id:
                raise ValueError("mapping references a different product")
            if mapping.table_id not in table_ids:
                raise ValueError(f"mapping references unlisted table: {mapping.table_id}")
            table = next(item for item in self.tables if item.table_id == mapping.table_id)
            if mapping.table_version != table.version:
                raise ValueError(f"mapping table version differs from table: {mapping.table_id}")
        return self


def _validate_named_ids(records: list, kind: str) -> None:
    identifiers = [
        record.metric_id if kind == "metric" else record.relationship_id for record in records
    ]
    names = [record.name.casefold() for record in records]
    if len(identifiers) != len(set(identifiers)) or len(names) != len(set(names)):
        raise ValueError(f"{kind} names and IDs must be unique within a product")
