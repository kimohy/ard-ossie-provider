"""End-to-end candidate semantic PDF pipeline with explicit publication modes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from ard_ossie.canonical import canonical_hash
from ard_ossie.ingestion import SourceFile
from ard_ossie.llm.contracts import LLMProvider
from ard_ossie.semantic.adjudication import (
    CandidateAdjudicator,
    DecisionRecord,
    DecisionReport,
)
from ard_ossie.semantic.candidates import (
    BlockCandidate,
    CandidateSet,
    RecognitionCandidate,
)
from ard_ossie.semantic.canonical import (
    CanonicalSemanticDocument,
    SemanticPipelineStatus,
    SemanticValidationReport,
    assemble_canonical,
)
from ard_ossie.semantic.evidence import (
    EvidenceDocument,
    EvidenceExtractionMode,
    ExtractedEvidence,
)
from ard_ossie.semantic.evidence_sources import (
    extract_pdf_evidence,
    resolve_evidence_authority,
)
from ard_ossie.semantic.layout import normalize_layout
from ard_ossie.semantic.models import (
    DegradedBlockAudit,
    ExtractionMode,
    RemovedElementAudit,
    SemanticFidelityReport,
    SpanProvenanceAudit,
    TableFidelityResult,
)
from ard_ossie.semantic.render import render_canonical_markdown
from ard_ossie.semantic.spacing import (
    KiwiSpacingScorer,
    KoreanSpacingScorer,
    build_spacing_candidate_set,
)
from ard_ossie.semantic.structure import StructureDocument
from ard_ossie.semantic.structure_candidates import (
    build_block_candidate_sets,
    build_continuation_candidate_sets,
    build_reading_order_candidate_set,
    build_recognition_candidate_sets,
    build_table_candidate_set,
)


class SemanticPipelineMode(StrEnum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class SemanticDiffSummary:
    changed: bool
    legacy_hash: str
    canonical_hash: str


@dataclass(frozen=True)
class SemanticPipelineResult:
    mode: SemanticPipelineMode
    markdown: str
    canonical_markdown: str
    canonical: CanonicalSemanticDocument
    validation: SemanticValidationReport
    decisions: DecisionReport
    candidate_sets: tuple[CandidateSet, ...]
    evidence: EvidenceDocument
    semantic_diff: SemanticDiffSummary


def parse_semantic_pdf_v2(
    source: SourceFile,
    *,
    hints: StructureDocument,
    mode: SemanticPipelineMode | str = SemanticPipelineMode.SHADOW,
    legacy_markdown: str = "",
    provider: LLMProvider | None = None,
    trusted_decisions: tuple[DecisionRecord, ...] = (),
    pdfium: object | None = None,
    ocr_document: object | None = None,
    extracted_evidence: ExtractedEvidence | None = None,
    spacing_scorer: KoreanSpacingScorer | None = None,
) -> SemanticPipelineResult:
    active_mode = SemanticPipelineMode(mode)
    extracted = extracted_evidence or extract_pdf_evidence(
        source,
        pdfium=pdfium,
        ocr_document=ocr_document,
    )
    if extracted.source_hash != source.sha256:
        raise ValueError("SEMANTIC_V2_SOURCE_HASH_MISMATCH")
    adjudicator = CandidateAdjudicator(provider, trusted=trusted_decisions)
    evidence_hash = canonical_hash(extracted.model_dump(mode="json"))

    recognition_sets = build_recognition_candidate_sets(extracted)
    recognition_decisions = tuple(
        adjudicator.decide(candidate_set, evidence_hash=evidence_hash)
        for candidate_set in recognition_sets
    )
    recognition_by_set = {
        item.candidate_set_id: item for item in recognition_decisions
    }
    selections: dict[str, str] = {}
    for candidate_set in recognition_sets:
        decision = recognition_by_set[candidate_set.candidate_set_id]
        selected = next(
            (
                candidate
                for candidate in candidate_set.candidates
                if candidate.candidate_id == decision.selected_candidate_id
            ),
            max(candidate_set.candidates, key=lambda item: (item.score, item.candidate_id)),
        )
        if isinstance(selected, RecognitionCandidate):
            selections[selected.region_id] = selected.hypothesis_id
    evidence = resolve_evidence_authority(
        extracted,
        selected_hypotheses=selections,
    )
    layout = normalize_layout(evidence, hints)
    scorer = spacing_scorer or KiwiSpacingScorer()

    spacing_sets = tuple(
        build_spacing_candidate_set(
            region=region,
            evidence=evidence,
            layout=layout,
            scorer=scorer,
        )
        for region in layout.regions
        if any(not _atom_text(evidence, atom_id).isspace() for atom_id in region.atom_ids)
        and not region.repeated_edge
    )
    block_sets = build_block_candidate_sets(evidence=evidence, layout=layout, hints=hints)
    reading_set = build_reading_order_candidate_set(layout)
    continuation_sets = build_continuation_candidate_sets(layout)
    table_sets: list[CandidateSet] = []
    regions = {item.region_id: item for item in layout.regions}
    for block_set in block_sets:
        if any(
            isinstance(candidate, BlockCandidate) and candidate.block_kind == "table"
            for candidate in block_set.candidates
        ):
            table_sets.append(
                build_table_candidate_set(
                    regions[block_set.region_id],
                    evidence,
                    layout,
                    hints,
                )
            )

    remaining_sets = (
        *spacing_sets,
        *block_sets,
        *table_sets,
        reading_set,
        *continuation_sets,
    )
    remaining_decisions = tuple(
        adjudicator.decide(
            candidate_set,
            evidence_hash=canonical_hash(evidence.model_dump(mode="json")),
        )
        for candidate_set in remaining_sets
    )
    candidate_sets = (*recognition_sets, *remaining_sets)
    decisions = (*recognition_decisions, *remaining_decisions)
    canonical, validation = assemble_canonical(
        evidence=evidence,
        layout=layout,
        candidate_sets=remaining_sets,
        decisions=decisions,
    )
    canonical_markdown = render_canonical_markdown(canonical)
    published = (
        canonical_markdown
        if active_mode is SemanticPipelineMode.CANDIDATE
        else legacy_markdown
    )
    return SemanticPipelineResult(
        mode=active_mode,
        markdown=published,
        canonical_markdown=canonical_markdown,
        canonical=canonical,
        validation=validation,
        decisions=DecisionReport(source_hash=evidence.source_hash, decisions=decisions),
        candidate_sets=candidate_sets,
        evidence=evidence,
        semantic_diff=SemanticDiffSummary(
            changed=legacy_markdown != canonical_markdown,
            legacy_hash=hashlib.sha256(legacy_markdown.encode()).hexdigest(),
            canonical_hash=hashlib.sha256(canonical_markdown.encode()).hexdigest(),
        ),
    )


def canonical_fidelity_report(
    evidence: EvidenceDocument,
    document: CanonicalSemanticDocument,
    validation: SemanticValidationReport,
) -> SemanticFidelityReport:
    non_whitespace = [atom for atom in evidence.atoms if not atom.text.isspace()]
    excluded_ids = {
        atom_id
        for exclusion in document.excluded_evidence
        for atom_id in exclusion.atom_ids
        if not _atom_text(evidence, atom_id).isspace()
    }
    total = len(non_whitespace)
    unmatched = validation.missing_atom_count
    duplicated = validation.duplicate_atom_count
    if validation.status is SemanticPipelineStatus.FAILED and not (unmatched or duplicated):
        unmatched = min(1, total)
    preserved = max(0, total - len(excluded_ids) - unmatched)

    degraded_blocks: list[DegradedBlockAudit] = []
    region_atoms = {region.region_id: region.atom_ids for region in evidence.regions}
    atom_catalog = {atom.atom_id: atom for atom in evidence.atoms}
    for order, decision in enumerate(
        item for item in document.decisions if item.outcome == "review_required"
    ):
        atom_ids = region_atoms.get(decision.region_id, ())
        if not atom_ids:
            continue
        text = "".join(atom_catalog[item].text for item in atom_ids)
        first = atom_catalog[atom_ids[0]]
        degraded_blocks.append(
            DegradedBlockAudit(
                order=order,
                reason=(
                    "provider_unavailable"
                    if decision.source == "unavailable"
                    else "repair_rejected"
                ),
                spans=[
                    SpanProvenanceAudit(
                        page=first.page,
                        bbox=first.bbox,
                        text_hash=hashlib.sha256(text.encode()).hexdigest(),
                    )
                ],
            )
        )
    warning_codes = list(
        dict.fromkeys(
            code
            for decision in document.decisions
            for code in decision.validation_codes
        )
    )
    extraction_mode = (
        ExtractionMode.PDF_EMBEDDED
        if evidence.extraction_mode is EvidenceExtractionMode.PDF_EMBEDDED
        else ExtractionMode.OCR
    )
    if validation.status is SemanticPipelineStatus.FAILED:
        status = "FAIL"
    elif validation.status is SemanticPipelineStatus.REVIEW_REQUIRED or (
        extraction_mode is ExtractionMode.OCR
    ):
        status = "WARN"
    else:
        status = "PASS"
    tables = [block for block in document.blocks if block.kind == "table"]
    removed_elements = [
        RemovedElementAudit(
            kind={
                "repeated_page_header": "page_header",
                "repeated_page_footer": "page_footer",
                "page_number": "page_number",
            }[item.reason],
            page=atom_catalog[item.atom_ids[0]].page,
            bbox=atom_catalog[item.atom_ids[0]].bbox,
            text_hash=hashlib.sha256(
                "".join(atom_catalog[atom_id].text for atom_id in item.atom_ids).encode()
            ).hexdigest(),
        )
        for item in document.excluded_evidence
    ]
    return SemanticFidelityReport(
        source_hash=evidence.source_hash,
        extraction_mode=extraction_mode,
        page_count=evidence.page_count,
        parser_versions=evidence.parser_versions,
        status=status,
        heading_count=sum(block.kind == "heading" for block in document.blocks),
        paragraph_count=sum(
            block.kind in {"paragraph", "caption"} for block in document.blocks
        ),
        list_item_count=sum(block.kind == "list_item" for block in document.blocks),
        table_count=len(tables),
        row_count=sum(block.row_count or 0 for block in tables),
        cell_count=sum(len(block.cells) for block in tables),
        source_span_count=total,
        preserved_span_count=preserved,
        excluded_span_count=len(excluded_ids),
        unmatched_span_count=unmatched,
        duplicated_span_count=duplicated,
        degraded_block_count=len(degraded_blocks),
        source_text_coverage=(
            1.0 if total == 0 else (preserved + len(excluded_ids)) / total
        ),
        removed_elements=removed_elements,
        degraded_blocks=degraded_blocks,
        table_results=[
            TableFidelityResult(
                order=block.order,
                row_count=block.row_count or 0,
                column_count=block.column_count or 0,
                matched_cell_count=sum(bool(cell.atom_ids) for cell in block.cells),
                total_cell_count=len(block.cells),
                status=(
                    "resolved"
                    if validation.status is SemanticPipelineStatus.VERIFIED
                    else "degraded"
                ),
            )
            for block in tables
        ],
        warning_codes=warning_codes,
    )


def _atom_text(evidence: EvidenceDocument, atom_id: str) -> str:
    return next(atom.text for atom in evidence.atoms if atom.atom_id == atom_id)
