from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from ard_ossie.canonical import canonical_hash
from ard_ossie.models import Sha256, StrictModel

SpanId = Annotated[str, StringConstraints(pattern=r"^span_[0-9a-f]{16}$")]
SEMANTIC_PARSER_VERSION = "semantic-structure-v1"
MAX_TABLE_ROWS = 10_000
MAX_TABLE_COLUMNS = 256
MAX_TABLE_GRID_AREA = 100_000
MAX_TABLE_CELLS = 100_000
MAX_LIST_DEPTH = 32


class ExtractionMode(StrEnum):
    PDF_EMBEDDED = "pdf_embedded"
    DOCX_XML = "docx_xml"
    OCR = "ocr"


def make_span_id(source_hash: str, ordinal: int) -> str:
    if ordinal < 0:
        raise ValueError("SPAN_ORDINAL_NEGATIVE")
    digest = hashlib.sha256(f"{source_hash}:{ordinal}".encode()).hexdigest()
    return f"span_{digest[:16]}"


class ImmutableStrictModel(StrictModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)


class SourceBox(ImmutableStrictModel):
    left: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_corners(self) -> SourceBox:
        if self.right < self.left or self.top < self.bottom:
            raise ValueError("SOURCE_BOX_CORNERS_INVALID")
        return self


class SourceSpan(ImmutableStrictModel):
    span_id: SpanId
    ordinal: int = Field(ge=0)
    page: int | None = Field(default=None, ge=1)
    bbox: SourceBox | None = None
    text: str = Field(min_length=1)
    text_hash: Sha256
    paragraph_break_before: bool = False

    @model_validator(mode="after")
    def validate_text_hash(self) -> SourceSpan:
        expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.text_hash != expected:
            raise ValueError("SOURCE_SPAN_TEXT_HASH_INVALID")
        return self


class NativeGroup(ImmutableStrictModel):
    order: int = Field(ge=0)
    kind: Literal["paragraph", "list_item", "table", "caption", "text_box", "alt_text"]
    span_ids: tuple[SpanId, ...]
    page: int | None = Field(default=None, ge=1)
    bbox: SourceBox | None = None
    style_name: str | None = None
    list_kind: Literal["ordered", "unordered"] | None = None
    list_depth: int | None = Field(default=None, ge=0, le=MAX_LIST_DEPTH)
    table_index: int | None = Field(default=None, ge=0)


class NativeTableCell(ImmutableStrictModel):
    start_row: int = Field(ge=0, le=MAX_TABLE_ROWS)
    end_row: int = Field(gt=0, le=MAX_TABLE_ROWS)
    start_column: int = Field(ge=0, le=MAX_TABLE_COLUMNS)
    end_column: int = Field(gt=0, le=MAX_TABLE_COLUMNS)
    span_ids: tuple[SpanId, ...] = ()
    column_header: bool = False
    bbox: SourceBox | None = None


class NativeTable(ImmutableStrictModel):
    order: int = Field(ge=0)
    row_count: int = Field(gt=0)
    column_count: int = Field(gt=0)
    cells: tuple[NativeTableCell, ...] = Field(default=(), max_length=MAX_TABLE_CELLS)

    @model_validator(mode="after")
    def validate_grid(self) -> NativeTable:
        _validate_grid(self.cells, self.row_count, self.column_count, "NATIVE_TABLE")
        return self


class NativeDocument(ImmutableStrictModel):
    source_hash: Sha256
    extraction_mode: ExtractionMode
    page_count: int = Field(ge=0)
    parser_versions: dict[str, str]
    spans: tuple[SourceSpan, ...]
    groups: tuple[NativeGroup, ...]
    tables: tuple[NativeTable, ...]

    @model_validator(mode="after")
    def validate_structure(self) -> NativeDocument:
        span_ids = [span.span_id for span in self.spans]
        ordinals = [span.ordinal for span in self.spans]
        if len(span_ids) != len(set(span_ids)) or len(ordinals) != len(set(ordinals)):
            raise ValueError("NATIVE_DOCUMENT_SPANS_NOT_UNIQUE")
        if any(span.span_id != make_span_id(self.source_hash, span.ordinal) for span in self.spans):
            raise ValueError("NATIVE_DOCUMENT_SPAN_SOURCE_MISMATCH")

        catalog = set(span_ids)
        group_orders = [group.order for group in self.groups]
        if len(group_orders) != len(set(group_orders)):
            raise ValueError("NATIVE_DOCUMENT_GROUP_ORDERS_NOT_UNIQUE")
        group_span_ids = [span_id for group in self.groups for span_id in group.span_ids]
        if len(group_span_ids) != len(set(group_span_ids)):
            raise ValueError("NATIVE_DOCUMENT_GROUP_SPANS_NOT_UNIQUE")
        table_orders = [table.order for table in self.tables]
        if len(table_orders) != len(set(table_orders)):
            raise ValueError("NATIVE_DOCUMENT_TABLE_ORDERS_NOT_UNIQUE")

        for group in self.groups:
            if not set(group.span_ids).issubset(catalog):
                raise ValueError("NATIVE_DOCUMENT_GROUP_SPAN_UNKNOWN")
            if group.kind != "table" and not group.span_ids:
                raise ValueError("NATIVE_DOCUMENT_GROUP_SPANS_REQUIRED")
            if group.kind == "table":
                if group.table_index is None or group.table_index >= len(self.tables):
                    raise ValueError("NATIVE_DOCUMENT_GROUP_TABLE_UNKNOWN")
            elif group.table_index is not None:
                raise ValueError("NATIVE_DOCUMENT_GROUP_TABLE_INVALID")

        for table in self.tables:
            for cell in table.cells:
                if not set(cell.span_ids).issubset(catalog):
                    raise ValueError("NATIVE_DOCUMENT_TABLE_SPAN_UNKNOWN")
        return self

    def span_catalog(self) -> dict[SpanId, SourceSpan]:
        return {span.span_id: span for span in self.spans}


class TableCellBlock(ImmutableStrictModel):
    start_row: int = Field(ge=0, le=MAX_TABLE_ROWS)
    end_row: int = Field(gt=0, le=MAX_TABLE_ROWS)
    start_column: int = Field(ge=0, le=MAX_TABLE_COLUMNS)
    end_column: int = Field(gt=0, le=MAX_TABLE_COLUMNS)
    span_ids: tuple[SpanId, ...] = ()
    column_header: bool = False


class HeadingBlock(ImmutableStrictModel):
    kind: Literal["heading"] = "heading"
    order: int = Field(ge=0)
    level: int = Field(ge=1, le=6)
    span_ids: tuple[SpanId, ...] = Field(min_length=1)


class ParagraphBlock(ImmutableStrictModel):
    kind: Literal["paragraph"] = "paragraph"
    order: int = Field(ge=0)
    span_ids: tuple[SpanId, ...] = Field(min_length=1)


class ListItemBlock(ImmutableStrictModel):
    kind: Literal["list_item"] = "list_item"
    order: int = Field(ge=0)
    list_kind: Literal["ordered", "unordered"]
    depth: int = Field(ge=0, le=MAX_LIST_DEPTH)
    span_ids: tuple[SpanId, ...] = Field(min_length=1)


class TableBlock(ImmutableStrictModel):
    kind: Literal["table"] = "table"
    order: int = Field(ge=0)
    row_count: int = Field(gt=0)
    column_count: int = Field(gt=0)
    cells: tuple[TableCellBlock, ...] = Field(min_length=1, max_length=MAX_TABLE_CELLS)

    @model_validator(mode="after")
    def validate_grid(self) -> TableBlock:
        _validate_grid(self.cells, self.row_count, self.column_count, "TABLE_BLOCK")
        return self


class LosslessBlock(ImmutableStrictModel):
    kind: Literal["lossless"] = "lossless"
    order: int = Field(ge=0)
    span_ids: tuple[SpanId, ...] = Field(min_length=1)
    reason: Literal["structure_unresolved", "provider_unavailable", "repair_rejected"]


SemanticBlock = Annotated[
    HeadingBlock | ParagraphBlock | ListItemBlock | TableBlock | LosslessBlock,
    Field(discriminator="kind"),
]


class ExcludedSpan(ImmutableStrictModel):
    span_id: SpanId
    kind: Literal["page_header", "page_footer", "page_number"]


class ReconciledDocument(ImmutableStrictModel):
    blocks: tuple[SemanticBlock, ...]
    excluded_spans: tuple[ExcludedSpan, ...] = ()

    @model_validator(mode="after")
    def validate_allocations(self) -> ReconciledDocument:
        orders = [block.order for block in self.blocks]
        if len(orders) != len(set(orders)):
            raise ValueError("RECONCILED_DOCUMENT_BLOCK_ORDERS_NOT_UNIQUE")
        excluded_ids = [span.span_id for span in self.excluded_spans]
        if len(excluded_ids) != len(set(excluded_ids)):
            raise ValueError("RECONCILED_DOCUMENT_EXCLUDED_SPANS_NOT_UNIQUE")

        allocations: list[SpanId] = []
        for block in self.blocks:
            if isinstance(block, TableBlock):
                allocations.extend(span_id for cell in block.cells for span_id in cell.span_ids)
            else:
                allocations.extend(block.span_ids)
        if len(allocations) != len(set(allocations)):
            raise ValueError("RECONCILED_DOCUMENT_SPAN_ALLOCATIONS_NOT_UNIQUE")
        if set(allocations) & set(excluded_ids):
            raise ValueError("RECONCILED_DOCUMENT_SPAN_EXCLUDED")
        return self


class RepairCell(StrictModel):
    start_row: int = Field(ge=0, le=MAX_TABLE_ROWS)
    end_row: int = Field(gt=0, le=MAX_TABLE_ROWS)
    start_column: int = Field(ge=0, le=MAX_TABLE_COLUMNS)
    end_column: int = Field(gt=0, le=MAX_TABLE_COLUMNS)
    span_ids: list[SpanId]
    column_header: bool


class RepairBlock(StrictModel):
    kind: Literal["heading", "paragraph", "list_item", "table", "exclude"]
    order: int = Field(ge=0)
    span_ids: list[SpanId]
    heading_level: int | None
    list_kind: Literal["ordered", "unordered"] | None
    list_depth: int | None = Field(le=MAX_LIST_DEPTH)
    row_count: int | None = Field(le=MAX_TABLE_ROWS)
    column_count: int | None = Field(le=MAX_TABLE_COLUMNS)
    cells: list[RepairCell] = Field(max_length=MAX_TABLE_CELLS)
    exclusion_kind: Literal["page_header", "page_footer", "page_number"] | None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_structure(self) -> RepairBlock:
        is_table = self.kind == "table"
        if is_table:
            if self.row_count is None or self.column_count is None:
                raise ValueError("REPAIR_BLOCK_TABLE_DIMENSIONS_REQUIRED")
            if self.row_count <= 0 or self.column_count <= 0:
                raise ValueError("REPAIR_BLOCK_TABLE_DIMENSIONS_INVALID")
            if self.span_ids:
                raise ValueError("REPAIR_BLOCK_TABLE_SPANS_INVALID")
            _validate_grid(self.cells, self.row_count, self.column_count, "REPAIR_BLOCK")
        elif self.row_count is not None or self.column_count is not None or self.cells:
            raise ValueError("REPAIR_BLOCK_TABLE_FIELDS_INVALID")

        if self.kind == "heading":
            if self.heading_level is None or not 1 <= self.heading_level <= 6:
                raise ValueError("REPAIR_BLOCK_HEADING_LEVEL_INVALID")
        elif self.heading_level is not None:
            raise ValueError("REPAIR_BLOCK_HEADING_LEVEL_INVALID")

        if self.kind == "list_item":
            if self.list_kind is None or self.list_depth is None or self.list_depth < 0:
                raise ValueError("REPAIR_BLOCK_LIST_METADATA_INVALID")
        elif self.list_kind is not None or self.list_depth is not None:
            raise ValueError("REPAIR_BLOCK_LIST_METADATA_INVALID")

        if self.kind == "exclude":
            if self.exclusion_kind is None:
                raise ValueError("REPAIR_BLOCK_EXCLUSION_KIND_REQUIRED")
        elif self.exclusion_kind is not None:
            raise ValueError("REPAIR_BLOCK_EXCLUSION_KIND_INVALID")
        return self


class RepairPlan(StrictModel):
    blocks: list[RepairBlock]

    @model_validator(mode="after")
    def validate_span_allocations(self) -> RepairPlan:
        allocations: list[SpanId] = []
        for block in self.blocks:
            if block.kind == "table":
                for cell in block.cells:
                    allocations.extend(cell.span_ids)
            else:
                allocations.extend(block.span_ids)
        if len(allocations) != len(set(allocations)):
            raise ValueError("REPAIR_PLAN_SPAN_ALLOCATIONS_NOT_UNIQUE")
        return self


class FidelityThresholds(StrictModel):
    overlap_weight: float = 0.55
    text_similarity_weight: float = 0.35
    order_weight: float = 0.10
    acceptance_score: float = 0.72
    page_edge_band: float = 0.10
    repeat_ratio: float = 0.60


class RemovedElementAudit(StrictModel):
    kind: Literal["page_header", "page_footer", "page_number"]
    page: int = Field(ge=1)
    bbox: SourceBox | None
    text_hash: Sha256


class SpanProvenanceAudit(StrictModel):
    page: int | None = Field(default=None, ge=1)
    bbox: SourceBox | None
    text_hash: Sha256


class DegradedBlockAudit(StrictModel):
    order: int = Field(ge=0)
    reason: Literal["structure_unresolved", "provider_unavailable", "repair_rejected"]
    spans: list[SpanProvenanceAudit] = Field(min_length=1)


class TableFidelityResult(StrictModel):
    order: int = Field(ge=0)
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    matched_cell_count: int = Field(ge=0)
    total_cell_count: int = Field(ge=0)
    status: Literal["resolved", "repaired", "degraded"]


class SemanticFidelityReport(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://github.com/kimohy/ard-ossie-provider/schemas/reports/semantic-fidelity.schema.json",
        },
    )

    source_hash: Sha256
    extraction_mode: ExtractionMode
    page_count: int = Field(ge=0)
    parser_versions: dict[str, str]
    status: Literal["PASS", "WARN", "FAIL"]
    heading_count: int = Field(ge=0)
    paragraph_count: int = Field(ge=0)
    list_item_count: int = Field(ge=0)
    table_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    cell_count: int = Field(ge=0)
    source_span_count: int = Field(ge=0)
    preserved_span_count: int = Field(ge=0)
    excluded_span_count: int = Field(ge=0)
    unmatched_span_count: int = Field(ge=0)
    duplicated_span_count: int = Field(ge=0)
    degraded_block_count: int = Field(ge=0)
    source_text_coverage: float = Field(ge=0, le=1)
    removed_elements: list[RemovedElementAudit] = Field(default_factory=list)
    degraded_blocks: list[DegradedBlockAudit] = Field(default_factory=list)
    table_results: list[TableFidelityResult] = Field(default_factory=list)
    thresholds: FidelityThresholds = Field(default_factory=FidelityThresholds)

    @model_validator(mode="after")
    def validate_fidelity(self) -> SemanticFidelityReport:
        if self.degraded_block_count != len(self.degraded_blocks):
            raise ValueError("FIDELITY_DEGRADED_BLOCK_COUNT_INVALID")
        if self.source_span_count != (
            self.preserved_span_count + self.excluded_span_count + self.unmatched_span_count
        ):
            raise ValueError("FIDELITY_SPAN_COUNTS_INVALID")
        coverage = (
            1.0
            if self.source_span_count == 0
            else (self.preserved_span_count + self.excluded_span_count) / self.source_span_count
        )
        if self.source_text_coverage != coverage:
            raise ValueError("FIDELITY_COVERAGE_INVALID")
        if self.unmatched_span_count or self.duplicated_span_count:
            if self.status != "FAIL":
                raise ValueError("FIDELITY_STATUS_FAIL_REQUIRED")
        elif self.extraction_mode is ExtractionMode.OCR or self.degraded_block_count:
            if self.status != "WARN":
                raise ValueError("FIDELITY_STATUS_WARN_REQUIRED")
        elif self.status != "PASS":
            raise ValueError("FIDELITY_STATUS_PASS_REQUIRED")
        return self


class SemanticStructureRepairRecord(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://github.com/kimohy/ard-ossie-provider/schemas/reports/semantic-structure-repair.schema.json",
        },
    )

    source_hash: Sha256
    ordered_span_hashes: list[Sha256]
    parser_version: str
    prompt_version: str
    schema_hash: Sha256
    provider: str
    model: str
    outcome: Literal["applied", "reused", "rejected", "degraded"]
    plan: RepairPlan | None
    provider_error_code: str | None
    validation_codes: list[str]
    applied_orders: list[int]
    rejected_orders: list[int]
    plan_hash: Sha256 | None

    @model_validator(mode="after")
    def validate_record(self) -> SemanticStructureRepairRecord:
        applied = set(self.applied_orders)
        rejected = set(self.rejected_orders)
        if len(applied) != len(self.applied_orders) or len(rejected) != len(self.rejected_orders):
            raise ValueError("REPAIR_RECORD_ORDERS_NOT_UNIQUE")
        if applied & rejected:
            raise ValueError("REPAIR_RECORD_ORDERS_OVERLAP")
        if self.outcome in {"applied", "reused"} and (
            self.plan is None or self.plan_hash is None
        ):
            raise ValueError("REPAIR_RECORD_PLAN_REQUIRED")
        if self.plan_hash is not None and (
            self.plan is None
            or self.plan_hash != canonical_hash(self.plan.model_dump(mode="json"))
        ):
            raise ValueError("REPAIR_RECORD_PLAN_HASH_INVALID")
        return self


def _validate_grid(
    cells: tuple[NativeTableCell, ...] | tuple[TableCellBlock, ...] | list[RepairCell],
    row_count: int,
    column_count: int,
    error_prefix: str,
) -> None:
    if row_count <= 0 or column_count <= 0:
        raise ValueError("TABLE_DIMENSIONS_INVALID")
    if row_count > MAX_TABLE_ROWS:
        raise ValueError("TABLE_ROWS_LIMIT_EXCEEDED")
    if column_count > MAX_TABLE_COLUMNS:
        raise ValueError("TABLE_COLUMNS_LIMIT_EXCEEDED")
    if row_count * column_count > MAX_TABLE_GRID_AREA:
        raise ValueError("TABLE_GRID_AREA_LIMIT_EXCEEDED")
    if len(cells) > MAX_TABLE_CELLS:
        raise ValueError("TABLE_CELL_COUNT_LIMIT_EXCEEDED")
    grid_area = row_count * column_count
    occupied = bytearray(grid_area)
    occupied_count = 0
    for cell in cells:
        start_row = cell.start_row
        end_row = cell.end_row
        start_column = cell.start_column
        end_column = cell.end_column
        if (
            start_row < 0
            or start_column < 0
            or end_row <= start_row
            or end_column <= start_column
        ):
            raise ValueError(f"{error_prefix}_CELL_OFFSETS_INVALID")
        if end_row > row_count or end_column > column_count:
            raise ValueError(f"{error_prefix}_CELL_OFFSETS_OUT_OF_BOUNDS")
        for row in range(start_row, end_row):
            row_offset = row * column_count
            for column in range(start_column, end_column):
                position = row_offset + column
                if occupied[position]:
                    raise ValueError(f"{error_prefix}_CELL_REGIONS_OVERLAP")
                occupied[position] = 1
                occupied_count += 1
    if occupied_count != grid_area:
        raise ValueError(f"{error_prefix}_CELL_GRID_NOT_PARTITIONED")
