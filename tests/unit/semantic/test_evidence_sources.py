from __future__ import annotations

import pytest

from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceExtractionMode,
    EvidenceRegion,
    ExtractedEvidence,
    RecognitionHypothesis,
    authoritative_non_whitespace,
)
from ard_ossie.semantic.evidence_sources import resolve_evidence_authority
from ard_ossie.semantic.models import SourceBox

SOURCE_HASH = "b" * 64
EMBEDDED_ATOM_A = "atom_1000000000000001"
EMBEDDED_ATOM_B = "atom_1000000000000002"
EMBEDDED_REGION = "region_1000000000000001"
OCR_REGION = "region_2000000000000001"
OCR_HYPOTHESIS_A = "hyp_2000000000000001"
OCR_HYPOTHESIS_B = "hyp_2000000000000002"
EMBEDDED_BOX = SourceBox(left=0.1, bottom=0.7, right=0.3, top=0.8)
OCR_BOX = SourceBox(left=0.1, bottom=0.1, right=0.4, top=0.2)


def embedded_atom(atom_id: str, ordinal: int, text: str) -> EvidenceAtom:
    return EvidenceAtom(
        atom_id=atom_id,
        ordinal=ordinal,
        page=1,
        bbox=EMBEDDED_BOX,
        text=text,
        kind="character",
        authority="embedded",
        source_object=0,
        source_index=ordinal,
    )


def extracted_with_ocr(
    *,
    ocr_box: SourceBox = OCR_BOX,
    competing: bool = False,
) -> ExtractedEvidence:
    hypotheses = [
        RecognitionHypothesis(
            hypothesis_id=OCR_HYPOTHESIS_A,
            region_id=OCR_REGION,
            page=1,
            bbox=ocr_box,
            text="이미지표",
            engine="docling-easyocr",
            confidence=0.91,
        )
    ]
    if competing:
        hypotheses.append(
            RecognitionHypothesis(
                hypothesis_id=OCR_HYPOTHESIS_B,
                region_id=OCR_REGION,
                page=1,
                bbox=ocr_box,
                text="이미지 표",
                engine="secondary-ocr",
                confidence=0.85,
            )
        )
    return ExtractedEvidence(
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_MIXED,
        page_count=1,
        parser_versions={"fixture": "1"},
        atoms=(
            embedded_atom(EMBEDDED_ATOM_A, 0, "본"),
            embedded_atom(EMBEDDED_ATOM_B, 1, "문"),
        ),
        hypotheses=tuple(hypotheses),
        regions=(
            EvidenceRegion(
                region_id=EMBEDDED_REGION,
                page=1,
                bbox=EMBEDDED_BOX,
                atom_ids=(EMBEDDED_ATOM_A, EMBEDDED_ATOM_B),
                authority="embedded",
            ),
            EvidenceRegion(
                region_id=OCR_REGION,
                page=1,
                bbox=ocr_box,
                hypothesis_ids=tuple(item.hypothesis_id for item in hypotheses),
                authority="ocr",
                requires_review=competing,
            ),
        ),
    )


def test_mixed_page_uses_nonoverlapping_ocr_region_without_duplicate_text() -> None:
    selected = resolve_evidence_authority(extracted_with_ocr())

    assert selected.extraction_mode is EvidenceExtractionMode.PDF_MIXED
    assert [region.authority for region in selected.regions] == ["embedded", "ocr"]
    assert authoritative_non_whitespace(selected) == "본문이미지표"
    assert all(atom.authority == "ocr" for atom in selected.atoms[2:])


def test_overlapping_ocr_region_is_dropped_when_embedded_evidence_is_authoritative() -> None:
    selected = resolve_evidence_authority(extracted_with_ocr(ocr_box=EMBEDDED_BOX))

    assert selected.extraction_mode is EvidenceExtractionMode.PDF_EMBEDDED
    assert len(selected.regions) == 1
    assert authoritative_non_whitespace(selected) == "본문"


def test_selected_ocr_hypothesis_controls_only_the_allowlisted_region() -> None:
    selected = resolve_evidence_authority(
        extracted_with_ocr(competing=True),
        selected_hypotheses={OCR_REGION: OCR_HYPOTHESIS_B},
    )

    assert "".join(atom.text for atom in selected.atoms[2:]) == "이미지 표"
    assert selected.regions[1].requires_review is False


def test_unknown_ocr_hypothesis_selection_is_rejected() -> None:
    with pytest.raises(ValueError, match="EVIDENCE_HYPOTHESIS_SELECTION_INVALID"):
        resolve_evidence_authority(
            extracted_with_ocr(competing=True),
            selected_hypotheses={OCR_REGION: "hyp_ffffffffffffffff"},
        )
