"""Docling structure hints reconciled against source-native text spans."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

from docling_core.types.doc import (
    ListItem,
    SectionHeaderItem,
    TableItem,
    TextItem,
    TitleItem,
)

from ard_ossie.semantic.models import (
    MAX_LIST_DEPTH,
    MAX_TABLE_CELLS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_GRID_AREA,
    MAX_TABLE_ROWS,
    ExcludedSpan,
    ExtractionMode,
    HeadingBlock,
    ListItemBlock,
    NativeDocument,
    NativeGroup,
    ParagraphBlock,
    SemanticBlock,
    SourceBox,
    SpanId,
    TableBlock,
    TableCellBlock,
)

OVERLAP_WEIGHT = 0.55
TEXT_SIMILARITY_WEIGHT = 0.35
ORDER_WEIGHT = 0.10
ACCEPTANCE_SCORE = 0.72
PAGE_EDGE_BAND = 0.10
REPEAT_RATIO = 0.60

_PAGE_NUMBER = re.compile(r"\s*\d+\s*(?:/\s*\d+\s*)?")


@dataclass(frozen=True)
class StructureCell:
    start_row: int
    end_row: int
    start_column: int
    end_column: int
    text_hint: str
    column_header: bool
    bbox: SourceBox | None


@dataclass(frozen=True)
class StructureTable:
    row_count: int
    column_count: int
    cells: tuple[StructureCell, ...]


@dataclass(frozen=True)
class StructureBlock:
    kind: Literal["heading", "paragraph", "list_item", "table"]
    order: int
    page: int | None
    bbox: SourceBox | None
    text_hint: str
    heading_level: int | None = None
    list_kind: Literal["ordered", "unordered"] | None = None
    list_depth: int | None = None
    table: StructureTable | None = None


@dataclass(frozen=True)
class StructureDocument:
    blocks: tuple[StructureBlock, ...]


@dataclass(frozen=True)
class ReconciliationResult:
    blocks: tuple[SemanticBlock, ...]
    unresolved_span_ids: tuple[SpanId, ...]
    excluded_spans: tuple[ExcludedSpan, ...]


@dataclass(frozen=True)
class _AssignedBlock:
    semantic: SemanticBlock
    span_ids: tuple[SpanId, ...]
    page: int | None
    bbox: SourceBox | None
    source_boxes: tuple[SourceBox, ...]


@dataclass(frozen=True)
class _GroupMatch:
    first_index: int
    last_index: int
    groups: tuple[NativeGroup, ...]
    span_ids: tuple[SpanId, ...]
    page: int | None
    bbox: SourceBox | None
    score: float


@dataclass
class _IntervalNode:
    key: tuple[float, int]
    top: float
    index: int
    left: _IntervalNode | None = None
    right: _IntervalNode | None = None
    height: int = 1
    minimum_bottom: float = 0.0
    maximum_top: float = 0.0

    def __post_init__(self) -> None:
        self.minimum_bottom = self.key[0]
        self.maximum_top = self.top


def _bounded_rectangle_matches(
    items: list[tuple[int, SourceBox]],
    queries: list[tuple[int, SourceBox]],
    *,
    limit: int,
) -> dict[int, tuple[int, ...]]:
    """Report at most ``limit`` intersections per query in bounded sweep-line work."""
    matches: dict[int, list[int]] = {index: [] for index, _box in queries}
    if limit <= 0:
        return {index: () for index in matches}

    degenerate_items: dict[tuple[float, float, float, float], list[int]] = {}
    events: list[tuple[float, int, int, SourceBox]] = []
    for index, box in items:
        if _box_is_degenerate(box):
            degenerate_items.setdefault(_box_coordinates(box), []).append(index)
            continue
        events.extend(((box.right, 0, index, box), (box.left, 2, index, box)))
    for index, box in queries:
        if _box_is_degenerate(box):
            matches[index].extend(degenerate_items.get(_box_coordinates(box), ())[:limit])
            continue
        events.extend(((box.right, 1, index, box), (box.left, 3, index, box)))

    active_items: _IntervalNode | None = None
    active_queries: _IntervalNode | None = None
    item_keys: dict[int, tuple[float, int]] = {}
    query_keys: dict[int, tuple[float, int]] = {}
    for _coordinate, event_kind, index, box in sorted(
        events,
        key=lambda event: (event[0], event[1], event[2]),
    ):
        if event_kind == 0:
            key = item_keys.pop(index, None)
            if key is not None:
                active_items = _interval_delete(active_items, key)
            continue
        if event_kind == 1:
            key = query_keys.pop(index, None)
            if key is not None:
                active_queries = _interval_delete(active_queries, key)
            continue
        if event_kind == 2:
            overlapping_queries: list[int] = []
            _interval_query(
                active_queries,
                box.bottom,
                box.top,
                overlapping_queries,
                limit=None,
            )
            for query_index in overlapping_queries:
                matches[query_index].append(index)
                if len(matches[query_index]) >= limit:
                    key = query_keys.pop(query_index)
                    active_queries = _interval_delete(active_queries, key)
            key = (box.bottom, index)
            active_items = _interval_insert(active_items, key, box.top, index)
            item_keys[index] = key
            continue

        overlapping_items: list[int] = []
        _interval_query(
            active_items,
            box.bottom,
            box.top,
            overlapping_items,
            limit=limit,
        )
        matches[index].extend(overlapping_items)
        if len(matches[index]) < limit:
            key = (box.bottom, index)
            active_queries = _interval_insert(active_queries, key, box.top, index)
            query_keys[index] = key

    return {index: tuple(values) for index, values in matches.items()}


def _box_coordinates(box: SourceBox) -> tuple[float, float, float, float]:
    return (box.left, box.bottom, box.right, box.top)


def _box_is_degenerate(box: SourceBox) -> bool:
    return box.left == box.right or box.bottom == box.top


def _interval_insert(
    node: _IntervalNode | None,
    key: tuple[float, int],
    top: float,
    index: int,
) -> _IntervalNode:
    if node is None:
        return _IntervalNode(key=key, top=top, index=index)
    if key < node.key:
        node.left = _interval_insert(node.left, key, top, index)
    else:
        node.right = _interval_insert(node.right, key, top, index)
    return _interval_balance(_interval_update(node))


def _interval_delete(
    node: _IntervalNode | None,
    key: tuple[float, int],
) -> _IntervalNode | None:
    if node is None:
        return None
    if key < node.key:
        node.left = _interval_delete(node.left, key)
    elif key > node.key:
        node.right = _interval_delete(node.right, key)
    elif node.left is None:
        return node.right
    elif node.right is None:
        return node.left
    else:
        successor = node.right
        while successor.left is not None:
            successor = successor.left
        node.key = successor.key
        node.top = successor.top
        node.index = successor.index
        node.right = _interval_delete(node.right, successor.key)
    return _interval_balance(_interval_update(node))


def _interval_update(node: _IntervalNode) -> _IntervalNode:
    node.height = 1 + max(_interval_height(node.left), _interval_height(node.right))
    node.minimum_bottom = min(
        [
            node.key[0],
            *(
                child.minimum_bottom
                for child in (node.left, node.right)
                if child is not None
            ),
        ]
    )
    node.maximum_top = max(
        [
            node.top,
            *(
                child.maximum_top
                for child in (node.left, node.right)
                if child is not None
            ),
        ]
    )
    return node


def _interval_height(node: _IntervalNode | None) -> int:
    return 0 if node is None else node.height


def _interval_balance(node: _IntervalNode) -> _IntervalNode:
    balance = _interval_height(node.left) - _interval_height(node.right)
    if balance > 1:
        assert node.left is not None
        if _interval_height(node.left.left) < _interval_height(node.left.right):
            node.left = _interval_rotate_left(node.left)
        return _interval_rotate_right(node)
    if balance < -1:
        assert node.right is not None
        if _interval_height(node.right.right) < _interval_height(node.right.left):
            node.right = _interval_rotate_right(node.right)
        return _interval_rotate_left(node)
    return node


def _interval_rotate_left(node: _IntervalNode) -> _IntervalNode:
    replacement = node.right
    assert replacement is not None
    node.right = replacement.left
    replacement.left = _interval_update(node)
    return _interval_update(replacement)


def _interval_rotate_right(node: _IntervalNode) -> _IntervalNode:
    replacement = node.left
    assert replacement is not None
    node.left = replacement.right
    replacement.right = _interval_update(node)
    return _interval_update(replacement)


def _interval_query(
    node: _IntervalNode | None,
    bottom: float,
    top: float,
    matches: list[int],
    *,
    limit: int | None,
) -> None:
    if (
        node is None
        or (limit is not None and len(matches) >= limit)
        or not _interval_subtree_may_overlap(node, bottom, top)
    ):
        return
    _interval_query(node.left, bottom, top, matches, limit=limit)
    if (limit is None or len(matches) < limit) and node.key[0] < top and node.top > bottom:
        matches.append(node.index)
    _interval_query(node.right, bottom, top, matches, limit=limit)


def _interval_subtree_may_overlap(
    node: _IntervalNode,
    bottom: float,
    top: float,
) -> bool:
    return node.minimum_bottom < top and node.maximum_top > bottom


def build_docling_skeleton(document: Any) -> StructureDocument:
    """Convert Docling items into span-free structural hints in reading order."""
    blocks: list[StructureBlock] = []
    for item, tree_level in document.iterate_items():
        item_name = item.__class__.__name__
        page, bbox = _item_location(item, document)
        text_hint = _item_text_hint(item)

        if isinstance(item, TableItem) or item_name == "TableItem":
            table = _structure_table(item, document, page)
            blocks.append(
                StructureBlock(
                    kind="table",
                    order=len(blocks),
                    page=page,
                    bbox=bbox,
                    text_hint=" ".join(cell.text_hint for cell in table.cells),
                    table=table,
                )
            )
        elif isinstance(item, TitleItem) or item_name == "TitleItem":
            blocks.append(
                StructureBlock(
                    kind="heading",
                    order=len(blocks),
                    page=page,
                    bbox=bbox,
                    text_hint=text_hint,
                    heading_level=1,
                )
            )
        elif isinstance(item, SectionHeaderItem) or item_name == "SectionHeaderItem":
            blocks.append(
                StructureBlock(
                    kind="heading",
                    order=len(blocks),
                    page=page,
                    bbox=bbox,
                    text_hint=text_hint,
                    heading_level=max(1, min(6, int(getattr(item, "level", 1)))),
                )
            )
        elif isinstance(item, ListItem) or item_name == "ListItem":
            list_depth = max(0, int(tree_level) - 1)
            if list_depth > MAX_LIST_DEPTH:
                raise ValueError("LIST_DEPTH_LIMIT_EXCEEDED")
            blocks.append(
                StructureBlock(
                    kind="list_item",
                    order=len(blocks),
                    page=page,
                    bbox=bbox,
                    text_hint=text_hint,
                    list_kind=(
                        "ordered" if bool(getattr(item, "enumerated", False)) else "unordered"
                    ),
                    list_depth=list_depth,
                )
            )
        elif isinstance(item, TextItem) or item_name == "TextItem":
            blocks.append(
                StructureBlock(
                    kind="paragraph",
                    order=len(blocks),
                    page=page,
                    bbox=bbox,
                    text_hint=text_hint,
                )
            )
    return StructureDocument(blocks=tuple(blocks))


def reconcile_structure(
    native: NativeDocument, skeleton: StructureDocument
) -> ReconciliationResult:
    """Assign native spans to structural hints without changing source text."""
    catalog = native.span_catalog()
    groups = tuple(sorted(native.groups, key=lambda group: group.order))
    allocated: set[SpanId] = set()
    assignments: list[_AssignedBlock] = []
    group_cursor = 0

    for structure in sorted(skeleton.blocks, key=lambda block: block.order):
        if structure.kind == "table":
            table_assignment = _match_table(
                native,
                groups,
                catalog,
                structure,
                group_cursor,
                allocated,
            )
            if table_assignment is None:
                continue
            assignment, group_cursor = table_assignment
        else:
            match = _match_groups(
                groups,
                catalog,
                structure,
                group_cursor,
                allocated,
                allow_group_ranges=native.extraction_mode is ExtractionMode.PDF_EMBEDDED,
            )
            if match is None:
                continue
            block_order = (
                match.groups[0].order
                if native.extraction_mode is ExtractionMode.DOCX_XML
                else structure.order
            )
            semantic = _semantic_block(structure, block_order, match.span_ids)
            assignment = _assigned_block(semantic, match.span_ids, match.page, match.bbox, catalog)
            group_cursor = match.last_index + 1

        if any(span_id in allocated for span_id in assignment.span_ids):
            continue
        allocated.update(assignment.span_ids)
        assignments.append(assignment)

    assignments, excluded = _exclude_repeated_edges(native, assignments, catalog)
    excluded_ids = {item.span_id for item in excluded}
    unresolved = tuple(
        source_span.span_id
        for source_span in sorted(native.spans, key=lambda item: item.ordinal)
        if source_span.span_id not in allocated and source_span.span_id not in excluded_ids
    )
    return ReconciliationResult(
        blocks=tuple(
            item.semantic for item in sorted(assignments, key=lambda item: item.semantic.order)
        ),
        unresolved_span_ids=unresolved,
        excluded_spans=excluded,
    )


def _item_text_hint(item: Any) -> str:
    value = getattr(item, "orig", None)
    if value is None:
        value = getattr(item, "text", "")
    return str(value)


def _item_location(item: Any, document: Any) -> tuple[int | None, SourceBox | None]:
    provenances = tuple(getattr(item, "prov", ()) or ())
    if not provenances:
        return None, None
    page = int(provenances[0].page_no)
    # A StructureBlock has one page only. For a multi-page Docling item, retain the
    # first provenance page and never combine its geometry with continuation pages.
    first_page_provenances = tuple(
        provenance for provenance in provenances if int(provenance.page_no) == page
    )
    boxes = [
        normalized
        for provenance in first_page_provenances
        if (normalized := _normalize_docling_box(provenance.bbox, document, page)) is not None
    ]
    return page, _union_boxes(boxes)


def _normalize_docling_box(bbox: Any, document: Any, page: int) -> SourceBox | None:
    try:
        page_item = document.pages[page]
        width = float(page_item.size.width)
        height = float(page_item.size.height)
    except (AttributeError, KeyError, TypeError):
        return None
    if width <= 0 or height <= 0:
        return None
    try:
        bottom_left = bbox.to_bottom_left_origin(height)
        return SourceBox(
            left=_unit(float(bottom_left.l) / width),
            bottom=_unit(float(bottom_left.b) / height),
            right=_unit(float(bottom_left.r) / width),
            top=_unit(float(bottom_left.t) / height),
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _structure_table(item: Any, document: Any, page: int | None) -> StructureTable:
    data = item.data
    row_count = int(data.num_rows)
    column_count = int(data.num_cols)
    _validate_structure_table_limits(row_count, column_count, len(data.table_cells))
    seen: set[tuple[int, int, int, int]] = set()
    cells: list[StructureCell] = []
    for cell in data.table_cells:
        key = (
            int(cell.start_row_offset_idx),
            int(cell.end_row_offset_idx),
            int(cell.start_col_offset_idx),
            int(cell.end_col_offset_idx),
        )
        if key in seen:
            continue
        seen.add(key)
        cells.append(
            StructureCell(
                start_row=key[0],
                end_row=key[1],
                start_column=key[2],
                end_column=key[3],
                text_hint=str(cell.text),
                column_header=bool(cell.column_header),
                bbox=(
                    _normalize_docling_box(cell.bbox, document, page)
                    if page is not None and cell.bbox is not None
                    else None
                ),
            )
        )
    return StructureTable(
        row_count=row_count,
        column_count=column_count,
        cells=tuple(cells),
    )


def _match_groups(
    groups: tuple[NativeGroup, ...],
    catalog: dict[SpanId, Any],
    structure: StructureBlock,
    cursor: int,
    allocated: set[SpanId],
    *,
    allow_group_ranges: bool,
) -> _GroupMatch | None:
    best: _GroupMatch | None = None
    for start in range(cursor, len(groups)):
        if not _ordinary_group_compatible(groups[start], structure):
            continue
        candidate_groups: list[NativeGroup] = []
        candidate_ids: list[SpanId] = []
        for end in range(start, len(groups)):
            if end > start and not allow_group_ranges:
                break
            group = groups[end]
            if not _ordinary_group_compatible(group, structure):
                break
            if any(span_id in allocated for span_id in group.span_ids):
                break
            candidate_groups.append(group)
            candidate_ids.extend(group.span_ids)
            if len(candidate_ids) != len(set(candidate_ids)):
                break
            location = _groups_location(tuple(candidate_groups), catalog)
            if location is None:
                break
            page, bbox = location
            candidate_text = "".join(catalog[span_id].text for span_id in candidate_ids)
            score = _score_candidate(
                text=candidate_text,
                text_hint=structure.text_hint,
                candidate_order=groups[start].order,
                structure_order=structure.order,
                page=page,
                bbox=bbox,
                structure_page=structure.page,
                structure_bbox=structure.bbox,
            )
            if score is None or score < ACCEPTANCE_SCORE:
                continue
            match = _GroupMatch(
                first_index=start,
                last_index=end,
                groups=tuple(candidate_groups),
                span_ids=tuple(candidate_ids),
                page=page,
                bbox=bbox,
                score=score,
            )
            if best is None or (-score, start, end) < (
                -best.score,
                best.first_index,
                best.last_index,
            ):
                best = match
    return best


def _ordinary_group_compatible(group: NativeGroup, structure: StructureBlock) -> bool:
    if group.kind in {"table", "alt_text", "text_box"}:
        return False
    if group.kind == "caption":
        return structure.kind == "paragraph"
    return True


def _match_table(
    native: NativeDocument,
    groups: tuple[NativeGroup, ...],
    catalog: dict[SpanId, Any],
    structure: StructureBlock,
    cursor: int,
    allocated: set[SpanId],
) -> tuple[_AssignedBlock, int] | None:
    if structure.table is None:
        return None
    if native.extraction_mode is ExtractionMode.PDF_EMBEDDED:
        return _match_pdf_table(native, groups, catalog, structure, cursor, allocated)

    best: tuple[float, int, NativeGroup] | None = None
    for index in range(cursor, len(groups)):
        group = groups[index]
        if group.kind != "table" or any(span_id in allocated for span_id in group.span_ids):
            continue
        location = _groups_location((group,), catalog)
        if location is None:
            continue
        page, bbox = location
        candidate_text = "".join(catalog[span_id].text for span_id in group.span_ids)
        score = _score_candidate(
            text=candidate_text,
            text_hint=structure.text_hint,
            candidate_order=group.order,
            structure_order=structure.order,
            page=page,
            bbox=bbox,
            structure_page=structure.page,
            structure_bbox=structure.bbox,
        )
        if score is None or score < ACCEPTANCE_SCORE:
            continue
        candidate = (score, index, group)
        if best is None or (-score, index) < (-best[0], best[1]):
            best = candidate
    if best is None:
        return None
    _, index, group = best
    if group.table_index is None or group.table_index >= len(native.tables):
        return None
    native_table = native.tables[group.table_index]
    if not native_table.cells:
        return None
    span_ids = tuple(span_id for cell in native_table.cells for span_id in cell.span_ids)
    if len(span_ids) != len(set(span_ids)) or set(span_ids) != set(group.span_ids):
        return None
    semantic = TableBlock(
        order=group.order if native.extraction_mode is ExtractionMode.DOCX_XML else structure.order,
        row_count=native_table.row_count,
        column_count=native_table.column_count,
        cells=tuple(
            TableCellBlock(
                start_row=cell.start_row,
                end_row=cell.end_row,
                start_column=cell.start_column,
                end_column=cell.end_column,
                span_ids=cell.span_ids,
                column_header=cell.column_header,
            )
            for cell in native_table.cells
        ),
    )
    location = _groups_location((group,), catalog)
    if location is None:
        return None
    page, bbox = location
    return (
        _assigned_block(
            semantic,
            span_ids,
            page,
            bbox,
            catalog,
            additional_boxes=tuple(
                cell.bbox for cell in native_table.cells if cell.bbox is not None
            ),
        ),
        index + 1,
    )


def _match_pdf_table(
    native: NativeDocument,
    groups: tuple[NativeGroup, ...],
    catalog: dict[SpanId, Any],
    structure: StructureBlock,
    cursor: int,
    allocated: set[SpanId],
) -> tuple[_AssignedBlock, int] | None:
    assert structure.table is not None
    table_bbox = structure.bbox or _union_boxes(
        [cell.bbox for cell in structure.table.cells if cell.bbox is not None]
    )
    if table_bbox is None or structure.page is None:
        return None
    table_geometry = (
        table_bbox,
        *(cell.bbox for cell in structure.table.cells if cell.bbox is not None),
    )
    geometry_items = list(enumerate(table_geometry))
    group_locations = {
        index: location[1]
        for index in range(cursor, len(groups))
        if (location := _groups_location((groups[index],), catalog)) is not None
        and location[0] == structure.page
        and location[1] is not None
    }
    group_geometry_matches = _bounded_rectangle_matches(
        geometry_items,
        list(group_locations.items()),
        limit=1,
    )
    table_group_indices = [
        index for index in group_locations if group_geometry_matches[index]
    ]
    group_candidate_ids = {
        span_id for index in table_group_indices for span_id in groups[index].span_ids
    }
    source_geometry_queries = [
        (index, source_span.bbox)
        for index, source_span in enumerate(native.spans)
        if source_span.span_id not in allocated
        and source_span.page == structure.page
        and source_span.bbox is not None
    ]
    source_geometry_matches = _bounded_rectangle_matches(
        geometry_items,
        source_geometry_queries,
        limit=1,
    )
    geometry_candidate_ids = {
        native.spans[index].span_id
        for index, _bbox in source_geometry_queries
        if source_geometry_matches[index]
    }
    candidate_ids = group_candidate_ids | geometry_candidate_ids
    table_spans = [
        source_span
        for source_span in sorted(native.spans, key=lambda item: item.ordinal)
        if source_span.span_id in candidate_ids and source_span.span_id not in allocated
    ]
    if not table_spans:
        return None

    if any(
        source_span.page != structure.page
        or source_span.bbox is None
        or source_span.span_id not in geometry_candidate_ids
        for source_span in table_spans
    ):
        return None
    cell_matches = _bounded_rectangle_matches(
        [
            (index, cell.bbox)
            for index, cell in enumerate(structure.table.cells)
            if cell.bbox is not None
        ],
        [
            (index, source_span.bbox)
            for index, source_span in enumerate(table_spans)
            if source_span.bbox is not None
        ],
        limit=2,
    )
    mapped: list[list[SpanId]] = [[] for _ in structure.table.cells]
    for index, source_span in enumerate(table_spans):
        destinations = cell_matches[index]
        if len(destinations) != 1:
            return None
        mapped[destinations[0]].append(source_span.span_id)

    ordered_candidate_ids = tuple(source_span.span_id for source_span in table_spans)
    ordered_candidate_set = set(ordered_candidate_ids)
    candidate_text = "".join(source_span.text for source_span in table_spans)
    first_order = min(
        (
            group.order
            for group in groups[cursor:]
            if set(group.span_ids) & ordered_candidate_set
        ),
        default=structure.order,
    )
    score = _score_candidate(
        text=candidate_text,
        text_hint=structure.text_hint,
        candidate_order=first_order,
        structure_order=structure.order,
        page=structure.page,
        bbox=table_bbox,
        structure_page=structure.page,
        structure_bbox=table_bbox,
    )
    if score is None or score < ACCEPTANCE_SCORE:
        return None

    semantic = TableBlock(
        order=structure.order,
        row_count=structure.table.row_count,
        column_count=structure.table.column_count,
        cells=tuple(
            TableCellBlock(
                start_row=cell.start_row,
                end_row=cell.end_row,
                start_column=cell.start_column,
                end_column=cell.end_column,
                span_ids=tuple(mapped[index]),
                column_header=cell.column_header,
            )
            for index, cell in enumerate(structure.table.cells)
        ),
    )
    next_cursor = max(table_group_indices, default=cursor - 1) + 1
    return (
        _assigned_block(
            semantic,
            ordered_candidate_ids,
            structure.page,
            table_bbox,
            catalog,
            additional_boxes=tuple(
                cell.bbox for cell in structure.table.cells if cell.bbox is not None
            ),
        ),
        next_cursor,
    )


def _semantic_block(
    structure: StructureBlock, order: int, span_ids: tuple[SpanId, ...]
) -> SemanticBlock:
    if structure.kind == "heading":
        return HeadingBlock(
            order=order,
            level=max(1, min(6, structure.heading_level or 1)),
            span_ids=span_ids,
        )
    if structure.kind == "list_item":
        return ListItemBlock(
            order=order,
            list_kind=structure.list_kind or "unordered",
            depth=max(0, structure.list_depth or 0),
            span_ids=span_ids,
        )
    return ParagraphBlock(order=order, span_ids=span_ids)


def _assigned_block(
    semantic: SemanticBlock,
    span_ids: tuple[SpanId, ...],
    page: int | None,
    bbox: SourceBox | None,
    catalog: dict[SpanId, Any],
    *,
    additional_boxes: tuple[SourceBox, ...] = (),
) -> _AssignedBlock:
    return _AssignedBlock(
        semantic=semantic,
        span_ids=span_ids,
        page=page,
        bbox=bbox,
        source_boxes=(
            *((bbox,) if bbox is not None else ()),
            *additional_boxes,
            *(catalog[span_id].bbox for span_id in span_ids if catalog[span_id].bbox is not None),
        ),
    )


def _score_candidate(
    *,
    text: str,
    text_hint: str,
    candidate_order: int,
    structure_order: int,
    page: int | None,
    bbox: SourceBox | None,
    structure_page: int | None,
    structure_bbox: SourceBox | None,
) -> float | None:
    if page is not None and structure_page is not None and page != structure_page:
        return None
    scores: list[tuple[float, float]] = []
    projected_text = _comparison_projection(text)
    projected_hint = _comparison_projection(text_hint)
    scores.append(
        (
            TEXT_SIMILARITY_WEIGHT,
            SequenceMatcher(None, projected_text, projected_hint, autojunk=False).ratio(),
        )
    )
    scores.append((ORDER_WEIGHT, 1.0 / (1.0 + abs(candidate_order - structure_order))))

    if bbox is not None and structure_bbox is not None:
        overlap = _box_overlap_score(bbox, structure_bbox)
        if overlap <= 0:
            return None
        scores.append((OVERLAP_WEIGHT, overlap))

    total_weight = sum(weight for weight, _score in scores)
    return sum(weight * score for weight, score in scores) / total_weight


def _validate_structure_table_limits(
    row_count: int,
    column_count: int,
    cell_count: int,
) -> None:
    if row_count > MAX_TABLE_ROWS:
        raise ValueError("TABLE_ROWS_LIMIT_EXCEEDED")
    if column_count > MAX_TABLE_COLUMNS:
        raise ValueError("TABLE_COLUMNS_LIMIT_EXCEEDED")
    if row_count <= 0 or column_count <= 0:
        raise ValueError("TABLE_DIMENSIONS_INVALID")
    if row_count * column_count > MAX_TABLE_GRID_AREA:
        raise ValueError("TABLE_GRID_AREA_LIMIT_EXCEEDED")
    if cell_count > MAX_TABLE_CELLS:
        raise ValueError("TABLE_CELL_COUNT_LIMIT_EXCEEDED")


def _comparison_projection(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold()
    return "".join(character for character in normalized if not character.isspace())


def _groups_location(
    groups: tuple[NativeGroup, ...], catalog: dict[SpanId, Any]
) -> tuple[int | None, SourceBox | None] | None:
    pages = {
        page
        for group in groups
        for page in (
            group.page,
            *(catalog[span_id].page for span_id in group.span_ids),
        )
        if page is not None
    }
    if len(pages) > 1:
        return None
    page = next(iter(pages)) if pages else None
    boxes = [
        candidate
        for group in groups
        for candidate in (
            group.bbox,
            *(catalog[span_id].bbox for span_id in group.span_ids),
        )
        if candidate is not None
    ]
    return page, _union_boxes(boxes)


def _union_boxes(boxes: list[SourceBox]) -> SourceBox | None:
    if not boxes:
        return None
    return SourceBox(
        left=min(item.left for item in boxes),
        bottom=min(item.bottom for item in boxes),
        right=max(item.right for item in boxes),
        top=max(item.top for item in boxes),
    )


def _box_overlap_score(first: SourceBox, second: SourceBox) -> float:
    intersection_width = max(0.0, min(first.right, second.right) - max(first.left, second.left))
    intersection_height = max(0.0, min(first.top, second.top) - max(first.bottom, second.bottom))
    intersection = intersection_width * intersection_height
    first_area = (first.right - first.left) * (first.top - first.bottom)
    second_area = (second.right - second.left) * (second.top - second.bottom)
    smaller_area = min(first_area, second_area)
    if smaller_area <= 0:
        return 1.0 if first == second else 0.0
    return intersection / smaller_area


def _boxes_overlap(first: SourceBox, second: SourceBox) -> bool:
    return _box_overlap_score(first, second) > 0


def _exclude_repeated_edges(
    native: NativeDocument,
    assignments: list[_AssignedBlock],
    catalog: dict[SpanId, Any],
) -> tuple[list[_AssignedBlock], tuple[ExcludedSpan, ...]]:
    if native.page_count < 2:
        return assignments, ()
    threshold = max(2, math.ceil(native.page_count * REPEAT_RATIO))
    candidates: dict[
        tuple[Literal["top", "bottom"], str], list[tuple[int, _AssignedBlock, str]]
    ] = {}
    for index, assignment in enumerate(assignments):
        if assignment.page is None or assignment.bbox is None:
            continue
        edge: Literal["top", "bottom"] | None = None
        if assignment.bbox.bottom >= 1.0 - PAGE_EDGE_BAND:
            edge = "top"
        elif assignment.bbox.top <= PAGE_EDGE_BAND:
            edge = "bottom"
        if edge is None:
            continue
        text = "".join(catalog[span_id].text for span_id in assignment.span_ids)
        signature = (
            "<PAGE_NUMBER>" if _PAGE_NUMBER.fullmatch(text) else _comparison_projection(text)
        )
        if not signature:
            continue
        candidates.setdefault((edge, signature), []).append((index, assignment, text))

    excluded_by_id: dict[SpanId, ExcludedSpan] = {}
    removed_indices: set[int] = set()
    for (edge, signature), items in candidates.items():
        if len({assignment.page for _index, assignment, _text in items}) < threshold:
            continue
        for index, assignment, _text in items:
            if _overlaps_other_assignment(index, assignment, assignments):
                continue
            kind: Literal["page_header", "page_footer", "page_number"]
            if signature == "<PAGE_NUMBER>":
                kind = "page_number"
            elif edge == "top":
                kind = "page_header"
            else:
                kind = "page_footer"
            removed_indices.add(index)
            for span_id in assignment.span_ids:
                excluded_by_id[span_id] = ExcludedSpan(span_id=span_id, kind=kind)

    retained = [item for index, item in enumerate(assignments) if index not in removed_indices]
    excluded = tuple(
        excluded_by_id[source_span.span_id]
        for source_span in sorted(native.spans, key=lambda item: item.ordinal)
        if source_span.span_id in excluded_by_id
    )
    return retained, excluded


def _overlaps_other_assignment(
    own_index: int, candidate: _AssignedBlock, assignments: list[_AssignedBlock]
) -> bool:
    assert candidate.bbox is not None
    for index, other in enumerate(assignments):
        if index == own_index or other.page != candidate.page:
            continue
        if any(_boxes_overlap(candidate.bbox, other_box) for other_box in other.source_boxes):
            return True
    return False
