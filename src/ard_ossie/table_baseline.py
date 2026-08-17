from __future__ import annotations

import stat
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field, ValidationError, field_validator, model_validator

from ard_ossie.canonical import canonical_hash
from ard_ossie.ir import ColumnIR
from ard_ossie.models import (
    ColumnId,
    ProductId,
    ProductRecord,
    StrictModel,
    TableId,
    TableLocator,
    Version,
)
from ard_ossie.registry import Registry

_TABLE_CONTENT_SCHEMA_VERSION = "table-content-v1"


class TableBaselineError(ValueError):
    """A redacted published-dictionary baseline contract failure."""


class PublishedColumnBaseline(StrictModel):
    column_id: ColumnId
    ordinal: int = Field(gt=0)
    name: str
    logical_name: str | None = None
    data_type: str
    nullable: bool
    primary_key: bool
    description: str | None = None
    foreign_key: str | None = None
    formula: str | None = None
    comment: str | None = None

    @field_validator("name", "data_type", mode="before")
    @classmethod
    def require_nonempty(cls, value: object) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("published column fields must be non-empty")
        return normalized


class PublishedTableBaseline(StrictModel):
    table_id: TableId
    table_version: Version
    dataset_name: str
    source: str
    description: str | None = None
    columns: list[PublishedColumnBaseline] = Field(min_length=1)

    @field_validator("dataset_name", "source", mode="before")
    @classmethod
    def require_nonempty(cls, value: object) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("published table fields must be non-empty")
        return normalized

    @model_validator(mode="after")
    def validate_columns(self) -> PublishedTableBaseline:
        column_ids = [column.column_id for column in self.columns]
        ordinals = [column.ordinal for column in self.columns]
        names = [column.name.casefold() for column in self.columns]
        if (
            len(column_ids) != len(set(column_ids))
            or len(ordinals) != len(set(ordinals))
            or len(names) != len(set(names))
        ):
            raise ValueError("published table column identities must be unique")
        return self


class PublishedDictionaryBaseline(StrictModel):
    product_id: ProductId
    product_version: Version
    tables: list[PublishedTableBaseline] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tables(self) -> PublishedDictionaryBaseline:
        table_ids = [table.table_id for table in self.tables]
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("published table IDs must be unique")
        return self


def parse_table_baseline(payload: bytes) -> PublishedDictionaryBaseline:
    try:
        text = payload.decode("utf-8", errors="strict")
        return PublishedDictionaryBaseline.model_validate_json(text)
    except (UnicodeDecodeError, ValidationError, ValueError):
        raise TableBaselineError("TABLE_BASELINE_INVALID") from None


def validate_table_baseline(
    baseline: PublishedDictionaryBaseline,
    *,
    product: ProductRecord,
    registry: Registry,
) -> dict[str, PublishedTableBaseline]:
    try:
        mappings = [
            mapping
            for mapping in registry.mappings()
            if mapping.product_id == product.product_id
        ]
        baseline_by_id = {table.table_id: table for table in baseline.tables}
        mapping_by_id = {mapping.table_id: mapping for mapping in mappings}
        if (
            baseline.product_id != product.product_id
            or baseline.product_version != product.version
            or len(mapping_by_id) != len(mappings)
            or set(baseline_by_id) != set(mapping_by_id)
        ):
            raise ValueError
        for table_id, table in baseline_by_id.items():
            mapping = mapping_by_id[table_id]
            registered = registry.get_table(table_id)
            if registered is None or table.table_version != mapping.table_version:
                raise ValueError
            expected_source = ".".join(
                (
                    registered.locator.catalog,
                    registered.locator.schema_name,
                    registered.locator.table_name,
                )
            )
            if (
                registered.version != mapping.table_version
                or table.dataset_name != registered.locator.table_name
                or table.source != expected_source
            ):
                raise ValueError
        return baseline_by_id
    except (KeyError, ValueError):
        raise TableBaselineError("TABLE_BASELINE_INVALID") from None


def read_local_table_baseline(product_path: Path) -> bytes | None:
    generated = product_path / "generated"
    path = generated / "data-dictionary.json"
    try:
        generated_stat = generated.lstat()
    except FileNotFoundError:
        return None
    try:
        path_stat = path.lstat()
        if (
            not stat.S_ISDIR(generated_stat.st_mode)
            or generated.is_symlink()
            or not stat.S_ISREG(path_stat.st_mode)
            or path.is_symlink()
        ):
            raise TableBaselineError("TABLE_BASELINE_INVALID")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise TableBaselineError("TABLE_BASELINE_INVALID") from None


def table_content_hash(
    table: PublishedTableBaseline,
    *,
    locator: TableLocator,
) -> str:
    return canonical_hash(
        {
            "schema_version": _TABLE_CONTENT_SCHEMA_VERSION,
            "table_id": table.table_id,
            "locator": locator,
            "dataset_name": table.dataset_name,
            "source": table.source,
            "description": table.description,
            "columns": [
                column.model_dump(mode="json", exclude_none=True)
                for column in sorted(
                    table.columns,
                    key=lambda item: (item.ordinal, item.column_id),
                )
            ],
        }
    )


def published_table_from_ir(
    *,
    table_id: TableId,
    table_version: Version,
    locator: TableLocator,
    description: str | None,
    columns: Sequence[ColumnIR],
) -> PublishedTableBaseline:
    return PublishedTableBaseline(
        table_id=table_id,
        table_version=table_version,
        dataset_name=locator.table_name,
        source=".".join((locator.catalog, locator.schema_name, locator.table_name)),
        description=description,
        columns=[
            PublishedColumnBaseline(
                column_id=column.column_id,
                ordinal=column.ordinal,
                name=column.name,
                logical_name=column.logical_name,
                data_type=column.data_type,
                nullable=column.nullable,
                primary_key=column.primary_key,
                description=column.description,
                foreign_key=column.foreign_key,
                formula=column.formula,
                comment=column.comment,
            )
            for column in columns
        ],
    )
