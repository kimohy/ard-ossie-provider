"""Deterministic layout normalization over immutable PDF evidence."""

from __future__ import annotations

import re
from collections import defaultdict
from statistics import median
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ard_ossie.canonical import canonical_hash
from ard_ossie.models import Sha256
from ard_ossie.semantic.evidence import (
    AtomId,
    EvidenceAtom,
    EvidenceDocument,
    RegionId,
    make_evidence_id,
)
from ard_ossie.semantic.models import ImmutableStrictModel, SourceBox
from ard_ossie.semantic.structure import StructureDocument

LineId = Annotated[str, StringConstraints(pattern=r"^line_[0-9a-f]{16}$")]
LayoutHint = Literal[
    "heading",
    "paragraph",
    "list_item",
    "table",
    "caption",
    "figure",
    "page_header",
    "page_footer",
    "page_number",
]

MAX_HORIZONTAL_GAP_MULTIPLIER = 6.0
LINE_GAP_MULTIPLIER = 1.75
COLUMN_OVERLAP_RATIO = 0.20
BASELINE_OVERLAP_RATIO = 0.35
_PAGE_NUMBER = re.compile(r"\s*\d+\s*(?:/\s*\d+\s*)?")


class LayoutLine(ImmutableStrictModel):
    line_id: LineId
    page: int = Field(ge=1)
    bbox: SourceBox
    atom_ids: tuple[AtomId, ...] = Field(min_length=1)
    evidence_region_ids: tuple[RegionId, ...] = Field(min_length=1)
    baseline: float = Field(ge=0, le=1)
    median_height: float = Field(gt=0, le=1)


class LayoutRegion(ImmutableStrictModel):
    region_id: RegionId
    page: int = Field(ge=1)
    bbox: SourceBox
    line_ids: tuple[LineId, ...] = Field(min_length=1)
    atom_ids: tuple[AtomId, ...] = Field(min_length=1)
    evidence_region_ids: tuple[RegionId, ...] = Field(min_length=1)
    column: int = Field(ge=0)
    hint: LayoutHint | None = None
    repeated_edge: bool = False


class ReadingOrderEdge(ImmutableStrictModel):
    before_region_id: RegionId
    after_region_id: RegionId
    reason: Literal["same_column", "next_column", "next_page"]


class LayoutDocument(ImmutableStrictModel):
    source_hash: Sha256
    page_count: int = Field(ge=1)
    lines: tuple[LayoutLine, ...]
    regions: tuple[LayoutRegion, ...]
    order_edges: tuple[ReadingOrderEdge, ...]

    @model_validator(mode="after")
    def validate_layout(self) -> LayoutDocument:
        line_ids = [line.line_id for line in self.lines]
        region_ids = [region.region_id for region in self.regions]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("LAYOUT_LINES_NOT_UNIQUE")
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("LAYOUT_REGIONS_NOT_UNIQUE")
        line_catalog = set(line_ids)
        region_catalog = set(region_ids)
        owned_lines = [line_id for region in self.regions for line_id in region.line_ids]
        if set(owned_lines) != line_catalog or len(owned_lines) != len(set(owned_lines)):
            raise ValueError("LAYOUT_LINE_OWNERSHIP_INVALID")
        if any(
            edge.before_region_id not in region_catalog
            or edge.after_region_id not in region_catalog
            or edge.before_region_id == edge.after_region_id
            for edge in self.order_edges
        ):
            raise ValueError("READING_ORDER_EDGE_INVALID")
        _ordered_region_ids(self.regions, self.order_edges)
        return self


def normalize_layout(
    evidence: EvidenceDocument,
    hints: StructureDocument,
    *,
    page_edge_band: float = 0.10,
    repeat_ratio: float = 0.60,
) -> LayoutDocument:
    lines = _cluster_lines(evidence)
    regions = _group_regions(evidence, lines)
    regions = _apply_structure_hints(regions, hints)
    regions = _mark_repeated_edges(
        evidence,
        regions,
        page_edge_band=page_edge_band,
        repeat_ratio=repeat_ratio,
    )
    regions = tuple(sorted(regions, key=_reading_key))
    edges = _reading_order_edges(regions)
    return LayoutDocument(
        source_hash=evidence.source_hash,
        page_count=evidence.page_count,
        lines=lines,
        regions=regions,
        order_edges=edges,
    )


def topological_region_order(layout: LayoutDocument) -> tuple[RegionId, ...]:
    return _ordered_region_ids(layout.regions, layout.order_edges)


def _cluster_lines(evidence: EvidenceDocument) -> tuple[LayoutLine, ...]:
    atoms = {atom.atom_id: atom for atom in evidence.atoms}
    lines: list[LayoutLine] = []
    for evidence_region in evidence.regions:
        ordered = [atoms[atom_id] for atom_id in evidence_region.atom_ids]
        for chunk in _line_chunks(ordered):
            boxes = [atom.bbox for atom in chunk if atom.bbox is not None]
            bbox = _union_boxes(boxes)
            if bbox is None:
                continue
            heights = [box.top - box.bottom for box in boxes if box.top > box.bottom]
            if not heights:
                continue
            line_id = _line_id(
                evidence.source_hash,
                chunk[0].atom_id,
                chunk[-1].atom_id,
            )
            lines.append(
                LayoutLine(
                    line_id=line_id,
                    page=evidence_region.page,
                    bbox=bbox,
                    atom_ids=tuple(atom.atom_id for atom in chunk),
                    evidence_region_ids=(evidence_region.region_id,),
                    baseline=median(box.bottom for box in boxes),
                    median_height=median(heights),
                )
            )
    return tuple(sorted(lines, key=lambda item: (item.page, -item.bbox.top, item.bbox.left)))


def _line_chunks(atoms: list[EvidenceAtom]) -> list[list[EvidenceAtom]]:
    visible_widths = [
        atom.bbox.right - atom.bbox.left
        for atom in atoms
        if atom.bbox is not None and atom.bbox.right > atom.bbox.left and atom.kind == "character"
    ]
    median_width = median(visible_widths) if visible_widths else 0.0
    chunks: list[list[EvidenceAtom]] = []
    current: list[EvidenceAtom] = []
    previous_visible: EvidenceAtom | None = None
    for atom in atoms:
        split = False
        if current and atom.kind != "line_break" and previous_visible is not None:
            split = not _same_baseline(previous_visible.bbox, atom.bbox)
            if (
                not split
                and median_width > 0
                and previous_visible.bbox is not None
                and atom.bbox is not None
                and atom.bbox.left - previous_visible.bbox.right
                > median_width * MAX_HORIZONTAL_GAP_MULTIPLIER
            ):
                split = True
        if split:
            chunks.append(current)
            current = []
        current.append(atom)
        if atom.kind == "line_break":
            chunks.append(current)
            current = []
            previous_visible = None
        elif atom.bbox is not None and atom.kind == "character":
            previous_visible = atom
    if current:
        chunks.append(current)
    return chunks


def _group_regions(
    evidence: EvidenceDocument,
    lines: tuple[LayoutLine, ...],
) -> tuple[LayoutRegion, ...]:
    column_by_line = _column_assignments(lines)
    grouped: list[list[LayoutLine]] = []
    for (_page, _column), page_lines in _lines_by_page_column(lines, column_by_line).items():
        current: list[LayoutLine] = []
        for line in sorted(page_lines, key=lambda item: (-item.bbox.top, item.bbox.left)):
            if current and not _lines_share_region(current[-1], line):
                grouped.append(current)
                current = []
            current.append(line)
        if current:
            grouped.append(current)

    regions: list[LayoutRegion] = []
    for group in grouped:
        atom_ids = tuple(atom_id for line in group for atom_id in line.atom_ids)
        evidence_region_ids = tuple(
            dict.fromkeys(
                region_id for line in group for region_id in line.evidence_region_ids
            )
        )
        bbox = _union_boxes([line.bbox for line in group])
        assert bbox is not None
        regions.append(
            LayoutRegion(
                region_id=make_evidence_id(
                    "region", evidence.source_hash, "layout", atom_ids[0], atom_ids[-1]
                ),
                page=group[0].page,
                bbox=bbox,
                line_ids=tuple(line.line_id for line in group),
                atom_ids=atom_ids,
                evidence_region_ids=evidence_region_ids,
                column=column_by_line[group[0].line_id],
            )
        )
    return tuple(regions)


def _column_assignments(lines: tuple[LayoutLine, ...]) -> dict[LineId, int]:
    result: dict[LineId, int] = {}
    by_page: dict[int, list[LayoutLine]] = defaultdict(list)
    for line in lines:
        by_page[line.page].append(line)
    for page_lines in by_page.values():
        columns: list[SourceBox] = []
        members: list[list[LayoutLine]] = []
        for line in sorted(page_lines, key=lambda item: (item.bbox.left, -item.bbox.top)):
            match = next(
                (
                    index
                    for index, column in enumerate(columns)
                    if _horizontal_overlap_ratio(line.bbox, column) >= COLUMN_OVERLAP_RATIO
                ),
                None,
            )
            if match is None:
                columns.append(line.bbox)
                members.append([line])
            else:
                members[match].append(line)
                columns[match] = _union_boxes([columns[match], line.bbox]) or line.bbox
        ordered = sorted(range(len(columns)), key=lambda index: columns[index].left)
        for column_number, old_index in enumerate(ordered):
            for line in members[old_index]:
                result[line.line_id] = column_number
    return result


def _lines_by_page_column(
    lines: tuple[LayoutLine, ...],
    column_by_line: dict[LineId, int],
) -> dict[tuple[int, int], list[LayoutLine]]:
    result: dict[tuple[int, int], list[LayoutLine]] = defaultdict(list)
    for line in lines:
        result[(line.page, column_by_line[line.line_id])].append(line)
    return result


def _lines_share_region(first: LayoutLine, second: LayoutLine) -> bool:
    gap = first.bbox.bottom - second.bbox.top
    height = median((first.median_height, second.median_height))
    return (
        gap <= height * LINE_GAP_MULTIPLIER
        and _horizontal_overlap_ratio(first.bbox, second.bbox) >= COLUMN_OVERLAP_RATIO
    )


def _apply_structure_hints(
    regions: tuple[LayoutRegion, ...],
    hints: StructureDocument,
) -> tuple[LayoutRegion, ...]:
    result: list[LayoutRegion] = []
    for region in regions:
        matches = [
            (_box_overlap_fraction(region.bbox, block.bbox), block)
            for block in hints.blocks
            if block.bbox is not None and block.page == region.page
        ]
        best = max(matches, default=None, key=lambda item: (item[0], -item[1].order))
        hint = best[1].kind if best is not None and best[0] > 0 else None
        result.append(region.model_copy(update={"hint": hint}))
    return tuple(result)


def _mark_repeated_edges(
    evidence: EvidenceDocument,
    regions: tuple[LayoutRegion, ...],
    *,
    page_edge_band: float,
    repeat_ratio: float,
) -> tuple[LayoutRegion, ...]:
    atom_text = {atom.atom_id: atom.text for atom in evidence.atoms}
    keyed: dict[tuple[str, str], list[RegionId]] = defaultdict(list)
    region_text: dict[RegionId, str] = {}
    for region in regions:
        text = "".join(atom_text[atom_id] for atom_id in region.atom_ids)
        normalized = "".join(text.split()).casefold()
        region_text[region.region_id] = normalized
        if region.bbox.top >= 1 - page_edge_band:
            keyed[("page_header", normalized)].append(region.region_id)
        elif region.bbox.bottom <= page_edge_band:
            keyed[("page_footer", normalized)].append(region.region_id)
    required_pages = evidence.page_count * repeat_ratio
    page_by_region = {region.region_id: region.page for region in regions}
    repeated = {
        region_id: edge
        for (edge, _text), region_ids in keyed.items()
        if len({page_by_region[region_id] for region_id in region_ids}) >= required_pages
        for region_id in region_ids
    }
    result: list[LayoutRegion] = []
    for region in regions:
        edge = repeated.get(region.region_id)
        if edge is None:
            result.append(region)
            continue
        hint: LayoutHint = (
            "page_number"
            if _PAGE_NUMBER.fullmatch(region_text[region.region_id])
            else edge  # type: ignore[assignment]
        )
        result.append(region.model_copy(update={"hint": hint, "repeated_edge": True}))
    return tuple(result)


def _reading_order_edges(
    regions: tuple[LayoutRegion, ...],
) -> tuple[ReadingOrderEdge, ...]:
    edges: list[ReadingOrderEdge] = []
    for first, second in zip(regions, regions[1:], strict=False):
        if first.page != second.page:
            reason = "next_page"
        elif first.column != second.column:
            reason = "next_column"
        else:
            reason = "same_column"
        edges.append(
            ReadingOrderEdge(
                before_region_id=first.region_id,
                after_region_id=second.region_id,
                reason=reason,
            )
        )
    return tuple(edges)


def _ordered_region_ids(
    regions: tuple[LayoutRegion, ...],
    edges: tuple[ReadingOrderEdge, ...],
) -> tuple[RegionId, ...]:
    rank = {region.region_id: index for index, region in enumerate(regions)}
    incoming = {region.region_id: 0 for region in regions}
    outgoing: dict[RegionId, list[RegionId]] = defaultdict(list)
    for edge in edges:
        outgoing[edge.before_region_id].append(edge.after_region_id)
        incoming[edge.after_region_id] += 1
    ready = sorted((item for item, count in incoming.items() if count == 0), key=rank.get)
    ordered: list[RegionId] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for following in sorted(outgoing[current], key=rank.get):
            incoming[following] -= 1
            if incoming[following] == 0:
                ready.append(following)
                ready.sort(key=rank.get)
    if len(ordered) != len(regions):
        raise ValueError("READING_ORDER_CYCLE")
    return tuple(ordered)


def _reading_key(region: LayoutRegion) -> tuple[int, int, float, float, str]:
    return region.page, region.column, -region.bbox.top, region.bbox.left, region.region_id


def _line_id(source_hash: str, *parts: object) -> str:
    digest = canonical_hash([source_hash, *[str(part) for part in parts]])
    return f"line_{digest[:16]}"


def _same_baseline(first: SourceBox | None, second: SourceBox | None) -> bool:
    if first is None or second is None:
        return True
    overlap = max(0.0, min(first.top, second.top) - max(first.bottom, second.bottom))
    minimum_height = min(first.top - first.bottom, second.top - second.bottom)
    return minimum_height > 0 and overlap / minimum_height >= BASELINE_OVERLAP_RATIO


def _horizontal_overlap_ratio(first: SourceBox, second: SourceBox) -> float:
    overlap = max(0.0, min(first.right, second.right) - max(first.left, second.left))
    minimum_width = min(first.right - first.left, second.right - second.left)
    return 0.0 if minimum_width <= 0 else overlap / minimum_width


def _box_overlap_fraction(first: SourceBox, second: SourceBox | None) -> float:
    if second is None:
        return 0.0
    width = max(0.0, min(first.right, second.right) - max(first.left, second.left))
    height = max(0.0, min(first.top, second.top) - max(first.bottom, second.bottom))
    area = (first.right - first.left) * (first.top - first.bottom)
    return 0.0 if area <= 0 else width * height / area


def _union_boxes(boxes: list[SourceBox]) -> SourceBox | None:
    if not boxes:
        return None
    return SourceBox(
        left=min(box.left for box in boxes),
        bottom=min(box.bottom for box in boxes),
        right=max(box.right for box in boxes),
        top=max(box.top for box in boxes),
    )
