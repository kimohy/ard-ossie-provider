"""Lossless semantic-document parsing orchestration."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ard_ossie.canonical import canonical_hash
from ard_ossie.ingestion import SourceFile, SourceValidationError, materialized_source_path
from ard_ossie.llm import ProviderExecutionError
from ard_ossie.semantic.models import (
    DegradedBlockAudit,
    ExcludedSpan,
    ExtractionMode,
    HeadingBlock,
    ListItemBlock,
    LosslessBlock,
    NativeDocument,
    NativeTable,
    OcrCorrectionPageAudit,
    ParagraphBlock,
    ReconciledDocument,
    RemovedElementAudit,
    RepairBlock,
    RepairPlan,
    SemanticBlock,
    SemanticFidelityReport,
    SemanticStructureRepairRecord,
    SpanId,
    SpanProvenanceAudit,
    TableBlock,
    TableCellBlock,
    TableFidelityResult,
)
from ard_ossie.semantic.render import (
    CoverageResult,
    render_semantic_markdown,
    validate_source_coverage,
)
from ard_ossie.semantic.repair import SemanticStructureRepairPlanner
from ard_ossie.semantic.sources import (
    SemanticSourceError,
    extract_docx_native,
    extract_ocr_native,
)
from ard_ossie.semantic.structure import (
    ReconciliationResult,
    StructureDocument,
    build_docling_skeleton,
    reconcile_structure,
)

if TYPE_CHECKING:
    from ard_ossie.docling_parser import Evidence
    from ard_ossie.semantic.correction import OcrCorrectionPlanner


@dataclass(frozen=True)
class SemanticParseResult:
    markdown: str
    has_content: bool
    evidence: tuple[Evidence, ...]
    fidelity: SemanticFidelityReport
    repair_record: SemanticStructureRepairRecord | None


def parse_semantic_document(
    source: SourceFile,
    *,
    converter: Any | None = None,
    full_page_ocr_converter: Any | None = None,
    repair_planner: SemanticStructureRepairPlanner | None = None,
    trusted_record: SemanticStructureRepairRecord | None = None,
    correction_planner: OcrCorrectionPlanner | None = None,
    trusted_fidelity: SemanticFidelityReport | None = None,
    pdfium: Any | None = None,
) -> SemanticParseResult:
    """Parse a PDF or DOCX without allowing structural hints to author text."""
    native, skeleton = _native_and_structure(
        source,
        converter=converter,
        full_page_ocr_converter=full_page_ocr_converter,
        pdfium=pdfium,
    )
    correction_audits: tuple[OcrCorrectionPageAudit, ...] = ()
    correction_warnings: tuple[str, ...] = ()
    if native.extraction_mode is ExtractionMode.OCR:
        if correction_planner is None:
            correction_warnings = ("SEMANTIC_OCR_CORRECTION_UNAVAILABLE",)
        else:
            correction = correction_planner.correct(
                source,
                native,
                trusted_fidelity=trusted_fidelity,
                pdfium=pdfium,
            )
            native = correction.document
            correction_audits = correction.audits
            correction_warnings = correction.warning_codes
    reconciled = reconcile_structure(native, skeleton)
    completed, repair_record = _repair_and_degrade(
        native,
        skeleton,
        reconciled,
        repair_planner=repair_planner,
        trusted_record=trusted_record,
    )
    coverage = validate_source_coverage(native, completed)
    markdown = render_semantic_markdown(completed, native.span_catalog())
    fidelity = _build_fidelity(
        native,
        completed,
        coverage,
        repair_record,
        correction_audits=correction_audits,
        correction_warnings=correction_warnings,
    )
    excluded_ids = {item.span_id for item in completed.excluded_spans}
    has_content = any(
        not span.text.isspace() for span in native.spans if span.span_id not in excluded_ids
    )
    return SemanticParseResult(
        markdown=markdown,
        has_content=has_content,
        evidence=tuple(_semantic_evidence(source, native, completed)),
        fidelity=fidelity,
        repair_record=repair_record,
    )


def _native_and_structure(
    source: SourceFile,
    *,
    converter: Any | None,
    full_page_ocr_converter: Any | None,
    pdfium: Any | None,
) -> tuple[NativeDocument, StructureDocument]:
    suffix = source.path.suffix.casefold()
    if suffix == ".pdf":
        ocr_document = _convert_with_full_page_ocr(
            source,
            full_page_ocr_converter=full_page_ocr_converter,
        )
        native = extract_ocr_native(source, ocr_document)
        if not native.spans:
            raise SemanticSourceError("SEMANTIC_OCR_UNREADABLE")
        skeleton = build_docling_skeleton(ocr_document)
    elif suffix == ".docx":
        native = extract_docx_native(source)
        skeleton = _ordinary_structure(source, converter=converter)
    else:
        raise SourceValidationError(f"SOURCE_EXTENSION_NOT_ALLOWED: {suffix}")
    merged_versions = {
        **native.parser_versions,
        "docling": importlib.metadata.version("docling"),
        "docling-core": importlib.metadata.version("docling-core"),
    }
    return native.model_copy(update={"parser_versions": merged_versions}), skeleton


def _ordinary_structure(
    source: SourceFile,
    *,
    converter: Any | None,
) -> StructureDocument:
    from docling.exceptions import ConversionError

    active_converter = converter or _new_converter()
    try:
        with materialized_source_path(source) as private_path:
            result = active_converter.convert(str(private_path))
    except ConversionError:
        return StructureDocument(blocks=())
    return build_docling_skeleton(result.document)


def _convert_with_full_page_ocr(
    source: SourceFile,
    *,
    full_page_ocr_converter: Any | None,
) -> Any:
    from docling.exceptions import ConversionError

    active_converter = full_page_ocr_converter or _new_full_page_ocr_converter()
    try:
        with materialized_source_path(source) as private_path:
            result = active_converter.convert(str(private_path))
    except ConversionError as exc:
        raise SemanticSourceError("SEMANTIC_OCR_UNREADABLE") from exc
    return result.document


def _new_converter() -> Any:
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def _new_full_page_ocr_converter() -> Any:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import OcrAutoOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=PdfPipelineOptions(
                    do_ocr=True,
                    ocr_options=OcrAutoOptions(force_full_page_ocr=True),
                )
            )
        }
    )


def _repair_and_degrade(
    native: NativeDocument,
    skeleton: StructureDocument,
    reconciled: ReconciliationResult,
    *,
    repair_planner: SemanticStructureRepairPlanner | None,
    trusted_record: SemanticStructureRepairRecord | None,
) -> tuple[ReconciledDocument, SemanticStructureRepairRecord | None]:
    blocks = list(reconciled.blocks)
    excluded = list(reconciled.excluded_spans)
    unresolved = tuple(reconciled.unresolved_span_ids)
    repair_record: SemanticStructureRepairRecord | None = None
    reason = (
        "structure_unresolved"
        if native.extraction_mode is ExtractionMode.OCR
        else "provider_unavailable"
    )
    if (
        unresolved
        and native.extraction_mode is not ExtractionMode.OCR
        and repair_planner is not None
    ):
        try:
            application = repair_planner.repair(
                native,
                skeleton,
                unresolved,
                trusted_record=trusted_record,
            )
        except ProviderExecutionError:
            # Provider failures never get to author or remove source allocations.
            # The native fallback below records this as provider_unavailable.
            pass
        else:
            accepted, repair_record = _validate_native_table_repairs(
                native,
                application.blocks,
                application.record,
            )
            blocks.extend(accepted)
            excluded.extend(_applied_exclusions(repair_record))
            if repair_record.outcome == "rejected":
                reason = "repair_rejected"
            elif repair_record.outcome in {"applied", "reused"}:
                reason = "structure_unresolved"

    blocks, excluded = _enforce_native_tables(
        native,
        blocks,
        excluded,
        reason=reason,
    )
    allocated = set(_block_span_ids(blocks))
    excluded_ids = {item.span_id for item in excluded}
    remaining_ids = {
        span.span_id
        for span in native.spans
        if span.span_id not in allocated and span.span_id not in excluded_ids
    }
    used_orders = {block.order for block in blocks}
    native_table_ids = {
        span_id
        for table in native.tables
        for cell in table.cells
        for span_id in cell.span_ids
    }

    for preferred, paragraph_ids in _ordinary_paragraphs(
        native,
        remaining_ids=remaining_ids,
        native_table_ids=native_table_ids,
    ):
        order = _available_order(preferred, used_orders)
        blocks.append(LosslessBlock(order=order, span_ids=paragraph_ids, reason=reason))
        used_orders.add(order)
        remaining_ids.difference_update(paragraph_ids)

    # Unknown native grouping is retained losslessly and audited; coverage stays hard.
    for paragraph_ids in _contiguous_span_ids(
        [
            span
            for span in sorted(native.spans, key=lambda item: item.ordinal)
            if span.span_id in remaining_ids
        ]
    ):
        order = _available_order(native.span_catalog()[paragraph_ids[0]].ordinal, used_orders)
        blocks.append(LosslessBlock(order=order, span_ids=paragraph_ids, reason=reason))
        used_orders.add(order)

    return _completed_document_and_repair(
        native,
        blocks,
        excluded,
        repair_record,
    )


def _validate_native_table_repairs(
    native: NativeDocument,
    application_blocks: tuple[SemanticBlock, ...],
    record: SemanticStructureRepairRecord,
) -> tuple[list[SemanticBlock], SemanticStructureRepairRecord]:
    if record.plan is None:
        return list(application_blocks), record
    invalid_orders: set[int] = set()
    for table in native.tables:
        table_ids = set(_native_table_span_ids(table))
        if not table_ids:
            continue
        crossing = _crossing_table_region(native, table)
        ownership_ids = set(crossing or table_ids)
        touching = [
            block
            for block in record.plan.blocks
            if set(_repair_block_span_ids(block)) & ownership_ids
        ]
        valid = (
            crossing is None
            and len(touching) == 1
            and _repair_table_matches_native(touching[0], table)
        )
        if not valid:
            invalid_orders.update(block.order for block in touching)

    if not invalid_orders:
        return list(application_blocks), record
    updated = _reject_repair_orders(record, invalid_orders)
    return (
        [block for block in application_blocks if block.order not in invalid_orders],
        updated,
    )


def _reject_repair_orders(
    record: SemanticStructureRepairRecord,
    invalid_orders: set[int],
) -> SemanticStructureRepairRecord:
    payload = record.model_dump(mode="python")
    payload.update(
        {
            "outcome": "rejected",
            "validation_codes": list(
                dict.fromkeys(
                    [*record.validation_codes, "SEMANTIC_REPAIR_TABLE_INVALID"]
                )
            ),
            "applied_orders": [
                order for order in record.applied_orders if order not in invalid_orders
            ],
            "rejected_orders": sorted({*record.rejected_orders, *invalid_orders}),
        }
    )
    return SemanticStructureRepairRecord.model_validate(payload)


def _enforce_native_tables(
    native: NativeDocument,
    blocks: list[SemanticBlock],
    excluded: list[ExcludedSpan],
    *,
    reason: str,
) -> tuple[list[SemanticBlock], list[ExcludedSpan]]:
    active = list(blocks)
    active_excluded = list(excluded)
    used_orders = {block.order for block in active}
    claimed_blank_blocks: set[int] = set()

    for table in native.tables:
        table_ids = set(_native_table_span_ids(table))
        crossing = _crossing_table_region(native, table)
        if not table_ids:
            blank_index = next(
                (
                    index
                    for index, block in enumerate(active)
                    if index not in claimed_blank_blocks
                    and isinstance(block, TableBlock)
                    and not _block_span_ids((block,))
                    and _table_block_matches_native(block, table)
                ),
                None,
            )
            if blank_index is None:
                preferred_order = _native_table_group_order(native, table.order)
                order = _available_order(preferred_order, used_orders)
                active.append(_native_table_block(table, order=order))
                used_orders.add(order)
                claimed_blank_blocks.add(len(active) - 1)
            else:
                claimed_blank_blocks.add(blank_index)
            continue

        region_ids = set(crossing or _native_table_span_ids(table))
        if any(
            isinstance(block, LosslessBlock)
            and region_ids.issubset(block.span_ids)
            for block in active
        ):
            continue
        touching_indices = {
            index
            for index, block in enumerate(active)
            if set(_block_span_ids((block,))) & region_ids
        }
        valid_indices = {
            index
            for index in touching_indices
            if crossing is None
            and isinstance(active[index], TableBlock)
            and _table_block_matches_native(active[index], table)
        }
        if len(touching_indices) == 1 and touching_indices == valid_indices:
            continue

        active = [
            block for index, block in enumerate(active) if index not in touching_indices
        ]
        active_excluded = [
            item for item in active_excluded if item.span_id not in region_ids
        ]
        used_orders = {block.order for block in active}
        ordered_region = tuple(
            span.span_id
            for span in sorted(native.spans, key=lambda item: item.ordinal)
            if span.span_id in region_ids
        )
        if ordered_region:
            order = _available_order(_native_table_group_order(native, table.order), used_orders)
            active.append(
                LosslessBlock(order=order, span_ids=ordered_region, reason=reason)
            )
            used_orders.add(order)

    return active, active_excluded


def _applied_exclusions(
    record: SemanticStructureRepairRecord,
) -> list[ExcludedSpan]:
    if record.plan is None:
        return []
    applied_orders = set(record.applied_orders)
    return [
        ExcludedSpan(span_id=span_id, kind=block.exclusion_kind)
        for block in record.plan.blocks
        if block.kind == "exclude"
        and block.order in applied_orders
        and block.exclusion_kind is not None
        for span_id in block.span_ids
    ]


def _block_span_ids(blocks: Iterable[SemanticBlock]) -> list[SpanId]:
    result: list[SpanId] = []
    for block in blocks:
        if isinstance(block, TableBlock):
            result.extend(span_id for cell in block.cells for span_id in cell.span_ids)
        else:
            result.extend(block.span_ids)
    return result


def _repair_block_span_ids(block: RepairBlock) -> tuple[SpanId, ...]:
    if block.kind == "table":
        return tuple(span_id for cell in block.cells for span_id in cell.span_ids)
    return tuple(block.span_ids)


def _native_table_span_ids(table: NativeTable) -> tuple[SpanId, ...]:
    return tuple(dict.fromkeys(span_id for cell in table.cells for span_id in cell.span_ids))


def _repair_table_matches_native(block: RepairBlock, table: NativeTable) -> bool:
    if (
        block.kind != "table"
        or block.row_count != table.row_count
        or block.column_count != table.column_count
        or len(block.cells) != len(table.cells)
    ):
        return False
    return all(
        (
            repair.start_row,
            repair.end_row,
            repair.start_column,
            repair.end_column,
            tuple(repair.span_ids),
            repair.column_header,
        )
        == (
            native.start_row,
            native.end_row,
            native.start_column,
            native.end_column,
            native.span_ids,
            native.column_header,
        )
        for repair, native in zip(block.cells, table.cells, strict=True)
    )


def _table_block_matches_native(block: TableBlock, table: NativeTable) -> bool:
    if (
        block.row_count != table.row_count
        or block.column_count != table.column_count
        or len(block.cells) != len(table.cells)
    ):
        return False
    return all(
        (
            semantic.start_row,
            semantic.end_row,
            semantic.start_column,
            semantic.end_column,
            semantic.span_ids,
            semantic.column_header,
        )
        == (
            native.start_row,
            native.end_row,
            native.start_column,
            native.end_column,
            native.span_ids,
            native.column_header,
        )
        for semantic, native in zip(block.cells, table.cells, strict=True)
    )


def _native_table_block(table: NativeTable, *, order: int) -> TableBlock:
    return TableBlock(
        order=order,
        row_count=table.row_count,
        column_count=table.column_count,
        cells=tuple(
            TableCellBlock(
                start_row=cell.start_row,
                end_row=cell.end_row,
                start_column=cell.start_column,
                end_column=cell.end_column,
                span_ids=cell.span_ids,
                column_header=cell.column_header,
            )
            for cell in table.cells
        ),
    )


def _crossing_table_region(
    native: NativeDocument,
    table: NativeTable,
) -> tuple[SpanId, ...] | None:
    catalog = native.span_catalog()
    table_ids = set(_native_table_span_ids(table))
    if not table_ids:
        return None
    ordinals = [catalog[span_id].ordinal for span_id in table_ids]
    first, last = min(ordinals), max(ordinals)
    region = tuple(
        span.span_id
        for span in sorted(native.spans, key=lambda item: item.ordinal)
        if first <= span.ordinal <= last
    )
    return region if set(region) != table_ids else None


def _native_table_group_order(native: NativeDocument, table_order: int) -> int:
    return next(
        (
            group.order
            for group in native.groups
            if group.kind == "table" and group.table_index == table_order
        ),
        table_order,
    )


def _ordinary_paragraphs(
    native: NativeDocument,
    *,
    remaining_ids: set[SpanId],
    native_table_ids: set[SpanId],
) -> list[tuple[int, tuple[SpanId, ...]]]:
    catalog = native.span_catalog()
    paragraphs: list[tuple[int, tuple[SpanId, ...]]] = []
    grouped_ids: set[SpanId] = set()
    for group in sorted(native.groups, key=lambda item: item.order):
        if group.kind == "table":
            continue
        group_spans = [
            catalog[span_id]
            for span_id in group.span_ids
            if span_id in remaining_ids and span_id not in native_table_ids
        ]
        for span_ids in _contiguous_span_ids(group_spans):
            paragraphs.append((group.order, span_ids))
            grouped_ids.update(span_ids)

    orphan_spans = [
        span
        for span in sorted(native.spans, key=lambda item: item.ordinal)
        if span.span_id in remaining_ids
        and span.span_id not in native_table_ids
        and span.span_id not in grouped_ids
    ]
    paragraphs.extend(
        (catalog[span_ids[0]].ordinal, span_ids)
        for span_ids in _contiguous_span_ids(orphan_spans)
    )
    return paragraphs


def _available_order(preferred: int, used_orders: set[int]) -> int:
    if preferred not in used_orders:
        return preferred
    candidate = max(used_orders, default=-1) + 1
    while candidate in used_orders:
        candidate += 1
    return candidate


def _completed_document_and_repair(
    native: NativeDocument,
    blocks: list[SemanticBlock],
    excluded: list[ExcludedSpan],
    repair_record: SemanticStructureRepairRecord | None,
) -> tuple[ReconciledDocument, SemanticStructureRepairRecord | None]:
    catalog = native.span_catalog()

    def semantic_source_key(block: SemanticBlock) -> tuple[int, int]:
        span_ids = _block_span_ids((block,))
        first_ordinal = min(
            (catalog[span_id].ordinal for span_id in span_ids),
            default=block.order,
        )
        return first_ordinal, block.order

    plan = repair_record.plan if repair_record is not None else None
    paired_plan_to_final: dict[int, int] = {}
    claimed_final: set[int] = set()
    if plan is not None:
        applied_orders = set(repair_record.applied_orders)
        for plan_index, repair_block in enumerate(plan.blocks):
            if repair_block.order not in applied_orders or repair_block.kind == "exclude":
                continue
            repair_ids = _repair_block_span_ids(repair_block)
            final_index = next(
                (
                    index
                    for index, semantic in enumerate(blocks)
                    if index not in claimed_final
                    and semantic.order == repair_block.order
                    and semantic.kind == repair_block.kind
                    and tuple(_block_span_ids((semantic,))) == repair_ids
                ),
                None,
            )
            if final_index is not None:
                paired_plan_to_final[plan_index] = final_index
                claimed_final.add(final_index)

    entries: list[tuple[str, int]] = [
        ("final", index) for index in range(len(blocks))
    ]
    if plan is not None:
        entries.extend(
            ("plan", index)
            for index in range(len(plan.blocks))
            if index not in paired_plan_to_final
        )

    def entry_source_key(entry: tuple[str, int]) -> tuple[int, int, int]:
        kind, index = entry
        if kind == "final":
            first, original_order = semantic_source_key(blocks[index])
            return first, original_order, 0
        assert plan is not None
        repair_block = plan.blocks[index]
        span_ids = _repair_block_span_ids(repair_block)
        first = min(
            (catalog[span_id].ordinal for span_id in span_ids),
            default=repair_block.order,
        )
        return first, repair_block.order, 1

    ordered_entries = sorted(entries, key=entry_source_key)
    entry_orders = {entry: order for order, entry in enumerate(ordered_entries)}
    normalized = tuple(
        block.model_copy(update={"order": entry_orders[("final", index)]})
        for index, block in sorted(
            enumerate(blocks),
            key=lambda item: entry_orders[("final", item[0])],
        )
    )
    document = ReconciledDocument(blocks=normalized, excluded_spans=tuple(excluded))
    if repair_record is None or plan is None:
        return document, repair_record

    plan_order_by_old: dict[int, int] = {}
    remapped_blocks: list[RepairBlock] = []
    for plan_index, repair_block in enumerate(plan.blocks):
        final_index = paired_plan_to_final.get(plan_index)
        entry = (
            ("final", final_index)
            if final_index is not None
            else ("plan", plan_index)
        )
        remapped_order = entry_orders[entry]
        plan_order_by_old[repair_block.order] = remapped_order
        remapped_blocks.append(repair_block.model_copy(update={"order": remapped_order}))
    remapped_plan = RepairPlan(
        blocks=sorted(remapped_blocks, key=lambda item: item.order)
    )
    payload = repair_record.model_dump(mode="python")
    payload.update(
        {
            "plan": remapped_plan,
            "applied_orders": sorted(
                plan_order_by_old[order] for order in repair_record.applied_orders
            ),
            "rejected_orders": sorted(
                plan_order_by_old[order] for order in repair_record.rejected_orders
            ),
            "plan_hash": canonical_hash(remapped_plan.model_dump(mode="json")),
        }
    )
    return document, SemanticStructureRepairRecord.model_validate(payload)


def _contiguous_span_ids(spans: list[Any]) -> list[tuple[SpanId, ...]]:
    groups: list[list[Any]] = []
    for span in spans:
        if not groups or span.ordinal != groups[-1][-1].ordinal + 1:
            groups.append([span])
        else:
            groups[-1].append(span)
    return [tuple(span.span_id for span in group) for group in groups]


def _build_fidelity(
    native: NativeDocument,
    document: ReconciledDocument,
    coverage: CoverageResult,
    repair_record: SemanticStructureRepairRecord | None,
    *,
    correction_audits: tuple[OcrCorrectionPageAudit, ...] = (),
    correction_warnings: tuple[str, ...] = (),
) -> SemanticFidelityReport:
    catalog = native.span_catalog()
    degraded = [block for block in document.blocks if isinstance(block, LosslessBlock)]
    table_results = _table_fidelity_results(native, document, repair_record)
    return SemanticFidelityReport(
        source_hash=native.source_hash,
        extraction_mode=native.extraction_mode,
        page_count=native.page_count,
        parser_versions=dict(native.parser_versions),
        status=(
            "WARN"
            if degraded
            or correction_warnings
            or (native.extraction_mode is ExtractionMode.OCR and not correction_audits)
            else "PASS"
        ),
        heading_count=sum(isinstance(block, HeadingBlock) for block in document.blocks),
        paragraph_count=sum(isinstance(block, ParagraphBlock) for block in document.blocks),
        list_item_count=sum(isinstance(block, ListItemBlock) for block in document.blocks),
        table_count=len(table_results),
        row_count=sum(table.row_count for table in table_results),
        cell_count=sum(table.total_cell_count for table in table_results),
        source_span_count=coverage.source_span_count,
        preserved_span_count=coverage.preserved_span_count,
        excluded_span_count=coverage.excluded_span_count,
        unmatched_span_count=coverage.unmatched_span_count,
        duplicated_span_count=coverage.duplicated_span_count,
        degraded_block_count=len(degraded),
        source_text_coverage=coverage.source_text_coverage,
        removed_elements=[
            RemovedElementAudit(
                kind=item.kind,
                page=catalog[item.span_id].page,
                bbox=catalog[item.span_id].bbox,
                text_hash=catalog[item.span_id].text_hash,
            )
            for item in document.excluded_spans
            if catalog[item.span_id].page is not None
        ],
        degraded_blocks=[
            DegradedBlockAudit(
                order=block.order,
                reason=block.reason,
                spans=[
                    SpanProvenanceAudit(
                        page=catalog[span_id].page,
                        bbox=catalog[span_id].bbox,
                        text_hash=catalog[span_id].text_hash,
                    )
                    for span_id in block.span_ids
                ],
            )
            for block in degraded
        ],
        table_results=table_results,
        ocr_corrections=list(correction_audits),
        ocr_correction_applied_count=sum(
            patch.outcome in {"applied", "reused"}
            for page in correction_audits
            for patch in page.patches
        ),
        ocr_correction_rejected_count=sum(
            patch.outcome == "rejected"
            for page in correction_audits
            for patch in page.patches
        ),
        warning_codes=list(dict.fromkeys(correction_warnings)),
    )


def _table_fidelity_results(
    native: NativeDocument,
    document: ReconciledDocument,
    repair_record: SemanticStructureRepairRecord | None,
) -> list[TableFidelityResult]:
    repaired_orders = set(repair_record.applied_orders) if repair_record is not None else set()
    results: list[TableFidelityResult] = []
    represented_native_tables: set[int] = set()
    table_blocks = [block for block in document.blocks if isinstance(block, TableBlock)]
    lossless = [block for block in document.blocks if isinstance(block, LosslessBlock)]

    for block in table_blocks:
        area = block.row_count * block.column_count
        results.append(
            TableFidelityResult(
                order=block.order,
                row_count=block.row_count,
                column_count=block.column_count,
                matched_cell_count=area,
                total_cell_count=area,
                status="repaired" if block.order in repaired_orders else "resolved",
            )
        )
        block_ids = set(_block_span_ids((block,)))
        for table in native.tables:
            if block_ids == {
                span_id for cell in table.cells for span_id in cell.span_ids
            }:
                represented_native_tables.add(table.order)

    for table in native.tables:
        if table.order in represented_native_tables:
            continue
        table_ids = set(_native_table_span_ids(table))
        degraded_block = next(
            (
                block
                for block in lossless
                if table_ids and table_ids.issubset(block.span_ids)
            ),
            None,
        )
        if degraded_block is None:
            continue
        area = table.row_count * table.column_count
        results.append(
            TableFidelityResult(
                order=degraded_block.order,
                row_count=table.row_count,
                column_count=table.column_count,
                matched_cell_count=area,
                total_cell_count=area,
                status="degraded",
            )
        )
    return sorted(results, key=lambda item: item.order)


def _semantic_evidence(
    source: SourceFile,
    native: NativeDocument,
    document: ReconciledDocument,
) -> list[Evidence]:
    from ard_ossie.docling_parser import Evidence

    published_ids = set(_block_span_ids(document.blocks))
    evidence: list[Evidence] = []
    for span in sorted(native.spans, key=lambda item: item.ordinal):
        if span.span_id not in published_ids:
            continue
        locator: dict[str, Any] = {
            "document": source.relative_path,
            "span_id": span.span_id,
            "order": span.ordinal,
        }
        if span.page is not None:
            locator["page"] = span.page
        if span.bbox is not None:
            locator["bbox"] = span.bbox.model_dump(mode="json")
        evidence.append(
            Evidence(
                source_hash=source.sha256,
                role=source.role,
                locator=locator,
                excerpt=span.text[:500],
            )
        )
    return evidence
