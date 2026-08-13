import hashlib
import tracemalloc

import pytest
from pydantic import ValidationError

from ard_ossie.canonical import canonical_hash
from ard_ossie.semantic.models import (
    DegradedBlockAudit,
    ExtractionMode,
    NativeDocument,
    NativeGroup,
    NativeTable,
    NativeTableCell,
    ParagraphBlock,
    ReconciledDocument,
    RepairBlock,
    RepairCell,
    RepairPlan,
    SemanticFidelityReport,
    SemanticStructureRepairRecord,
    SourceBox,
    SourceSpan,
    TableBlock,
    TableCellBlock,
    _validate_grid,
    make_span_id,
)

SOURCE_HASH = "a" * 64


def source_span(ordinal: int = 0, text: str = "원문") -> SourceSpan:
    return SourceSpan(
        span_id=make_span_id(SOURCE_HASH, ordinal),
        ordinal=ordinal,
        text=text,
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def paragraph_repair_block(order: int, span_ids: list[str]) -> RepairBlock:
    return RepairBlock(
        kind="paragraph",
        order=order,
        span_ids=span_ids,
        heading_level=None,
        list_kind=None,
        list_depth=None,
        row_count=None,
        column_count=None,
        cells=[],
        exclusion_kind=None,
        confidence=1.0,
    )


def fidelity_report(**overrides: object) -> SemanticFidelityReport:
    values: dict[str, object] = {
        "source_hash": SOURCE_HASH,
        "extraction_mode": ExtractionMode.DOCX_XML,
        "page_count": 1,
        "parser_versions": {},
        "status": "PASS",
        "heading_count": 0,
        "paragraph_count": 1,
        "list_item_count": 0,
        "table_count": 0,
        "row_count": 0,
        "cell_count": 0,
        "source_span_count": 1,
        "preserved_span_count": 1,
        "excluded_span_count": 0,
        "unmatched_span_count": 0,
        "duplicated_span_count": 0,
        "degraded_block_count": 0,
        "source_text_coverage": 1.0,
    }
    values.update(overrides)
    return SemanticFidelityReport.model_validate(values)


def test_source_span_id_is_bound_to_source_and_ordinal() -> None:
    first = make_span_id(SOURCE_HASH, 7)
    assert first == make_span_id(SOURCE_HASH, 7)
    assert first != make_span_id(SOURCE_HASH, 8)
    span = SourceSpan(
        span_id=first,
        ordinal=7,
        page=1,
        bbox=SourceBox(left=0.1, bottom=0.2, right=0.8, top=0.3),
        text="개인정보 | 유효성",
        text_hash=hashlib.sha256("개인정보 | 유효성".encode()).hexdigest(),
    )
    assert span.text == "개인정보 | 유효성"


def test_native_document_rejects_span_id_from_another_source() -> None:
    span = SourceSpan(
        span_id=make_span_id("b" * 64, 0),
        ordinal=0,
        text="원문",
        text_hash=hashlib.sha256("원문".encode()).hexdigest(),
    )
    with pytest.raises(ValidationError):
        NativeDocument(
            source_hash=SOURCE_HASH,
            extraction_mode=ExtractionMode.DOCX_XML,
            page_count=0,
            parser_versions={},
            spans=[span],
            groups=[],
            tables=[],
        )


def test_repair_models_cannot_accept_authored_text() -> None:
    with pytest.raises(ValidationError):
        RepairBlock.model_validate(
            {
                "kind": "table",
                "order": 0,
                "span_ids": [],
                "heading_level": None,
                "list_kind": None,
                "list_depth": None,
                "row_count": 1,
                "column_count": 1,
                "cells": [
                    RepairCell(
                        start_row=0,
                        end_row=1,
                        start_column=0,
                        end_column=1,
                        span_ids=[make_span_id(SOURCE_HASH, 0)],
                        column_header=True,
                    ).model_dump()
                ],
                "exclusion_kind": None,
                "confidence": 1.0,
                "text": "LLM-authored value",
            }
        )


def test_repair_plan_requires_closed_structural_blocks() -> None:
    plan = RepairPlan(blocks=[])
    assert plan.model_dump() == {"blocks": []}
    assert ExtractionMode.PDF_EMBEDDED.value == "pdf_embedded"


def test_source_box_rejects_inverted_geometry() -> None:
    with pytest.raises(ValidationError, match="SOURCE_BOX_CORNERS_INVALID"):
        SourceBox(left=0.8, bottom=0.2, right=0.1, top=0.3)


def test_source_span_rejects_text_hash_mismatch() -> None:
    with pytest.raises(ValidationError, match="SOURCE_SPAN_TEXT_HASH_INVALID"):
        SourceSpan(
            span_id=make_span_id(SOURCE_HASH, 0),
            ordinal=0,
            text="원문",
            text_hash="b" * 64,
        )


def test_native_document_rejects_table_grid_with_gap() -> None:
    with pytest.raises(ValidationError, match="NATIVE_TABLE_CELL_GRID_NOT_PARTITIONED"):
        NativeDocument.model_validate(
            {
                "source_hash": SOURCE_HASH,
                "extraction_mode": "docx_xml",
                "page_count": 1,
                "parser_versions": {},
                "spans": [source_span().model_dump()],
                "groups": [
                    {
                        "order": 0,
                        "kind": "table",
                        "span_ids": [],
                        "table_index": 0,
                    }
                ],
                "tables": [
                    {
                        "order": 0,
                        "row_count": 1,
                        "column_count": 2,
                        "cells": [
                            {
                                "start_row": 0,
                                "end_row": 1,
                                "start_column": 0,
                                "end_column": 1,
                            }
                        ],
                    }
                ],
            }
        )


def test_native_document_rejects_unknown_table_span_reference() -> None:
    with pytest.raises(ValidationError, match="NATIVE_DOCUMENT_TABLE_SPAN_UNKNOWN"):
        NativeDocument(
            source_hash=SOURCE_HASH,
            extraction_mode=ExtractionMode.DOCX_XML,
            page_count=1,
            parser_versions={},
            spans=[source_span()],
            groups=[NativeGroup(order=0, kind="table", span_ids=(), table_index=0)],
            tables=[
                NativeTable(
                    order=0,
                    row_count=1,
                    column_count=1,
                    cells=[
                        NativeTableCell(
                            start_row=0,
                            end_row=1,
                            start_column=0,
                            end_column=1,
                            span_ids=(make_span_id(SOURCE_HASH, 1),),
                        )
                    ],
                )
            ],
        )


@pytest.mark.parametrize(
    "groups",
    [
        [NativeGroup(order=0, kind="paragraph", span_ids=(make_span_id(SOURCE_HASH, 0),) * 2)],
        [
            NativeGroup(order=0, kind="paragraph", span_ids=(make_span_id(SOURCE_HASH, 0),)),
            NativeGroup(order=1, kind="paragraph", span_ids=(make_span_id(SOURCE_HASH, 0),)),
        ],
    ],
)
def test_native_document_rejects_duplicate_span_allocations_across_groups(
    groups: list[NativeGroup],
) -> None:
    with pytest.raises(ValidationError, match="NATIVE_DOCUMENT_GROUP_SPANS_NOT_UNIQUE"):
        NativeDocument(
            source_hash=SOURCE_HASH,
            extraction_mode=ExtractionMode.PDF_EMBEDDED,
            page_count=1,
            parser_versions={},
            spans=[source_span()],
            groups=groups,
            tables=[],
        )


def test_reconciled_document_rejects_duplicate_span_allocations() -> None:
    span_id = make_span_id(SOURCE_HASH, 0)
    with pytest.raises(ValidationError, match="RECONCILED_DOCUMENT_SPAN_ALLOCATIONS_NOT_UNIQUE"):
        ReconciledDocument(
            blocks=[
                ParagraphBlock(order=0, span_ids=(span_id,)),
                ParagraphBlock(order=1, span_ids=(span_id,)),
            ]
        )


def test_repair_plan_rejects_duplicate_span_within_a_block() -> None:
    span_id = make_span_id(SOURCE_HASH, 0)
    with pytest.raises(ValidationError, match="REPAIR_PLAN_SPAN_ALLOCATIONS_NOT_UNIQUE"):
        RepairPlan(blocks=[paragraph_repair_block(0, [span_id, span_id])])


def test_repair_plan_rejects_duplicate_span_across_table_cells() -> None:
    span_id = make_span_id(SOURCE_HASH, 0)
    table = RepairBlock(
        kind="table",
        order=0,
        span_ids=[],
        heading_level=None,
        list_kind=None,
        list_depth=None,
        row_count=1,
        column_count=2,
        cells=[
            RepairCell(
                start_row=0,
                end_row=1,
                start_column=0,
                end_column=1,
                span_ids=[span_id],
                column_header=False,
            ),
            RepairCell(
                start_row=0,
                end_row=1,
                start_column=1,
                end_column=2,
                span_ids=[span_id],
                column_header=False,
            ),
        ],
        exclusion_kind=None,
        confidence=1.0,
    )
    with pytest.raises(ValidationError, match="REPAIR_PLAN_SPAN_ALLOCATIONS_NOT_UNIQUE"):
        RepairPlan(blocks=[table])


def test_repair_plan_rejects_duplicate_span_across_blocks() -> None:
    span_id = make_span_id(SOURCE_HASH, 0)
    with pytest.raises(ValidationError, match="REPAIR_PLAN_SPAN_ALLOCATIONS_NOT_UNIQUE"):
        RepairPlan(
            blocks=[
                paragraph_repair_block(0, [span_id]),
                paragraph_repair_block(1, [span_id]),
            ]
        )


def test_table_grid_validation_stays_memory_safe_for_large_dimensions() -> None:
    tracemalloc.start()
    try:
        with pytest.raises(ValidationError, match="TABLE_ROWS_LIMIT_EXCEEDED"):
            TableBlock(
                order=0,
                row_count=1_000_000_000,
                column_count=1_000_000_000,
                cells=[
                    TableCellBlock(
                        start_row=0,
                        end_row=1,
                        start_column=0,
                        end_column=1,
                    )
                ],
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000


def test_grid_validation_work_is_linear_in_cells_and_area() -> None:
    reads = 0

    class CountingCell:
        def __init__(self, row: int, column: int) -> None:
            self._row = row
            self._column = column

        @property
        def start_row(self) -> int:
            nonlocal reads
            reads += 1
            return self._row

        @property
        def end_row(self) -> int:
            nonlocal reads
            reads += 1
            return self._row + 1

        @property
        def start_column(self) -> int:
            nonlocal reads
            reads += 1
            return self._column

        @property
        def end_column(self) -> int:
            nonlocal reads
            reads += 1
            return self._column + 1

    cells = [CountingCell(row, column) for row in range(16) for column in range(256)]

    _validate_grid(cells, 16, 256, "TABLE_BLOCK")  # type: ignore[arg-type]

    assert reads <= 16 * (16 * 256 + len(cells))


@pytest.mark.parametrize(
    ("model", "kwargs", "error"),
    [
        (
            NativeTable,
            {
                "order": 0,
                "row_count": 1,
                "column_count": 257,
                "cells": (
                    NativeTableCell(
                        start_row=0,
                        end_row=1,
                        start_column=0,
                        end_column=256,
                    ),
                ),
            },
            "TABLE_COLUMNS_LIMIT_EXCEEDED",
        ),
        (
            TableBlock,
            {
                "order": 0,
                "row_count": 10_001,
                "column_count": 1,
                "cells": (
                    TableCellBlock(
                        start_row=0,
                        end_row=10_000,
                        start_column=0,
                        end_column=1,
                    ),
                ),
            },
            "TABLE_ROWS_LIMIT_EXCEEDED",
        ),
    ],
)
def test_semantic_models_reject_unbounded_table_dimensions(
    model: type[object],
    kwargs: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        model(**kwargs)  # type: ignore[call-arg]


def test_semantic_models_reject_unbounded_list_depth() -> None:
    span_id = make_span_id(SOURCE_HASH, 0)
    with pytest.raises(ValidationError, match="less than or equal to 32"):
        RepairBlock(
            kind="list_item",
            order=0,
            span_ids=[span_id],
            heading_level=None,
            list_kind="unordered",
            list_depth=33,
            row_count=None,
            column_count=None,
            cells=[],
            exclusion_kind=None,
            confidence=1.0,
        )


def test_fidelity_report_rejects_inconsistent_span_counts() -> None:
    with pytest.raises(ValidationError, match="FIDELITY_SPAN_COUNTS_INVALID"):
        fidelity_report(preserved_span_count=0)


def test_fidelity_report_requires_fail_for_unmatched_spans() -> None:
    with pytest.raises(ValidationError, match="FIDELITY_STATUS_FAIL_REQUIRED"):
        fidelity_report(
            status="WARN",
            source_span_count=2,
            preserved_span_count=1,
            unmatched_span_count=1,
            source_text_coverage=0.5,
        )


def test_fidelity_report_requires_warn_for_ocr_or_degradation() -> None:
    with pytest.raises(ValidationError, match="FIDELITY_STATUS_WARN_REQUIRED"):
        fidelity_report(extraction_mode=ExtractionMode.OCR)
    with pytest.raises(ValidationError, match="FIDELITY_STATUS_WARN_REQUIRED"):
        fidelity_report(
            degraded_block_count=1,
            degraded_blocks=[
                DegradedBlockAudit(
                    order=0,
                    reason="repair_rejected",
                    spans=[{"bbox": None, "text_hash": SOURCE_HASH}],
                )
            ],
        )


def test_repair_record_requires_canonical_plan_hash() -> None:
    plan = RepairPlan(blocks=[])
    record = SemanticStructureRepairRecord(
        source_hash=SOURCE_HASH,
        ordered_span_hashes=[],
        parser_version="semantic-structure-v1",
        prompt_version="v1",
        schema_hash=SOURCE_HASH,
        provider="provider",
        model="model",
        outcome="applied",
        plan=plan,
        provider_error_code=None,
        validation_codes=[],
        applied_orders=[],
        rejected_orders=[],
        plan_hash=canonical_hash(plan.model_dump(mode="json")),
    )
    assert record.plan_hash == canonical_hash({"blocks": []})

    with pytest.raises(ValidationError, match="REPAIR_RECORD_PLAN_HASH_INVALID"):
        SemanticStructureRepairRecord.model_validate(
            {**record.model_dump(), "plan_hash": "b" * 64}
        )
