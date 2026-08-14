"""Bounded semantic structure candidates derived from immutable evidence."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from statistics import median

from ard_ossie.semantic.candidates import (
    BlockCandidate,
    CandidateSet,
    ContinuationCandidate,
    ReadingOrderCandidate,
    RecognitionCandidate,
    TableCandidate,
    TableCellCandidate,
    make_candidate_id,
    make_candidate_set_id,
    make_cell_id,
)
from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceDocument,
    ExtractedEvidence,
    RegionId,
)
from ard_ossie.semantic.layout import LayoutDocument, LayoutLine, LayoutRegion
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.structure import StructureDocument, StructureTable

_HEADING_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)*)(?:[.)]|\s)")
_ORDERED_LIST = re.compile(r"^(\s*)(?:\d+[.)]|[a-zA-Z][.)])\s+")
_UNORDERED_LIST = re.compile(r"^(\s*)[-*•▪◦]\s+")
_CAPTION = re.compile(r"^\s*(?:그림|표|사진|도표|figure|table)\s*\d+", re.IGNORECASE)
_TERMINAL_PUNCTUATION = frozenset(".!?。！？다요음임함됨)")


def build_recognition_candidate_sets(
    evidence: ExtractedEvidence | EvidenceDocument,
) -> tuple[CandidateSet, ...]:
    hypotheses = {item.hypothesis_id: item for item in evidence.hypotheses}
    result: list[CandidateSet] = []
    for region in evidence.regions:
        if not region.hypothesis_ids:
            continue
        candidates = tuple(
            sorted(
                (
                    RecognitionCandidate(
                        candidate_id=make_candidate_id(
                            "recognition",
                            region.region_id,
                            {
                                "hypothesis_id": hypothesis.hypothesis_id,
                                "text": hypothesis.text,
                                "engine": hypothesis.engine,
                            },
                        ),
                        region_id=region.region_id,
                        hypothesis_id=hypothesis.hypothesis_id,
                        text=hypothesis.text,
                        engine=hypothesis.engine,
                        score=hypothesis.confidence,
                        features={"ocr_confidence": hypothesis.confidence},
                    )
                    for hypothesis_id in region.hypothesis_ids
                    for hypothesis in (hypotheses[hypothesis_id],)
                ),
                key=lambda item: (-item.score, item.candidate_id),
            )[:5]
        )
        result.append(
            _candidate_set(
                evidence.source_hash,
                region.region_id,
                "recognition",
                candidates,
            )
        )
    return tuple(result)


def build_block_candidate_sets(
    *,
    evidence: EvidenceDocument,
    layout: LayoutDocument,
    hints: StructureDocument,
) -> tuple[CandidateSet, ...]:
    if evidence.source_hash != layout.source_hash:
        raise ValueError("STRUCTURE_SOURCE_HASH_MISMATCH")
    atom_catalog = {atom.atom_id: atom for atom in evidence.atoms}
    typical_height = median(line.median_height for line in layout.lines) if layout.lines else 0.0
    result: list[CandidateSet] = []
    for region in layout.regions:
        if len(region.atom_ids) != len(set(region.atom_ids)) or not set(region.atom_ids).issubset(
            atom_catalog
        ):
            raise ValueError("STRUCTURE_REGION_ATOM_INVALID")
        text = "".join(atom_catalog[item].text for item in region.atom_ids)
        features = _block_features(region, text, typical_height, hints)
        candidates = _block_candidates(region, text, features)
        result.append(_candidate_set(layout.source_hash, region.region_id, "block", candidates))
    return tuple(result)


def build_reading_order_candidate_set(layout: LayoutDocument) -> CandidateSet:
    if not layout.regions:
        raise ValueError("READING_ORDER_REGIONS_EMPTY")
    anchor = layout.regions[0].region_id
    orders = _bounded_topological_orders(layout, limit=5)
    candidates = tuple(
        ReadingOrderCandidate(
            candidate_id=make_candidate_id(
                "reading_order",
                anchor,
                {"region_ids": order},
            ),
            region_id=anchor,
            region_ids=order,
            score=max(0.55, 0.88 - index * 0.06),
            features={"canonical_rank": float(index)},
        )
        for index, order in enumerate(orders)
    )
    return _candidate_set(layout.source_hash, anchor, "reading_order", candidates)


def build_continuation_candidate_sets(layout: LayoutDocument) -> tuple[CandidateSet, ...]:
    by_page: dict[int, list[LayoutRegion]] = defaultdict(list)
    for region in layout.regions:
        by_page[region.page].append(region)
    result: list[CandidateSet] = []
    for page in range(1, layout.page_count):
        previous = _page_edge_region(by_page.get(page, ()), bottom=True)
        current = _page_edge_region(by_page.get(page + 1, ()), bottom=False)
        if previous is None or current is None:
            continue
        repeated_header = any(
            region.repeated_edge and region.bbox.top >= 0.85
            for region in by_page.get(page + 1, ())
        )
        alignment = _horizontal_overlap_ratio(previous, current)
        matching_table = previous.hint == current.hint == "table"
        paragraph_flow = (
            previous.hint in {None, "paragraph"}
            and current.hint in {None, "paragraph"}
            and previous.bbox.bottom <= 0.10
            and current.bbox.top >= 0.80
        )
        if not (alignment >= 0.75 and ((matching_table and repeated_header) or paragraph_flow)):
            continue
        continue_score = 0.90 if matching_table and repeated_header else 0.78
        shared_features = {
            "edge_alignment": round(alignment, 6),
            "repeated_header": float(repeated_header),
            "matching_table": float(matching_table),
        }
        candidates = tuple(
            ContinuationCandidate(
                candidate_id=make_candidate_id(
                    "continuation",
                    current.region_id,
                    {
                        "previous_region_id": previous.region_id,
                        "current_region_id": current.region_id,
                        "continue_previous": decision,
                    },
                ),
                region_id=current.region_id,
                previous_region_id=previous.region_id,
                current_region_id=current.region_id,
                continue_previous=decision,
                score=score,
                features=shared_features,
            )
            for decision, score in ((True, continue_score), (False, 0.55))
        )
        result.append(
            _candidate_set(layout.source_hash, current.region_id, "continuation", candidates)
        )
    return tuple(result)


def build_table_candidate_set(
    region: LayoutRegion,
    evidence: EvidenceDocument,
    layout: LayoutDocument,
    hints: StructureDocument,
) -> CandidateSet:
    if evidence.source_hash != layout.source_hash:
        raise ValueError("TABLE_SOURCE_HASH_MISMATCH")
    layout_regions = {item.region_id: item for item in layout.regions}
    if layout_regions.get(region.region_id) != region:
        raise ValueError("TABLE_REGION_UNKNOWN")
    atom_catalog = {atom.atom_id: atom for atom in evidence.atoms}
    if len(region.atom_ids) != len(set(region.atom_ids)) or not set(region.atom_ids).issubset(
        atom_catalog
    ):
        raise ValueError("TABLE_REGION_ATOM_INVALID")
    line_catalog = {line.line_id: line for line in layout.lines}
    try:
        lines = tuple(line_catalog[line_id] for line_id in region.line_ids)
    except KeyError as error:
        raise ValueError("TABLE_REGION_LINE_UNKNOWN") from error
    if not lines:
        raise ValueError("TABLE_REGION_LINES_EMPTY")

    table_hint = _matching_table_hint(region, hints)
    geometry = _geometry_table_candidate(region, lines, atom_catalog, table_hint)
    candidates = [geometry]
    split = _split_spanning_cells(region, geometry, atom_catalog, lines)
    if split is not None and split.candidate_id != geometry.candidate_id:
        candidates.append(split)
    ordered = tuple(sorted(candidates, key=lambda item: (-item.score, item.candidate_id))[:5])
    return _candidate_set(layout.source_hash, region.region_id, "table", ordered)


def _block_features(
    region: LayoutRegion,
    text: str,
    typical_height: float,
    hints: StructureDocument,
) -> dict[str, float]:
    line_height = (region.bbox.top - region.bbox.bottom) / max(1, len(region.line_ids))
    hint_kinds = {
        block.kind
        for block in hints.blocks
        if block.page == region.page and block.bbox is not None and _overlaps(region, block.bbox)
    }
    if region.hint is not None:
        hint_kinds.add(region.hint)
    return {
        "numbered_heading": float(_HEADING_NUMBER.match(text) is not None),
        "ordered_list": float(_ORDERED_LIST.match(text) is not None),
        "unordered_list": float(_UNORDERED_LIST.match(text) is not None),
        "caption_shape": float(_CAPTION.match(text) is not None),
        "short_text": float(len(text.strip()) <= 48),
        "terminal_punctuation": float(text.rstrip().endswith(tuple(_TERMINAL_PUNCTUATION))),
        "large_geometry": float(typical_height > 0 and line_height >= typical_height * 1.25),
        "heading_hint": float("heading" in hint_kinds),
        "paragraph_hint": float("paragraph" in hint_kinds),
        "table_hint": float("table" in hint_kinds),
        "list_hint": float("list_item" in hint_kinds),
    }


def _block_candidates(
    region: LayoutRegion,
    text: str,
    features: dict[str, float],
) -> tuple[BlockCandidate, ...]:
    specs: list[tuple[str, float, int | None, str | None, int | None]] = []
    paragraph_score = min(
        0.96,
        0.78
        + 0.10 * float(len(text.strip()) > 48)
        + 0.08 * features["terminal_punctuation"]
        + 0.04 * features["paragraph_hint"],
    )
    specs.append(("paragraph", paragraph_score, None, None, None))

    heading_match = _HEADING_NUMBER.match(text)
    if heading_match or features["heading_hint"] or features["large_geometry"]:
        heading_level = _heading_level(heading_match.group(1) if heading_match else None)
        heading_score = min(
            0.96,
            0.44
            + 0.45 * features["numbered_heading"]
            + 0.12 * features["large_geometry"]
            + 0.10 * features["heading_hint"]
            + 0.04 * features["short_text"],
        )
        specs.append(("heading", heading_score, heading_level, None, None))

    list_match = _ORDERED_LIST.match(text) or _UNORDERED_LIST.match(text)
    if list_match or features["list_hint"]:
        list_kind = "ordered" if _ORDERED_LIST.match(text) else "unordered"
        indent = len(list_match.group(1)) if list_match else 0
        list_depth = min(8, max(1, indent // 2 + 1))
        list_score = min(
            0.96,
            0.72
            + 0.20 * float(list_match is not None)
            + 0.04 * features["list_hint"],
        )
        specs.append(("list_item", list_score, None, list_kind, list_depth))

    if features["caption_shape"]:
        specs.append(("caption", 0.90, None, None, None))
    if features["table_hint"]:
        specs.append(("table", 0.92, None, None, None))

    candidates = [
        BlockCandidate(
            candidate_id=make_candidate_id(
                "block",
                region.region_id,
                {
                    "block_kind": block_kind,
                    "atom_ids": region.atom_ids,
                    "heading_level": heading_level,
                    "list_kind": list_kind,
                    "list_depth": list_depth,
                },
            ),
            region_id=region.region_id,
            block_kind=block_kind,
            atom_ids=region.atom_ids,
            heading_level=heading_level,
            list_kind=list_kind,
            list_depth=list_depth,
            score=score,
            features=features,
        )
        for block_kind, score, heading_level, list_kind, list_depth in specs
    ]
    return tuple(sorted(candidates, key=lambda item: (-item.score, item.candidate_id))[:5])


def _heading_level(number: str | None) -> int:
    if number is None:
        return 1
    segments = number.split(".")
    if len(segments) > 1:
        return min(6, len(segments))
    return min(6, max(1, int(segments[0])))


def _matching_table_hint(
    region: LayoutRegion,
    hints: StructureDocument,
) -> StructureTable | None:
    matches = [
        block
        for block in hints.blocks
        if block.kind == "table"
        and block.table is not None
        and block.page == region.page
        and block.bbox is not None
        and _overlaps(region, block.bbox)
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: item.order).table


def _geometry_table_candidate(
    region: LayoutRegion,
    lines: tuple[LayoutLine, ...],
    atom_catalog: dict[str, EvidenceAtom],
    table_hint: StructureTable | None,
) -> TableCandidate:
    row_groups = _row_groups(lines)
    column_anchors = _column_anchors(lines, table_hint)
    row_count = table_hint.row_count if table_hint is not None else len(row_groups)
    column_count = table_hint.column_count if table_hint is not None else len(column_anchors)
    if row_count <= 0 or column_count <= 0:
        raise ValueError("TABLE_GRID_DIMENSIONS_INVALID")

    if table_hint is not None:
        cell_specs = _hinted_cell_specs(
            lines,
            table_hint,
            row_count=row_count,
            column_count=column_count,
        )
        score = 0.94
        features = {"geometry_grid": 0.82, "docling_grid_agreement": 0.94}
    else:
        cell_specs = _geometric_cell_specs(
            lines,
            row_groups,
            column_anchors,
            row_count=row_count,
            column_count=column_count,
        )
        score = 0.82
        features = {"geometry_grid": 0.82, "docling_grid_agreement": 0.0}
    cells = _complete_cells(region.region_id, cell_specs, row_count, column_count)
    return _make_table_candidate(
        region,
        row_count=row_count,
        column_count=column_count,
        cells=cells,
        score=score,
        features=features,
    )


def _row_groups(lines: tuple[LayoutLine, ...]) -> tuple[tuple[LayoutLine, ...], ...]:
    remaining = set(range(len(lines)))
    groups: list[tuple[LayoutLine, ...]] = []
    while remaining:
        component = {min(remaining)}
        changed = True
        while changed:
            changed = False
            for index in tuple(remaining - component):
                if any(
                    _vertical_overlap(lines[index].bbox, lines[member].bbox)
                    for member in component
                ):
                    component.add(index)
                    changed = True
        remaining -= component
        groups.append(tuple(lines[index] for index in sorted(component)))
    return tuple(
        sorted(
            groups,
            key=lambda group: (
                -max(line.bbox.top for line in group),
                min(line.bbox.left for line in group),
            ),
        )
    )


def _column_anchors(
    lines: tuple[LayoutLine, ...],
    table_hint: StructureTable | None,
) -> tuple[float, ...]:
    positions = [line.bbox.left for line in lines]
    if table_hint is not None:
        positions.extend(cell.bbox.left for cell in table_hint.cells if cell.bbox is not None)
    clusters: list[list[float]] = []
    for position in sorted(positions):
        if not clusters or position - median(clusters[-1]) > 0.04:
            clusters.append([position])
        else:
            clusters[-1].append(position)
    anchors = [median(cluster) for cluster in clusters]
    if table_hint is not None and len(anchors) > table_hint.column_count:
        anchors = anchors[: table_hint.column_count]
    if table_hint is not None and len(anchors) < table_hint.column_count:
        left = min(positions)
        right = max(
            [line.bbox.right for line in lines]
            + [cell.bbox.right for cell in table_hint.cells if cell.bbox is not None]
        )
        step = (right - left) / table_hint.column_count
        anchors = [left + step * index for index in range(table_hint.column_count)]
    return tuple(anchors)


def _geometric_cell_specs(
    lines: tuple[LayoutLine, ...],
    row_groups: tuple[tuple[LayoutLine, ...], ...],
    column_anchors: tuple[float, ...],
    *,
    row_count: int,
    column_count: int,
) -> list[tuple[int, int, int, int, tuple[str, ...], bool]]:
    row_by_line = {
        line.line_id: row
        for row, group in enumerate(row_groups[:row_count])
        for line in group
    }
    grouped: dict[tuple[int, int, int, int], list[str]] = defaultdict(list)
    for line in lines:
        row = row_by_line[line.line_id]
        start_column = _nearest_anchor(line.bbox.left, column_anchors)
        end_column = max(
            start_column + 1,
            sum(anchor < line.bbox.right - 0.02 for anchor in column_anchors),
        )
        end_column = min(column_count, end_column)
        grouped[(row, row + 1, start_column, end_column)].extend(line.atom_ids)
    return [
        (*coordinates, tuple(atom_ids), coordinates[0] == 0)
        for coordinates, atom_ids in sorted(grouped.items())
    ]


def _hinted_cell_specs(
    lines: tuple[LayoutLine, ...],
    table_hint: StructureTable,
    *,
    row_count: int,
    column_count: int,
) -> list[tuple[int, int, int, int, tuple[str, ...], bool]]:
    assignments: dict[int, list[str]] = defaultdict(list)
    for line in lines:
        eligible = [
            (index, cell)
            for index, cell in enumerate(table_hint.cells)
            if cell.bbox is not None and _box_overlap_area(line.bbox, cell.bbox) > 0
        ]
        if not eligible:
            raise ValueError("TABLE_HINT_LINE_UNALLOCATED")
        best_index, _best_cell = max(
            eligible,
            key=lambda item: (_box_overlap_area(line.bbox, item[1].bbox), -item[0]),
        )
        assignments[best_index].extend(line.atom_ids)
    specs = [
        (
            cell.start_row,
            cell.end_row,
            cell.start_column,
            cell.end_column,
            tuple(assignments[index]),
            cell.column_header,
        )
        for index, cell in enumerate(table_hint.cells)
    ]
    if any(
        start_row < 0
        or end_row > row_count
        or start_column < 0
        or end_column > column_count
        for start_row, end_row, start_column, end_column, _atom_ids, _header in specs
    ):
        raise ValueError("TABLE_HINT_CELL_OUT_OF_BOUNDS")
    return specs


def _complete_cells(
    region_id: RegionId,
    specs: list[tuple[int, int, int, int, tuple[str, ...], bool]],
    row_count: int,
    column_count: int,
) -> tuple[TableCellCandidate, ...]:
    occupied = {
        (row, column)
        for start_row, end_row, start_column, end_column, _atom_ids, _header in specs
        for row in range(start_row, end_row)
        for column in range(start_column, end_column)
    }
    complete_specs = [*specs]
    for row in range(row_count):
        for column in range(column_count):
            if (row, column) not in occupied:
                complete_specs.append((row, row + 1, column, column + 1, (), row == 0))
    return tuple(
        TableCellCandidate(
            cell_id=make_cell_id(
                region_id,
                {
                    "start_row": start_row,
                    "end_row": end_row,
                    "start_column": start_column,
                    "end_column": end_column,
                    "atom_ids": atom_ids,
                },
            ),
            start_row=start_row,
            end_row=end_row,
            start_column=start_column,
            end_column=end_column,
            atom_ids=atom_ids,
            column_header=column_header,
        )
        for start_row, end_row, start_column, end_column, atom_ids, column_header in sorted(
            complete_specs,
            key=lambda item: (item[0], item[2], item[1], item[3]),
        )
    )


def _split_spanning_cells(
    region: LayoutRegion,
    candidate: TableCandidate,
    atom_catalog: dict[str, EvidenceAtom],
    lines: tuple[LayoutLine, ...],
) -> TableCandidate | None:
    spanning = [cell for cell in candidate.cells if cell.end_column - cell.start_column > 1]
    if not spanning:
        return None
    anchors = _column_anchors(lines, None)
    specs: list[tuple[int, int, int, int, tuple[str, ...], bool]] = []
    changed = False
    for cell in candidate.cells:
        if cell.end_column - cell.start_column <= 1:
            specs.append(
                (
                    cell.start_row,
                    cell.end_row,
                    cell.start_column,
                    cell.end_column,
                    cell.atom_ids,
                    cell.column_header,
                )
            )
            continue
        assignments: dict[int, list[str]] = defaultdict(list)
        for atom_id in cell.atom_ids:
            atom = atom_catalog[atom_id]
            if atom.bbox is None:
                assignments[cell.start_column].append(atom_id)
                continue
            center = (atom.bbox.left + atom.bbox.right) / 2
            column = min(
                cell.end_column - 1,
                max(cell.start_column, _nearest_anchor(center, anchors)),
            )
            assignments[column].append(atom_id)
        if len(assignments) <= 1:
            specs.append(
                (
                    cell.start_row,
                    cell.end_row,
                    cell.start_column,
                    cell.end_column,
                    cell.atom_ids,
                    cell.column_header,
                )
            )
            continue
        changed = True
        for column in range(cell.start_column, cell.end_column):
            specs.append(
                (
                    cell.start_row,
                    cell.end_row,
                    column,
                    column + 1,
                    tuple(assignments[column]),
                    cell.column_header,
                )
            )
    if not changed:
        return None
    cells = _complete_cells(region.region_id, specs, candidate.row_count, candidate.column_count)
    return _make_table_candidate(
        region,
        row_count=candidate.row_count,
        column_count=candidate.column_count,
        cells=cells,
        score=max(0.0, candidate.score - 0.06),
        features={**candidate.features, "split_boundary_variant": 0.76},
    )


def _make_table_candidate(
    region: LayoutRegion,
    *,
    row_count: int,
    column_count: int,
    cells: tuple[TableCellCandidate, ...],
    score: float,
    features: dict[str, float],
) -> TableCandidate:
    candidate_id = make_candidate_id(
        "table",
        region.region_id,
        {
            "row_count": row_count,
            "column_count": column_count,
            "cells": cells,
        },
    )
    return TableCandidate(
        candidate_id=candidate_id,
        region_id=region.region_id,
        row_count=row_count,
        column_count=column_count,
        cells=cells,
        atom_ids=region.atom_ids,
        score=score,
        features=features,
    )


def _nearest_anchor(position: float, anchors: tuple[float, ...]) -> int:
    return min(range(len(anchors)), key=lambda index: (abs(anchors[index] - position), index))


def _vertical_overlap(first: SourceBox, second: SourceBox) -> bool:
    return min(first.top, second.top) > max(first.bottom, second.bottom)


def _box_overlap_area(first: SourceBox, second: SourceBox | None) -> float:
    if second is None:
        return 0.0
    return max(0.0, min(first.right, second.right) - max(first.left, second.left)) * max(
        0.0,
        min(first.top, second.top) - max(first.bottom, second.bottom),
    )


def _bounded_topological_orders(
    layout: LayoutDocument,
    *,
    limit: int,
) -> tuple[tuple[RegionId, ...], ...]:
    region_ids = tuple(region.region_id for region in layout.regions)
    rank = {region_id: index for index, region_id in enumerate(region_ids)}
    incoming = {region_id: 0 for region_id in region_ids}
    outgoing: dict[RegionId, list[RegionId]] = defaultdict(list)
    for edge in layout.order_edges:
        outgoing[edge.before_region_id].append(edge.after_region_id)
        incoming[edge.after_region_id] += 1
    results: list[tuple[RegionId, ...]] = []

    def visit(order: tuple[RegionId, ...], counts: dict[RegionId, int]) -> None:
        if len(results) >= limit:
            return
        if len(order) == len(region_ids):
            results.append(order)
            return
        ready = sorted(
            (
                region_id
                for region_id in region_ids
                if region_id not in order and counts[region_id] == 0
            ),
            key=rank.get,
        )
        for region_id in ready:
            following_counts = counts.copy()
            for following in outgoing[region_id]:
                following_counts[following] -= 1
            visit((*order, region_id), following_counts)
            if len(results) >= limit:
                return

    visit((), incoming)
    if not results:
        raise ValueError("READING_ORDER_CYCLE")
    return tuple(results)


def _page_edge_region(
    regions: Iterable[LayoutRegion],
    *,
    bottom: bool,
) -> LayoutRegion | None:
    eligible = [region for region in regions if not region.repeated_edge]
    if not eligible:
        return None
    key = (lambda item: (item.bbox.bottom, item.region_id)) if bottom else (
        lambda item: (-item.bbox.top, item.region_id)
    )
    return min(eligible, key=key)


def _horizontal_overlap_ratio(first: LayoutRegion, second: LayoutRegion) -> float:
    overlap = max(
        0.0,
        min(first.bbox.right, second.bbox.right)
        - max(first.bbox.left, second.bbox.left),
    )
    minimum = min(first.bbox.right - first.bbox.left, second.bbox.right - second.bbox.left)
    return 0.0 if minimum <= 0 else overlap / minimum


def _overlaps(region: LayoutRegion, box: object) -> bool:
    if not all(hasattr(box, name) for name in ("left", "right", "bottom", "top")):
        return False
    return bool(
        min(region.bbox.right, box.right) > max(region.bbox.left, box.left)
        and min(region.bbox.top, box.top) > max(region.bbox.bottom, box.bottom)
    )


def _candidate_set(
    source_hash: str,
    region_id: RegionId,
    decision_type: str,
    candidates: tuple[object, ...],
) -> CandidateSet:
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    return CandidateSet(
        candidate_set_id=make_candidate_set_id(source_hash, region_id, candidate_ids),
        source_hash=source_hash,
        region_id=region_id,
        decision_type=decision_type,
        candidates=candidates,
    )
