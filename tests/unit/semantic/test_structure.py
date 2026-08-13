from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import SimpleNamespace

import ard_ossie.semantic.structure as semantic_structure
from ard_ossie.semantic.models import (
    ExtractionMode,
    HeadingBlock,
    NativeDocument,
    NativeGroup,
    NativeTable,
    NativeTableCell,
    SourceBox,
    SourceSpan,
    TableBlock,
    make_span_id,
)
from ard_ossie.semantic.structure import (
    StructureBlock,
    StructureCell,
    StructureDocument,
    StructureTable,
    _score_candidate,
    build_docling_skeleton,
    reconcile_structure,
)

SOURCE_HASH = "a" * 64


@dataclass
class FakeBox:
    l: float  # noqa: E741 - mirrors Docling's public BoundingBox field
    b: float
    r: float
    t: float

    def to_bottom_left_origin(self, page_height: float) -> FakeBox:
        return FakeBox(self.l, page_height - self.t, self.r, page_height - self.b)


class TitleItem:
    def __init__(self, text: str, prov: list[object]) -> None:
        self.orig = text
        self.text = f"hint:{text}"
        self.prov = prov


class SectionHeaderItem(TitleItem):
    def __init__(self, text: str, prov: list[object], level: int) -> None:
        super().__init__(text, prov)
        self.level = level


class ListItem(TitleItem):
    def __init__(self, text: str, prov: list[object], *, enumerated: bool) -> None:
        super().__init__(text, prov)
        self.enumerated = enumerated
        self.marker = "1." if enumerated else "-"


class TextItem(TitleItem):
    pass


class TableItem:
    def __init__(self, cells: list[object], prov: list[object]) -> None:
        self.orig = "table hint"
        self.text = "table hint"
        self.prov = prov
        self.data = SimpleNamespace(num_rows=1, num_cols=2, table_cells=cells)


def provenance(page: int, box: tuple[float, float, float, float]) -> object:
    return SimpleNamespace(page_no=page, bbox=FakeBox(*box))


def box(values: tuple[float, float, float, float]) -> SourceBox:
    return SourceBox(left=values[0], bottom=values[1], right=values[2], top=values[3])


def span(
    ordinal: int,
    text: str,
    *,
    page: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> SourceSpan:
    return SourceSpan(
        span_id=make_span_id(SOURCE_HASH, ordinal),
        ordinal=ordinal,
        page=page,
        bbox=box(bbox) if bbox is not None else None,
        text=text,
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def native_document(
    spans: list[SourceSpan],
    *,
    mode: ExtractionMode = ExtractionMode.PDF_EMBEDDED,
    page_count: int = 1,
    groups: list[NativeGroup] | None = None,
    tables: tuple[NativeTable, ...] = (),
) -> NativeDocument:
    native_groups = groups or [
        NativeGroup(
            order=index,
            kind="paragraph",
            span_ids=(source_span.span_id,),
            page=source_span.page,
            bbox=source_span.bbox,
        )
        for index, source_span in enumerate(spans)
    ]
    return NativeDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=mode,
        page_count=page_count,
        parser_versions={},
        spans=tuple(spans),
        groups=tuple(native_groups),
        tables=tables,
    )


def structure_document(*blocks: StructureBlock) -> StructureDocument:
    return StructureDocument(blocks=blocks)


def structure_block(
    kind: str,
    order: int,
    text: str,
    *,
    page: int | None = 1,
    bbox: tuple[float, float, float, float] | None = None,
    **metadata: object,
) -> StructureBlock:
    return StructureBlock(
        kind=kind,  # type: ignore[arg-type]
        order=order,
        page=page,
        bbox=box(bbox) if bbox is not None else None,
        text_hint=text,
        **metadata,
    )


def source_text(block: object, native: NativeDocument) -> str:
    catalog = native.span_catalog()
    if isinstance(block, TableBlock):
        ids = [span_id for cell in block.cells for span_id in cell.span_ids]
    else:
        ids = list(block.span_ids)  # type: ignore[attr-defined]
    return "".join(catalog[span_id].text for span_id in ids)


def test_docling_skeleton_preserves_order_depth_and_normalizes_bottomleft_boxes() -> None:
    prov = [provenance(1, (10.0, 10.0, 90.0, 20.0))]
    duplicate_cell = SimpleNamespace(
        start_row_offset_idx=0,
        end_row_offset_idx=1,
        start_col_offset_idx=0,
        end_col_offset_idx=1,
        text="가",
        column_header=True,
        bbox=FakeBox(10.0, 30.0, 50.0, 50.0),
    )
    second_cell = SimpleNamespace(
        start_row_offset_idx=0,
        end_row_offset_idx=1,
        start_col_offset_idx=1,
        end_col_offset_idx=2,
        text="나",
        column_header=False,
        bbox=FakeBox(50.0, 30.0, 90.0, 50.0),
    )
    items = [
        (TitleItem("제목", prov), 1),
        (SectionHeaderItem("절", prov, 99), 1),
        (ListItem("항목", prov, enumerated=True), 3),
        (TextItem("본문", prov), 1),
        (TableItem([duplicate_cell, duplicate_cell, second_cell], prov), 1),
    ]
    document = SimpleNamespace(
        pages={1: SimpleNamespace(size=SimpleNamespace(width=100.0, height=100.0))},
        iterate_items=lambda: iter(items),
    )

    skeleton = build_docling_skeleton(document)

    assert [block.kind for block in skeleton.blocks] == [
        "heading",
        "heading",
        "list_item",
        "paragraph",
        "table",
    ]
    assert [block.order for block in skeleton.blocks] == list(range(5))
    assert skeleton.blocks[0].heading_level == 1
    assert skeleton.blocks[1].heading_level == 6
    assert skeleton.blocks[2].list_kind == "ordered"
    assert skeleton.blocks[2].list_depth == 2
    assert skeleton.blocks[0].bbox == box((0.1, 0.8, 0.9, 0.9))
    assert skeleton.blocks[4].table is not None
    assert len(skeleton.blocks[4].table.cells) == 2
    assert skeleton.blocks[4].table.cells[0].bbox == box((0.1, 0.5, 0.5, 0.7))


def test_reconciler_uses_docling_structure_but_exact_native_text() -> None:
    native = native_document(
        [
            span(0, "개인정보", page=1, bbox=(0.1, 0.7, 0.9, 0.8)),
            span(1, "유효성", page=1, bbox=(0.1, 0.5, 0.9, 0.6)),
        ]
    )
    skeleton = structure_document(
        structure_block(
            "heading",
            0,
            "개 인정보",
            bbox=(0.1, 0.7, 0.9, 0.8),
            heading_level=2,
        ),
        structure_block("paragraph", 1, "유 효 성", bbox=(0.1, 0.5, 0.9, 0.6)),
    )

    result = reconcile_structure(native, skeleton)

    assert isinstance(result.blocks[0], HeadingBlock)
    assert result.blocks[0].level == 2
    assert source_text(result.blocks[0], native) == "개인정보"
    assert source_text(result.blocks[1], native) == "유효성"
    assert result.unresolved_span_ids == ()


def test_reconciler_renormalizes_missing_geometry_and_never_reuses_spans() -> None:
    native = native_document(
        [span(0, "Alpha", page=None), span(1, "Alpha", page=None)],
        mode=ExtractionMode.OCR,
    )
    skeleton = structure_document(
        structure_block("paragraph", 0, "Alpha", page=None),
        structure_block("paragraph", 1, "Alpha", page=None),
    )

    result = reconcile_structure(native, skeleton)

    assert [block.span_ids for block in result.blocks] == [
        (native.spans[0].span_id,),
        (native.spans[1].span_id,),
    ]
    assert result.unresolved_span_ids == ()


def test_reconciler_does_not_join_candidate_groups_across_known_pages() -> None:
    native = native_document(
        [
            span(0, "A", page=1, bbox=(0.1, 0.5, 0.4, 0.6)),
            span(1, "B", page=2, bbox=(0.5, 0.5, 0.9, 0.6)),
        ],
        page_count=2,
    )
    skeleton = structure_document(
        structure_block("paragraph", 0, "AB", page=1, bbox=(0.1, 0.5, 0.9, 0.6))
    )

    result = reconcile_structure(native, skeleton)

    assert native.spans[1].span_id not in {
        span_id
        for block in result.blocks
        for span_id in block.span_ids  # type: ignore[union-attr]
    }
    assert native.spans[1].span_id in result.unresolved_span_ids


def test_candidate_score_rejects_known_page_mismatch_without_bounding_boxes() -> None:
    assert (
        _score_candidate(
            text="same",
            text_hint="same",
            candidate_order=0,
            structure_order=0,
            page=1,
            bbox=None,
            structure_page=2,
            structure_bbox=None,
        )
        is None
    )


def test_docling_skeleton_uses_only_first_page_provenance_for_multipage_item() -> None:
    document = SimpleNamespace(
        pages={
            1: SimpleNamespace(size=SimpleNamespace(width=100.0, height=100.0)),
            2: SimpleNamespace(size=SimpleNamespace(width=200.0, height=200.0)),
        },
        iterate_items=lambda: iter(
            [
                (
                    TextItem(
                        "continued",
                        [
                            provenance(1, (10.0, 10.0, 90.0, 20.0)),
                            provenance(2, (20.0, 20.0, 180.0, 40.0)),
                        ],
                    ),
                    1,
                )
            ]
        ),
    )

    block = build_docling_skeleton(document).blocks[0]

    assert block.page == 1
    assert block.bbox == box((0.1, 0.8, 0.9, 0.9))


def test_reconciler_rejects_duplicate_span_id_within_candidate_group() -> None:
    source_span = span(0, "A", page=None)
    native = NativeDocument.model_construct(
        source_hash=SOURCE_HASH,
        extraction_mode=ExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={},
        spans=(source_span,),
        groups=(
            NativeGroup(
                order=0,
                kind="paragraph",
                span_ids=(source_span.span_id, source_span.span_id),
            ),
        ),
        tables=(),
    )

    result = reconcile_structure(
        native,
        structure_document(structure_block("paragraph", 0, "AA", page=None)),
    )

    assert result.blocks == ()
    assert result.unresolved_span_ids == (source_span.span_id,)


def test_reconciler_rejects_duplicate_span_id_across_candidate_groups() -> None:
    source_span = span(0, "A", page=None)
    native = NativeDocument.model_construct(
        source_hash=SOURCE_HASH,
        extraction_mode=ExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={},
        spans=(source_span,),
        groups=(
            NativeGroup(order=0, kind="paragraph", span_ids=(source_span.span_id,)),
            NativeGroup(order=1, kind="paragraph", span_ids=(source_span.span_id,)),
        ),
        tables=(),
    )

    result = reconcile_structure(
        native,
        structure_document(structure_block("paragraph", 0, "AA", page=None)),
    )

    assert result.blocks
    assert result.blocks[0].span_ids == (source_span.span_id,)  # type: ignore[union-attr]
    assert len(result.blocks[0].span_ids) == len(set(result.blocks[0].span_ids))  # type: ignore[union-attr]


def test_docx_auxiliary_group_cannot_steal_matching_body_paragraph() -> None:
    spans = [span(0, "same"), span(1, "same")]
    native = native_document(
        spans,
        mode=ExtractionMode.DOCX_XML,
        page_count=0,
        groups=[
            NativeGroup(order=0, kind="alt_text", span_ids=(spans[0].span_id,)),
            NativeGroup(order=1, kind="paragraph", span_ids=(spans[1].span_id,)),
        ],
    )

    result = reconcile_structure(
        native,
        structure_document(structure_block("paragraph", 0, "same", page=None)),
    )

    assert result.blocks[0].span_ids == (spans[1].span_id,)  # type: ignore[union-attr]
    assert result.unresolved_span_ids == (spans[0].span_id,)


def test_whitespace_only_hint_cannot_match_nonempty_native_text_without_geometry() -> None:
    native = native_document([span(0, "content", page=None)])

    result = reconcile_structure(
        native,
        structure_document(structure_block("paragraph", 0, " \n\t", page=None)),
    )

    assert result.blocks == ()
    assert result.unresolved_span_ids == (native.spans[0].span_id,)


def test_docx_native_table_grid_and_merges_take_precedence_over_docling_grid() -> None:
    spans = [span(0, "머리"), span(1, "값")]
    native_table = NativeTable(
        order=0,
        row_count=2,
        column_count=2,
        cells=(
            NativeTableCell(
                start_row=0,
                end_row=1,
                start_column=0,
                end_column=2,
                span_ids=(spans[0].span_id,),
                column_header=True,
            ),
            NativeTableCell(
                start_row=1,
                end_row=2,
                start_column=0,
                end_column=2,
                span_ids=(spans[1].span_id,),
            ),
        ),
    )
    native = native_document(
        spans,
        mode=ExtractionMode.DOCX_XML,
        page_count=0,
        groups=[
            NativeGroup(
                order=0,
                kind="table",
                span_ids=tuple(item.span_id for item in spans),
                table_index=0,
            )
        ],
        tables=(native_table,),
    )
    docling_table = StructureTable(
        row_count=1,
        column_count=2,
        cells=(
            StructureCell(0, 1, 0, 1, "머리", True, None),
            StructureCell(0, 1, 1, 2, "값", False, None),
        ),
    )
    skeleton = structure_document(
        structure_block("table", 0, "머리 값", page=None, table=docling_table)
    )

    result = reconcile_structure(native, skeleton)

    assert len(result.blocks) == 1
    table = result.blocks[0]
    assert isinstance(table, TableBlock)
    assert (table.row_count, table.column_count) == (2, 2)
    assert (table.cells[0].start_column, table.cells[0].end_column) == (0, 2)
    assert source_text(table, native) == "머리값"
    assert result.unresolved_span_ids == ()


def test_pdf_table_requires_each_source_span_to_map_to_exactly_one_cell() -> None:
    native = native_document(
        [
            span(0, "A", page=1, bbox=(0.1, 0.5, 0.4, 0.7)),
            span(1, "B", page=1, bbox=(0.6, 0.5, 0.9, 0.7)),
        ]
    )
    table = StructureTable(
        row_count=1,
        column_count=2,
        cells=(
            StructureCell(0, 1, 0, 1, "A", False, box((0.1, 0.5, 0.5, 0.7))),
            StructureCell(0, 1, 1, 2, "B", False, box((0.5, 0.5, 0.9, 0.7))),
        ),
    )
    skeleton = structure_document(
        structure_block("table", 0, "A B", bbox=(0.1, 0.5, 0.9, 0.7), table=table)
    )

    result = reconcile_structure(native, skeleton)

    assert isinstance(result.blocks[0], TableBlock)
    assert [cell.span_ids for cell in result.blocks[0].cells] == [
        (native.spans[0].span_id,),
        (native.spans[1].span_id,),
    ]
    assert result.unresolved_span_ids == ()

    ambiguous_native = native_document([span(0, "A B", page=1, bbox=(0.4, 0.5, 0.6, 0.7))])
    ambiguous = reconcile_structure(ambiguous_native, skeleton)
    assert ambiguous.blocks == ()
    assert ambiguous.unresolved_span_ids == (ambiguous_native.spans[0].span_id,)


def test_pdf_table_stays_unresolved_when_a_table_group_span_has_no_geometry() -> None:
    spans = [
        span(0, "A", page=1, bbox=(0.1, 0.5, 0.4, 0.7)),
        span(1, "B", page=1),
    ]
    native = native_document(
        spans,
        groups=[
            NativeGroup(
                order=0,
                kind="paragraph",
                span_ids=tuple(item.span_id for item in spans),
                page=1,
                bbox=box((0.1, 0.5, 0.9, 0.7)),
            )
        ],
    )
    table = StructureTable(
        row_count=1,
        column_count=2,
        cells=(
            StructureCell(0, 1, 0, 1, "A", False, box((0.1, 0.5, 0.5, 0.7))),
            StructureCell(0, 1, 1, 2, "B", False, box((0.5, 0.5, 0.9, 0.7))),
        ),
    )
    skeleton = structure_document(
        structure_block("table", 0, "A B", bbox=(0.1, 0.5, 0.9, 0.7), table=table)
    )

    result = reconcile_structure(native, skeleton)

    assert result.blocks == ()
    assert result.unresolved_span_ids == tuple(item.span_id for item in spans)


def test_pdf_table_maps_ungrouped_same_page_span_intersecting_a_cell() -> None:
    spans = [
        span(0, "A", page=1, bbox=(0.1, 0.5, 0.4, 0.7)),
        span(1, "B", page=1, bbox=(0.6, 0.5, 0.9, 0.7)),
    ]
    native = native_document(
        spans,
        groups=[
            NativeGroup(
                order=0,
                kind="paragraph",
                span_ids=(spans[0].span_id,),
                page=1,
                bbox=spans[0].bbox,
            )
        ],
    )
    table = StructureTable(
        row_count=1,
        column_count=2,
        cells=(
            StructureCell(0, 1, 0, 1, "A", False, box((0.1, 0.5, 0.5, 0.7))),
            StructureCell(0, 1, 1, 2, "B", False, box((0.5, 0.5, 0.9, 0.7))),
        ),
    )

    result = reconcile_structure(
        native,
        structure_document(
            structure_block("table", 0, "A B", bbox=(0.1, 0.5, 0.9, 0.7), table=table)
        ),
    )

    assert isinstance(result.blocks[0], TableBlock)
    assert [cell.span_ids for cell in result.blocks[0].cells] == [
        (spans[0].span_id,),
        (spans[1].span_id,),
    ]
    assert result.unresolved_span_ids == ()


def test_pdf_table_scans_cell_geometry_outside_the_outer_table_box() -> None:
    spans = [
        span(0, "A", page=1, bbox=(0.1, 0.5, 0.4, 0.7)),
        span(1, "B", page=1, bbox=(0.6, 0.5, 0.9, 0.7)),
    ]
    native = native_document(
        spans,
        groups=[
            NativeGroup(
                order=0,
                kind="paragraph",
                span_ids=(spans[0].span_id,),
                page=1,
                bbox=spans[0].bbox,
            )
        ],
    )
    table = StructureTable(
        row_count=1,
        column_count=2,
        cells=(
            StructureCell(0, 1, 0, 1, "A", False, box((0.1, 0.5, 0.5, 0.7))),
            StructureCell(0, 1, 1, 2, "B", False, box((0.5, 0.5, 0.9, 0.7))),
        ),
    )

    result = reconcile_structure(
        native,
        structure_document(
            structure_block("table", 0, "A B", bbox=(0.1, 0.5, 0.5, 0.7), table=table)
        ),
    )

    assert isinstance(result.blocks[0], TableBlock)
    assert [cell.span_ids for cell in result.blocks[0].cells] == [
        (spans[0].span_id,),
        (spans[1].span_id,),
    ]


def test_pdf_table_mapping_overlap_checks_are_subquadratic(
    monkeypatch,
) -> None:
    real_overlap = semantic_structure._boxes_overlap
    overlap_calls = 0

    def counted_overlap(first: SourceBox, second: SourceBox) -> bool:
        nonlocal overlap_calls
        overlap_calls += 1
        return real_overlap(first, second)

    monkeypatch.setattr(semantic_structure, "_boxes_overlap", counted_overlap)

    def reconcile_grid(cell_count: int) -> int:
        before = overlap_calls
        columns = 16
        rows = cell_count // columns
        spans: list[SourceSpan] = []
        cells: list[StructureCell] = []
        for index in range(cell_count):
            row, column = divmod(index, columns)
            left = column / columns
            right = (column + 1) / columns
            bottom = row / rows
            top = (row + 1) / rows
            inset_x = (right - left) / 10
            inset_y = (top - bottom) / 10
            spans.append(
                span(
                    index,
                    "x",
                    page=1,
                    bbox=(
                        left + inset_x,
                        bottom + inset_y,
                        right - inset_x,
                        top - inset_y,
                    ),
                )
            )
            cells.append(
                StructureCell(
                    row,
                    row + 1,
                    column,
                    column + 1,
                    "x",
                    False,
                    box((left, bottom, right, top)),
                )
            )
        native = native_document(spans)
        table = StructureTable(row_count=rows, column_count=columns, cells=tuple(cells))
        result = reconcile_structure(
            native,
            structure_document(
                structure_block(
                    "table",
                    0,
                    "x" * cell_count,
                    bbox=(0.0, 0.0, 1.0, 1.0),
                    table=table,
                )
            ),
        )
        assert isinstance(result.blocks[0], TableBlock)
        return overlap_calls - before

    smaller = reconcile_grid(64)
    larger = reconcile_grid(128)

    assert larger <= 3 * smaller + 64
    assert larger <= 64 * 128


def test_pdf_table_overlap_index_is_subquadratic_for_adversarial_gaps(
    monkeypatch,
) -> None:
    real_probe = semantic_structure._interval_subtree_may_overlap
    probe_calls = 0

    def counted_probe(
        node: semantic_structure._IntervalNode,
        bottom: float,
        top: float,
    ) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return real_probe(node, bottom, top)

    monkeypatch.setattr(
        semantic_structure,
        "_interval_subtree_may_overlap",
        counted_probe,
    )

    def reconcile_gap(cell_count: int) -> int:
        before = probe_calls
        spans = [
            span(index, "x", page=1, bbox=(0.01, 0.49, 0.99, 0.51))
            for index in range(cell_count)
        ]
        cells = tuple(
            StructureCell(
                0,
                1,
                index,
                index + 1,
                "x",
                False,
                box(
                    (
                        index / cell_count,
                        0.0 if index % 2 == 0 else 0.6,
                        (index + 0.8) / cell_count,
                        0.4 if index % 2 == 0 else 1.0,
                    )
                ),
            )
            for index in range(cell_count)
        )
        reconcile_structure(
            native_document(spans),
            structure_document(
                structure_block(
                    "table",
                    0,
                    "x" * cell_count,
                    bbox=(0.0, 0.0, 1.0, 1.0),
                    table=StructureTable(
                        row_count=1,
                        column_count=cell_count,
                        cells=cells,
                    ),
                )
            ),
        )
        return probe_calls - before

    smaller = reconcile_gap(64)
    larger = reconcile_gap(128)

    assert larger <= 3 * smaller + 128
    assert larger <= 64 * 128


def repeated_fixture() -> tuple[NativeDocument, StructureDocument]:
    spans: list[SourceSpan] = []
    hints: list[StructureBlock] = []
    for page in range(1, 4):
        values = [
            ("Policy", (0.1, 0.92, 0.9, 0.98)),
            ("Repeated body", (0.1, 0.45, 0.9, 0.55)),
            ("Confidential", (0.1, 0.02, 0.7, 0.08)),
            (str(page), (0.8, 0.02, 0.9, 0.08)),
        ]
        for text, bbox in values:
            ordinal = len(spans)
            spans.append(span(ordinal, text, page=page, bbox=bbox))
            hints.append(structure_block("paragraph", ordinal, text, page=page, bbox=bbox))
    return native_document(spans, page_count=3), structure_document(*hints)


def test_repeated_edges_and_variable_page_numbers_are_excluded_after_matching() -> None:
    native, skeleton = repeated_fixture()

    result = reconcile_structure(native, skeleton)

    assert [item.kind for item in result.excluded_spans].count("page_header") == 3
    assert [item.kind for item in result.excluded_spans].count("page_footer") == 3
    assert [item.kind for item in result.excluded_spans].count("page_number") == 3
    body_ids = {native.spans[index].span_id for index in (1, 5, 9)}
    excluded_ids = {item.span_id for item in result.excluded_spans}
    assert body_ids.isdisjoint(excluded_ids)
    assert body_ids.issubset(
        {span_id for block in result.blocks for span_id in block.span_ids}  # type: ignore[union-attr]
    )
    assert result.unresolved_span_ids == ()


def test_edge_candidate_overlapping_another_assigned_body_box_is_retained() -> None:
    spans: list[SourceSpan] = []
    hints: list[StructureBlock] = []
    for page in range(1, 4):
        for text, bbox in (
            ("Policy", (0.1, 0.92, 0.9, 0.98)),
            (f"Body {page}", (0.2, 0.91, 0.8, 0.99)),
        ):
            ordinal = len(spans)
            spans.append(span(ordinal, text, page=page, bbox=bbox))
            hints.append(structure_block("paragraph", ordinal, text, page=page, bbox=bbox))
    native = native_document(spans, page_count=3)

    result = reconcile_structure(native, structure_document(*hints))

    assert result.excluded_spans == ()
    assert len(result.blocks) == 6
