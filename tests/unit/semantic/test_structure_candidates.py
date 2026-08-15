from __future__ import annotations

from ard_ossie.semantic.adjudication import CandidateAdjudicator
from ard_ossie.semantic.candidates import (
    BlockCandidate,
    ContinuationCandidate,
    ReadingOrderCandidate,
    RecognitionCandidate,
)
from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceDocument,
    EvidenceExtractionMode,
    EvidenceRegion,
    ExtractedEvidence,
    RecognitionHypothesis,
)
from ard_ossie.semantic.layout import (
    LayoutDocument,
    LayoutLine,
    LayoutRegion,
    ReadingOrderEdge,
)
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.structure import (
    StructureBlock,
    StructureCell,
    StructureDocument,
    StructureTable,
)
from ard_ossie.semantic.structure_candidates import (
    build_block_candidate_sets,
    build_continuation_candidate_sets,
    build_reading_order_candidate_set,
    build_recognition_candidate_sets,
)

SOURCE_HASH = "d" * 64
BOX = SourceBox(left=0.1, bottom=0.7, right=0.8, top=0.76)


def _embedded_fixture(
    entries: tuple[tuple[int, str, SourceBox, str | None], ...],
) -> tuple[EvidenceDocument, LayoutDocument]:
    atoms: list[EvidenceAtom] = []
    evidence_regions: list[EvidenceRegion] = []
    lines: list[LayoutLine] = []
    regions: list[LayoutRegion] = []
    for index, (page, value, box, hint) in enumerate(entries, start=1):
        atom_ids: list[str] = []
        width = (box.right - box.left) / len(value)
        for character_index, character in enumerate(value):
            atom_id = f"atom_{len(atoms) + 1:016x}"
            atom_ids.append(atom_id)
            atoms.append(
                EvidenceAtom(
                    atom_id=atom_id,
                    ordinal=len(atoms),
                    page=page,
                    bbox=SourceBox(
                        left=box.left + character_index * width,
                        bottom=box.bottom,
                        right=box.left + (character_index + 1) * width,
                        top=box.top,
                    ),
                    text=character,
                    kind="whitespace" if character.isspace() else "character",
                    authority="embedded",
                    source_object=index,
                    source_index=character_index,
                )
            )
        evidence_region_id = f"region_{index:016x}"
        layout_region_id = f"region_{index + 100:016x}"
        line_id = f"line_{index:016x}"
        evidence_regions.append(
            EvidenceRegion(
                region_id=evidence_region_id,
                page=page,
                bbox=box,
                atom_ids=tuple(atom_ids),
                authority="embedded",
            )
        )
        lines.append(
            LayoutLine(
                line_id=line_id,
                page=page,
                bbox=box,
                atom_ids=tuple(atom_ids),
                evidence_region_ids=(evidence_region_id,),
                baseline=box.bottom,
                median_height=box.top - box.bottom,
            )
        )
        regions.append(
            LayoutRegion(
                region_id=layout_region_id,
                page=page,
                bbox=box,
                line_ids=(line_id,),
                atom_ids=tuple(atom_ids),
                evidence_region_ids=(evidence_region_id,),
                column=0,
                hint=hint,
            )
        )
    evidence = EvidenceDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=max(page for page, *_rest in entries),
        parser_versions={"fixture": "1"},
        atoms=tuple(atoms),
        regions=tuple(evidence_regions),
    )
    edges = tuple(
        ReadingOrderEdge(
            before_region_id=first.region_id,
            after_region_id=second.region_id,
            reason="next_page" if first.page != second.page else "same_column",
        )
        for first, second in zip(regions, regions[1:], strict=False)
    )
    layout = LayoutDocument(
        source_hash=SOURCE_HASH,
        page_count=evidence.page_count,
        lines=tuple(lines),
        regions=tuple(regions),
        order_edges=edges,
    )
    return evidence, layout


def test_heading_candidate_uses_numbering_and_geometry_without_authoring_text() -> None:
    evidence, layout = _embedded_fixture(((1, "2. 범용 설계", BOX, None),))

    candidate_set = build_block_candidate_sets(
        evidence=evidence,
        layout=layout,
        hints=StructureDocument(blocks=()),
    )[0]
    heading = max(candidate_set.candidates, key=lambda item: item.score)

    assert isinstance(heading, BlockCandidate)
    assert heading.block_kind == "heading"
    assert heading.heading_level == 2
    assert heading.atom_ids == layout.regions[0].atom_ids


def test_docling_heading_hint_cannot_override_ordinary_text_evidence() -> None:
    text = "이 문장은 일반적인 본문 문장으로 충분히 길고 제목 번호도 포함하지 않는다."
    evidence, layout = _embedded_fixture(((1, text, BOX, "heading"),))
    hints = StructureDocument(
        blocks=(
            StructureBlock(
                kind="heading",
                order=0,
                page=1,
                bbox=BOX,
                text_hint=text,
                heading_level=1,
            ),
        )
    )

    candidate_set = build_block_candidate_sets(evidence=evidence, layout=layout, hints=hints)[0]
    best = max(candidate_set.candidates, key=lambda item: item.score)

    assert isinstance(best, BlockCandidate)
    assert best.block_kind == "paragraph"
    assert any(item.block_kind == "heading" for item in candidate_set.candidates)


def test_short_exact_heading_hint_is_deterministic() -> None:
    evidence, layout = _embedded_fixture(((1, "문서 제목", BOX, "heading"),))
    hints = StructureDocument(
        blocks=(
            StructureBlock(
                kind="heading",
                order=0,
                page=1,
                bbox=BOX,
                text_hint="문서 제목",
                heading_level=1,
            ),
        )
    )

    candidate_set = build_block_candidate_sets(
        evidence=evidence,
        layout=layout,
        hints=hints,
    )[0]
    decision = CandidateAdjudicator(None).decide(candidate_set)

    selected = next(
        item
        for item in candidate_set.candidates
        if item.candidate_id == decision.selected_candidate_id
    )
    assert decision.outcome == "selected"
    assert selected.block_kind == "heading"


def test_incidental_hint_overlap_does_not_create_table_candidate() -> None:
    evidence, layout = _embedded_fixture(((1, "제목", BOX, None),))
    hints = StructureDocument(
        blocks=(
            StructureBlock(
                kind="table",
                order=0,
                page=1,
                bbox=SourceBox(left=0.1, bottom=0.69, right=0.8, top=0.71),
                text_hint="",
                table=StructureTable(
                    row_count=1,
                    column_count=1,
                    cells=(StructureCell(0, 1, 0, 1, "", True, None),),
                ),
            ),
        )
    )

    candidate_set = build_block_candidate_sets(
        evidence=evidence,
        layout=layout,
        hints=hints,
    )[0]

    assert not any(item.block_kind == "table" for item in candidate_set.candidates)


def test_list_depth_and_caption_candidates_are_bounded() -> None:
    evidence, layout = _embedded_fixture(
        (
            (1, "  - 하위 항목", BOX, None),
            (1, "그림 1. 처리 구조", SourceBox(left=0.2, bottom=0.2, right=0.7, top=0.24), None),
        )
    )

    candidate_sets = build_block_candidate_sets(
        evidence=evidence,
        layout=layout,
        hints=StructureDocument(blocks=()),
    )
    list_candidate = max(candidate_sets[0].candidates, key=lambda item: item.score)
    caption_candidate = max(candidate_sets[1].candidates, key=lambda item: item.score)

    assert isinstance(list_candidate, BlockCandidate)
    assert (list_candidate.block_kind, list_candidate.list_kind, list_candidate.list_depth) == (
        "list_item",
        "unordered",
        2,
    )
    assert isinstance(caption_candidate, BlockCandidate)
    assert caption_candidate.block_kind == "caption"
    assert all(len(item.candidates) <= 5 for item in candidate_sets)


def test_competing_ocr_hypotheses_become_allowlisted_recognition_candidates() -> None:
    region_id = "region_0000000000000001"
    hypotheses = (
        RecognitionHypothesis(
            hypothesis_id="hyp_0000000000000001",
            region_id=region_id,
            page=1,
            bbox=BOX,
            text="데이터",
            engine="ocr-a",
            confidence=0.83,
        ),
        RecognitionHypothesis(
            hypothesis_id="hyp_0000000000000002",
            region_id=region_id,
            page=1,
            bbox=BOX,
            text="데이타",
            engine="ocr-b",
            confidence=0.78,
        ),
    )
    evidence = ExtractedEvidence(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_OCR,
        page_count=1,
        parser_versions={"fixture": "1"},
        atoms=(),
        hypotheses=hypotheses,
        regions=(
            EvidenceRegion(
                region_id=region_id,
                page=1,
                bbox=BOX,
                hypothesis_ids=tuple(item.hypothesis_id for item in hypotheses),
                authority="ambiguous",
                requires_review=True,
            ),
        ),
    )

    candidate_set = build_recognition_candidate_sets(evidence)[0]

    assert {item.hypothesis_id for item in candidate_set.candidates} == {
        "hyp_0000000000000001",
        "hyp_0000000000000002",
    }
    assert all(isinstance(item, RecognitionCandidate) for item in candidate_set.candidates)
    assert all(item.region_id == region_id for item in candidate_set.candidates)


def test_reading_order_candidates_are_legal_topological_orders() -> None:
    evidence, layout = _embedded_fixture(
        (
            (1, "A", SourceBox(left=0.1, bottom=0.8, right=0.2, top=0.85), None),
            (1, "B", SourceBox(left=0.1, bottom=0.6, right=0.2, top=0.65), None),
            (1, "C", SourceBox(left=0.6, bottom=0.8, right=0.7, top=0.85), None),
            (1, "D", SourceBox(left=0.6, bottom=0.6, right=0.7, top=0.65), None),
        )
    )
    regions = tuple(
        region.model_copy(update={"column": 0 if index < 2 else 1})
        for index, region in enumerate(layout.regions)
    )
    independent_layout = layout.model_copy(
        update={
            "regions": regions,
            "order_edges": (
                ReadingOrderEdge(
                    before_region_id=regions[0].region_id,
                    after_region_id=regions[1].region_id,
                    reason="same_column",
                ),
                ReadingOrderEdge(
                    before_region_id=regions[2].region_id,
                    after_region_id=regions[3].region_id,
                    reason="same_column",
                ),
            ),
        }
    )

    candidate_set = build_reading_order_candidate_set(independent_layout)
    orders = [item.region_ids for item in candidate_set.candidates]

    assert all(isinstance(item, ReadingOrderCandidate) for item in candidate_set.candidates)
    assert 1 < len(orders) <= 5
    assert all(
        order.index(regions[0].region_id) < order.index(regions[1].region_id)
        for order in orders
    )
    assert all(
        order.index(regions[2].region_id) < order.index(regions[3].region_id)
        for order in orders
    )
    repeated = build_reading_order_candidate_set(independent_layout)
    assert candidate_set.model_dump_json() == repeated.model_dump_json()


def test_cross_page_continuation_requires_alignment_and_repeated_header() -> None:
    evidence, layout = _embedded_fixture(
        (
            (1, "표 본문", SourceBox(left=0.1, bottom=0.02, right=0.8, top=0.08), "table"),
            (2, "열 제목", SourceBox(left=0.1, bottom=0.90, right=0.8, top=0.96), "page_header"),
            (2, "표 계속", SourceBox(left=0.1, bottom=0.80, right=0.8, top=0.86), "table"),
        )
    )
    regions = (
        layout.regions[0],
        layout.regions[1].model_copy(update={"repeated_edge": True}),
        layout.regions[2],
    )
    layout = layout.model_copy(update={"regions": regions})

    candidate_set = build_continuation_candidate_sets(layout)[0]
    decisions = {
        (item.continue_previous, item.score)
        for item in candidate_set.candidates
        if isinstance(item, ContinuationCandidate)
    }

    assert decisions == {(True, 0.90), (False, 0.55)}
    assert all(item.previous_region_id == regions[0].region_id for item in candidate_set.candidates)
    assert all(item.current_region_id == regions[2].region_id for item in candidate_set.candidates)
