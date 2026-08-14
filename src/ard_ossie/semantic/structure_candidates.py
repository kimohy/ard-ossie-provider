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
    make_candidate_id,
    make_candidate_set_id,
)
from ard_ossie.semantic.evidence import EvidenceDocument, ExtractedEvidence, RegionId
from ard_ossie.semantic.layout import LayoutDocument, LayoutRegion
from ard_ossie.semantic.structure import StructureDocument

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
        0.58
        + 0.22 * float(len(text.strip()) > 48)
        + 0.08 * features["terminal_punctuation"]
        + 0.06 * features["paragraph_hint"],
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
        specs.append(("table", 0.74, None, None, None))

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
