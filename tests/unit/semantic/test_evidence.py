from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from ard_ossie.semantic.evidence import (
    EvidenceAtom,
    EvidenceDocument,
    EvidenceExtractionMode,
    EvidenceRegion,
    ExtractedEvidence,
    RecognitionHypothesis,
    authoritative_non_whitespace,
    make_evidence_id,
)
from ard_ossie.semantic.models import SourceBox

SOURCE_HASH = "a" * 64
ATOM_A = "atom_0000000000000001"
ATOM_B = "atom_0000000000000002"
ATOM_C = "atom_0000000000000003"
REGION_A = "region_0000000000000001"
REGION_B = "region_0000000000000002"
HYPOTHESIS_A = "hyp_0000000000000001"
BOX = SourceBox(left=0.1, bottom=0.1, right=0.2, top=0.2)


def atom(
    atom_id: str,
    ordinal: int,
    text: str,
    *,
    kind: str = "character",
) -> EvidenceAtom:
    return EvidenceAtom(
        atom_id=atom_id,
        ordinal=ordinal,
        page=1,
        bbox=None if kind == "line_break" else BOX,
        text=text,
        kind=kind,
        authority="embedded",
        source_object=0,
        source_index=ordinal,
    )


def region(
    region_id: str,
    *,
    atom_ids: tuple[str, ...] = (),
    hypothesis_ids: tuple[str, ...] = (),
    authority: str = "embedded",
    requires_review: bool = False,
) -> EvidenceRegion:
    return EvidenceRegion(
        region_id=region_id,
        page=1,
        bbox=BOX,
        atom_ids=atom_ids,
        hypothesis_ids=hypothesis_ids,
        authority=authority,
        requires_review=requires_review,
    )


def document(
    *,
    atoms: tuple[EvidenceAtom, ...],
    regions: tuple[EvidenceRegion, ...],
) -> EvidenceDocument:
    return EvidenceDocument(
        schema_version="semantic-evidence-v2",
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
        page_count=1,
        parser_versions={"semantic_parser": "semantic-evidence-v2"},
        atoms=atoms,
        regions=regions,
    )


def test_evidence_document_rejects_duplicate_atom_ownership() -> None:
    source_atom = atom(ATOM_A, 0, "가")

    with pytest.raises(ValidationError, match="EVIDENCE_ATOM_OWNERSHIP_NOT_UNIQUE"):
        document(
            atoms=(source_atom,),
            regions=(
                region(REGION_A, atom_ids=(ATOM_A,)),
                region(REGION_B, atom_ids=(ATOM_A,)),
            ),
        )


def test_evidence_document_rejects_unknown_atom_reference() -> None:
    with pytest.raises(ValidationError, match="EVIDENCE_REGION_ATOM_UNKNOWN"):
        document(
            atoms=(atom(ATOM_A, 0, "가"),),
            regions=(region(REGION_A, atom_ids=(ATOM_B,)),),
        )


def test_extracted_evidence_allows_ambiguous_region_but_document_does_not() -> None:
    hypothesis = RecognitionHypothesis(
        hypothesis_id=HYPOTHESIS_A,
        region_id=REGION_A,
        page=1,
        bbox=BOX,
        text="데이터",
        engine="fixture-ocr",
        confidence=0.75,
    )
    ambiguous = region(
        REGION_A,
        hypothesis_ids=(HYPOTHESIS_A,),
        authority="ambiguous",
        requires_review=True,
    )

    extracted = ExtractedEvidence(
        schema_version="semantic-evidence-v2",
        source_hash=SOURCE_HASH,
        extraction_mode=EvidenceExtractionMode.PDF_OCR,
        page_count=1,
        parser_versions={"fixture-ocr": "1"},
        atoms=(),
        hypotheses=(hypothesis,),
        regions=(ambiguous,),
    )

    assert extracted.regions == (ambiguous,)
    with pytest.raises(ValidationError, match="EVIDENCE_REGION_AUTHORITY_UNRESOLVED"):
        EvidenceDocument.model_validate(extracted.model_dump())


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        pytest.param(" ", "character", id="whitespace-as-character"),
        pytest.param("가", "whitespace", id="character-as-whitespace"),
        pytest.param("\t", "line_break", id="tab-as-line-break"),
        pytest.param("\n", "whitespace", id="line-break-as-whitespace"),
    ],
)
def test_evidence_atom_kind_must_match_text(text: str, kind: str) -> None:
    with pytest.raises(ValidationError, match="EVIDENCE_ATOM_KIND_INVALID"):
        atom(ATOM_A, 0, text, kind=kind)


def test_authoritative_non_whitespace_preserves_exact_code_points() -> None:
    evidence = document(
        atoms=(
            atom(ATOM_A, 0, "데"),
            atom(ATOM_B, 1, " ", kind="whitespace"),
            atom(ATOM_C, 2, "이"),
            atom("atom_0000000000000004", 3, "\n", kind="line_break"),
            atom("atom_0000000000000005", 4, "터"),
        ),
        regions=(
            region(
                REGION_A,
                atom_ids=(
                    ATOM_A,
                    ATOM_B,
                    ATOM_C,
                    "atom_0000000000000004",
                    "atom_0000000000000005",
                ),
            ),
        ),
    )

    assert authoritative_non_whitespace(evidence) == "데이터"


def test_make_evidence_id_is_stable_namespaced_and_content_sensitive() -> None:
    first = make_evidence_id("atom", SOURCE_HASH, 1, 7)
    repeated = make_evidence_id("atom", SOURCE_HASH, 1, 7)
    changed = make_evidence_id("atom", SOURCE_HASH, 1, 8)
    region_id = make_evidence_id("region", SOURCE_HASH, 1, 7)

    assert first == repeated
    assert first != changed
    assert first.removeprefix("atom_") == region_id.removeprefix("region_")
    assert re.fullmatch(r"atom_[0-9a-f]{16}", first)

