from __future__ import annotations

from ard_ossie.semantic.adjudication import (
    AdjudicationAttempt,
    CandidateAdjudicator,
    DecisionRecord,
)
from ard_ossie.semantic.candidates import (
    BlockCandidate,
    CandidateSet,
    TableCandidate,
    TableCellCandidate,
    make_candidate_id,
    make_candidate_set_id,
    make_spacing_candidate,
)
from ard_ossie.semantic.canonical import (
    CanonicalBlock,
    CanonicalCell,
    CanonicalContinuation,
    CanonicalSemanticDocument,
    SemanticPipelineStatus,
    WhitespaceDisposition,
    assemble_canonical,
    validate_canonical,
)
from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceDocument,
    EvidenceExtractionMode,
    EvidenceRegion,
)
from ard_ossie.semantic.layout import (
    LayoutDocument,
    LayoutLine,
    LayoutRegion,
    ReadingOrderEdge,
)
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.render import render_canonical_markdown

SOURCE_HASH = "1" * 64
REGION_A = "region_0000000000000001"
REGION_B = "region_0000000000000002"
BOX = SourceBox(left=0.1, bottom=0.5, right=0.9, top=0.6)


def _evidence(text: str) -> EvidenceDocument:
    atoms = tuple(
        EvidenceAtom(
            atom_id=f"atom_{index + 1:016x}",
            ordinal=index,
            page=1,
            bbox=SourceBox(
                left=0.1 + index * 0.02,
                bottom=0.5,
                right=0.12 + index * 0.02,
                top=0.55,
            ),
            text=character,
            kind="whitespace" if character.isspace() else "character",
            authority="embedded",
            source_object=0,
            source_index=index,
        )
        for index, character in enumerate(text)
    )
    return EvidenceDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={"fixture": "1"},
        atoms=atoms,
        regions=(
            EvidenceRegion(
                region_id=REGION_A,
                page=1,
                bbox=BOX,
                atom_ids=tuple(atom.atom_id for atom in atoms),
                authority="embedded",
            ),
        ),
    )


def _layout(evidence: EvidenceDocument) -> LayoutDocument:
    line = LayoutLine(
        line_id="line_0000000000000001",
        page=1,
        bbox=BOX,
        atom_ids=tuple(atom.atom_id for atom in evidence.atoms),
        evidence_region_ids=(REGION_A,),
        baseline=0.5,
        median_height=0.05,
    )
    region = LayoutRegion(
        region_id=REGION_A,
        page=1,
        bbox=BOX,
        line_ids=(line.line_id,),
        atom_ids=line.atom_ids,
        evidence_region_ids=(REGION_A,),
        column=0,
    )
    return LayoutDocument(
        source_hash=SOURCE_HASH,
        page_count=1,
        lines=(line,),
        regions=(region,),
        order_edges=(),
    )


def _document(text: str = "데이터 시맨틱") -> tuple[EvidenceDocument, CanonicalSemanticDocument]:
    evidence = _evidence("데이터 시맨틱")
    character_ids = tuple(atom.atom_id for atom in evidence.atoms if not atom.text.isspace())
    whitespace_id = next(atom.atom_id for atom in evidence.atoms if atom.text.isspace())
    document = CanonicalSemanticDocument(
        source_hash=SOURCE_HASH,
        region_order=(REGION_A,),
        blocks=(
            CanonicalBlock(
                block_id="block_0000000000000001",
                order=0,
                region_id=REGION_A,
                page=1,
                kind="paragraph",
                text=text,
                atom_ids=character_ids,
            ),
        ),
        whitespace_dispositions=(
            WhitespaceDisposition(
                atom_id=whitespace_id,
                outcome="retained",
                boundary_state="space",
                decision_id="decision_0000000000000001",
            ),
        ),
    )
    return evidence, document


def test_canonical_validation_detects_one_deleted_embedded_character() -> None:
    evidence, document = _document(text="데이터 시맨")

    report = validate_canonical(evidence, document)

    assert report.status is SemanticPipelineStatus.FAILED
    assert [finding.code for finding in report.findings] == ["INVARIANT_CHARACTER_LOSS"]


def test_character_validation_allows_layout_proven_cross_region_order() -> None:
    evidence = _evidence("AB")
    first, second = evidence.atoms
    evidence = evidence.model_copy(
        update={
            "regions": (
                EvidenceRegion(
                    region_id=REGION_A,
                    page=1,
                    bbox=BOX,
                    atom_ids=(first.atom_id,),
                    authority="embedded",
                ),
                EvidenceRegion(
                    region_id=REGION_B,
                    page=1,
                    bbox=BOX,
                    atom_ids=(second.atom_id,),
                    authority="embedded",
                ),
            )
        }
    )
    layout = LayoutDocument(
        source_hash=SOURCE_HASH,
        page_count=1,
        lines=(
            LayoutLine(
                line_id="line_0000000000000001",
                page=1,
                bbox=BOX,
                atom_ids=(first.atom_id,),
                evidence_region_ids=(REGION_A,),
                baseline=0.5,
                median_height=0.05,
            ),
            LayoutLine(
                line_id="line_0000000000000002",
                page=1,
                bbox=BOX,
                atom_ids=(second.atom_id,),
                evidence_region_ids=(REGION_B,),
                baseline=0.5,
                median_height=0.05,
            ),
        ),
        regions=(
            LayoutRegion(
                region_id=REGION_B,
                page=1,
                bbox=BOX,
                line_ids=("line_0000000000000002",),
                atom_ids=(second.atom_id,),
                evidence_region_ids=(REGION_B,),
                column=0,
            ),
            LayoutRegion(
                region_id=REGION_A,
                page=1,
                bbox=BOX,
                line_ids=("line_0000000000000001",),
                atom_ids=(first.atom_id,),
                evidence_region_ids=(REGION_A,),
                column=0,
            ),
        ),
        order_edges=(
            ReadingOrderEdge(
                before_region_id=REGION_B,
                after_region_id=REGION_A,
                reason="same_column",
            ),
        ),
    )
    document = CanonicalSemanticDocument(
        source_hash=SOURCE_HASH,
        region_order=(REGION_B, REGION_A),
        blocks=(
            CanonicalBlock(
                block_id="block_0000000000000001",
                order=0,
                region_id=REGION_B,
                page=1,
                kind="paragraph",
                text="B",
                atom_ids=(second.atom_id,),
            ),
            CanonicalBlock(
                block_id="block_0000000000000002",
                order=1,
                region_id=REGION_A,
                page=1,
                kind="paragraph",
                text="A",
                atom_ids=(first.atom_id,),
            ),
        ),
    )

    report = validate_canonical(evidence, document, layout=layout)

    assert report.status is SemanticPipelineStatus.VERIFIED


def test_exact_atom_allocation_rejects_duplicate_and_unknown_references() -> None:
    evidence, document = _document()
    block = document.blocks[0]
    duplicate = block.model_copy(update={"atom_ids": (*block.atom_ids, block.atom_ids[0])})
    corrupted = document.model_copy(update={"blocks": (duplicate,)})

    report = validate_canonical(evidence, corrupted)

    assert report.status is SemanticPipelineStatus.FAILED
    assert report.duplicate_atom_count == 1
    assert report.findings[0].code == "INVARIANT_ATOM_ALLOCATION_DUPLICATE"


def test_every_source_whitespace_atom_requires_one_disposition() -> None:
    evidence, document = _document()
    corrupted = document.model_copy(update={"whitespace_dispositions": ()})

    report = validate_canonical(evidence, corrupted)

    assert report.status is SemanticPipelineStatus.FAILED
    assert [finding.code for finding in report.findings] == ["INVARIANT_WHITESPACE_ACCOUNTING"]


def test_validation_counts_recovery_calls_but_not_trusted_reuse() -> None:
    evidence, document = _document()
    selected = "candidate_0000000000000001"
    recovered = DecisionRecord(
        decision_id="decision_0000000000000001",
        request_hash="2" * 64,
        source_hash=SOURCE_HASH,
        evidence_hash="3" * 64,
        candidate_set_id="candidate_set_0000000000000001",
        region_id=REGION_A,
        decision_type="spacing",
        selected_candidate_id=selected,
        outcome="selected",
        source="recovered",
        confidence=0.92,
        provider="test",
        model="test",
        recovery_status="recovered",
        attempts=(
            AdjudicationAttempt(
                attempt_index=1,
                phase="primary",
                request_hash="4" * 64,
                candidate_id=selected,
                confidence=0.70,
                status="low_confidence",
                provider_retry_count=1,
            ),
            AdjudicationAttempt(
                attempt_index=2,
                phase="recovery",
                request_hash="5" * 64,
                candidate_id=selected,
                confidence=0.92,
                status="accepted",
                provider_repair_count=1,
            ),
        ),
        consensus_method="same_candidate",
        consensus_candidate_id=selected,
        recovery_count=1,
    )

    current = validate_canonical(
        evidence,
        document.model_copy(update={"decisions": (recovered,)}),
    )
    cached = validate_canonical(
        evidence,
        document.model_copy(
            update={"decisions": (recovered.model_copy(update={"source": "cache"}),)}
        ),
    )

    assert current.model_call_count == 4
    assert cached.model_call_count == 0


def test_region_without_spacing_candidates_retains_source_whitespace() -> None:
    evidence = _evidence("데이터 시맨틱")

    document, report = assemble_canonical(
        evidence=evidence,
        layout=_layout(evidence),
        candidate_sets=(),
        decisions=(),
    )

    whitespace_id = next(atom.atom_id for atom in evidence.atoms if atom.text.isspace())
    assert document.whitespace_dispositions == (
        WhitespaceDisposition(
            atom_id=whitespace_id,
            outcome="retained",
            boundary_state="space",
            decision_id="decision_0000000000000000",
        ),
    )
    assert report.status is SemanticPipelineStatus.VERIFIED


def test_selected_spacing_is_projected_into_table_cell_text() -> None:
    evidence = _evidence("데 이 터")
    layout = _layout(evidence)
    region = layout.regions[0]
    character_ids = tuple(atom.atom_id for atom in evidence.atoms if not atom.text.isspace())
    source_whitespace = (
        (evidence.atoms[1].atom_id,),
        (evidence.atoms[3].atom_id,),
    )
    spacing = make_spacing_candidate(
        region_id=REGION_A,
        rendered_text="데이터",
        character_sequence="데이터",
        atom_ids=character_ids,
        source_whitespace=source_whitespace,
        score=0.95,
        features={"kiwi": 0.95},
    )
    block = BlockCandidate(
        candidate_id=make_candidate_id("block", REGION_A, "table"),
        region_id=REGION_A,
        block_kind="table",
        atom_ids=region.atom_ids,
        score=0.98,
        features={"table_hint": 1.0},
    )
    cell = TableCellCandidate(
        cell_id="cell_0000000000000001",
        start_row=0,
        end_row=1,
        start_column=0,
        end_column=1,
        atom_ids=region.atom_ids,
        column_header=True,
        rendered_text="데 이 터",
    )
    table = TableCandidate(
        candidate_id=make_candidate_id("table", REGION_A, "single-cell"),
        region_id=REGION_A,
        row_count=1,
        column_count=1,
        cells=(cell,),
        atom_ids=region.atom_ids,
        score=0.98,
        features={"geometry_grid": 1.0},
    )
    candidate_sets = tuple(
        CandidateSet(
            candidate_set_id=make_candidate_set_id(
                SOURCE_HASH,
                REGION_A,
                (candidate.candidate_id,),
            ),
            source_hash=SOURCE_HASH,
            region_id=REGION_A,
            decision_type=decision_type,
            candidates=(candidate,),
        )
        for decision_type, candidate in (
            ("spacing", spacing),
            ("block", block),
            ("table", table),
        )
    )
    adjudicator = CandidateAdjudicator(None)

    document, report = assemble_canonical(
        evidence=evidence,
        layout=layout,
        candidate_sets=candidate_sets,
        decisions=tuple(adjudicator.decide(item) for item in candidate_sets),
    )

    assert document.blocks[0].cells[0].text == "데이터"
    assert report.status is SemanticPipelineStatus.VERIFIED


def test_reading_order_must_satisfy_layout_dag() -> None:
    first_evidence = _evidence("AB")
    atoms = first_evidence.atoms
    evidence = first_evidence.model_copy(
        update={
            "regions": (
                EvidenceRegion(
                    region_id=REGION_A,
                    page=1,
                    bbox=BOX,
                    atom_ids=(atoms[0].atom_id,),
                    authority="embedded",
                ),
                EvidenceRegion(
                    region_id=REGION_B,
                    page=1,
                    bbox=BOX,
                    atom_ids=(atoms[1].atom_id,),
                    authority="embedded",
                ),
            )
        }
    )
    layout = LayoutDocument(
        source_hash=SOURCE_HASH,
        page_count=1,
        lines=(
            LayoutLine(
                line_id="line_0000000000000001",
                page=1,
                bbox=BOX,
                atom_ids=(atoms[0].atom_id,),
                evidence_region_ids=(REGION_A,),
                baseline=0.5,
                median_height=0.05,
            ),
            LayoutLine(
                line_id="line_0000000000000002",
                page=1,
                bbox=BOX,
                atom_ids=(atoms[1].atom_id,),
                evidence_region_ids=(REGION_B,),
                baseline=0.5,
                median_height=0.05,
            ),
        ),
        regions=(
            LayoutRegion(
                region_id=REGION_A,
                page=1,
                bbox=BOX,
                line_ids=("line_0000000000000001",),
                atom_ids=(atoms[0].atom_id,),
                evidence_region_ids=(REGION_A,),
                column=0,
            ),
            LayoutRegion(
                region_id=REGION_B,
                page=1,
                bbox=BOX,
                line_ids=("line_0000000000000002",),
                atom_ids=(atoms[1].atom_id,),
                evidence_region_ids=(REGION_B,),
                column=0,
            ),
        ),
        order_edges=(
            ReadingOrderEdge(
                before_region_id=REGION_A,
                after_region_id=REGION_B,
                reason="same_column",
            ),
        ),
    )
    document = CanonicalSemanticDocument(
        source_hash=SOURCE_HASH,
        region_order=(REGION_B, REGION_A),
        blocks=(
            CanonicalBlock(
                block_id="block_0000000000000001",
                order=0,
                region_id=REGION_B,
                page=1,
                kind="paragraph",
                text="B",
                atom_ids=(atoms[1].atom_id,),
            ),
            CanonicalBlock(
                block_id="block_0000000000000002",
                order=1,
                region_id=REGION_A,
                page=1,
                kind="paragraph",
                text="A",
                atom_ids=(atoms[0].atom_id,),
            ),
        ),
    )

    report = validate_canonical(evidence, document, layout=layout)

    assert any(item.code == "INVARIANT_READING_ORDER" for item in report.findings)


def test_table_topology_and_cross_page_continuation_are_revalidated() -> None:
    evidence = _evidence("AB")
    invalid_table = CanonicalBlock.model_construct(
        block_id="block_0000000000000001",
        order=0,
        region_id=REGION_A,
        page=1,
        kind="table",
        text="AB",
        atom_ids=tuple(atom.atom_id for atom in evidence.atoms),
        heading_level=None,
        list_kind=None,
        list_depth=None,
        cells=(
            CanonicalCell(
                cell_id="cell_0000000000000001",
                start_row=0,
                end_row=1,
                start_column=0,
                end_column=1,
                text="AB",
                atom_ids=tuple(atom.atom_id for atom in evidence.atoms),
            ),
        ),
        row_count=1,
        column_count=2,
        decision_ids=(),
    )
    document = CanonicalSemanticDocument(
        source_hash=SOURCE_HASH,
        region_order=(REGION_A,),
        blocks=(invalid_table,),
        continuations=(
            CanonicalContinuation(
                previous_region_id=REGION_B,
                current_region_id=REGION_A,
                decision_id="decision_0000000000000001",
            ),
        ),
    )

    report = validate_canonical(evidence, document, layout=_layout(evidence))
    codes = {finding.code for finding in report.findings}

    assert "INVARIANT_TABLE_GRID" in codes
    assert "INVARIANT_CONTINUATION" in codes


def test_low_confidence_decision_keeps_publishable_preview_with_review_debt() -> None:
    evidence = _evidence("데이터시맨틱")
    layout = _layout(evidence)
    region = layout.regions[0]
    character_ids = tuple(atom.atom_id for atom in evidence.atoms)
    spacing_candidates = (
        make_spacing_candidate(
            region_id=REGION_A,
            rendered_text="데이터 시맨틱",
            character_sequence="데이터시맨틱",
            atom_ids=character_ids,
            source_whitespace=tuple(() for _ in range(len(character_ids) - 1)),
            score=0.80,
            features={"kiwi": 0.80},
        ),
        make_spacing_candidate(
            region_id=REGION_A,
            rendered_text="데이터시맨틱",
            character_sequence="데이터시맨틱",
            atom_ids=character_ids,
            source_whitespace=tuple(() for _ in range(len(character_ids) - 1)),
            score=0.75,
            features={"dense": 0.75},
        ),
    )
    spacing_set = CandidateSet(
        candidate_set_id=make_candidate_set_id(
            SOURCE_HASH,
            REGION_A,
            tuple(item.candidate_id for item in spacing_candidates),
        ),
        source_hash=SOURCE_HASH,
        region_id=REGION_A,
        decision_type="spacing",
        candidates=spacing_candidates,
    )
    block = BlockCandidate(
        candidate_id=make_candidate_id("block", REGION_A, "paragraph"),
        region_id=REGION_A,
        block_kind="paragraph",
        atom_ids=region.atom_ids,
        score=0.95,
        features={"paragraph": 0.95},
    )
    block_set = CandidateSet(
        candidate_set_id=make_candidate_set_id(SOURCE_HASH, REGION_A, (block.candidate_id,)),
        source_hash=SOURCE_HASH,
        region_id=REGION_A,
        decision_type="block",
        candidates=(block,),
    )
    decisions = (
        CandidateAdjudicator(None).decide(spacing_set),
        CandidateAdjudicator(None).decide(block_set),
    )

    document, report = assemble_canonical(
        evidence=evidence,
        layout=layout,
        candidate_sets=(spacing_set, block_set),
        decisions=decisions,
    )

    assert "데이터 시맨틱" in render_canonical_markdown(document)
    assert report.status is SemanticPipelineStatus.REVIEW_PENDING
    assert report.publishable is True


def test_renderer_escapes_raw_html_and_canonical_hash_is_stable() -> None:
    evidence = _evidence("<b>원문</b>")
    atom_ids = tuple(atom.atom_id for atom in evidence.atoms)
    document = CanonicalSemanticDocument(
        source_hash=SOURCE_HASH,
        region_order=(REGION_A,),
        blocks=(
            CanonicalBlock(
                block_id="block_0000000000000001",
                order=0,
                region_id=REGION_A,
                page=1,
                kind="paragraph",
                text="<b>원문</b>",
                atom_ids=atom_ids,
            ),
        ),
    )

    first = validate_canonical(evidence, document)
    second = validate_canonical(evidence, document)
    rendered = render_canonical_markdown(document)

    assert "<b>" not in rendered
    assert first.canonical_hash == second.canonical_hash
    assert first.status is SemanticPipelineStatus.VERIFIED


def test_canonical_hash_ignores_decision_transport_identifiers() -> None:
    evidence, document = _document()
    changed = document.model_copy(
        update={
            "whitespace_dispositions": (
                document.whitespace_dispositions[0].model_copy(
                    update={"decision_id": "decision_0000000000000002"}
                ),
            )
        }
    )

    first = validate_canonical(evidence, document)
    second = validate_canonical(evidence, changed)

    assert first.canonical_hash == second.canonical_hash
