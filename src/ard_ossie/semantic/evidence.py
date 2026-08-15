"""Immutable source evidence for the candidate semantic PDF pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ard_ossie.canonical import canonical_hash
from ard_ossie.models import Sha256
from ard_ossie.semantic.models import ImmutableStrictModel, SourceBox

AtomId = Annotated[str, StringConstraints(pattern=r"^atom_[0-9a-f]{16}$")]
RegionId = Annotated[str, StringConstraints(pattern=r"^region_[0-9a-f]{16}$")]
HypothesisId = Annotated[str, StringConstraints(pattern=r"^hyp_[0-9a-f]{16}$")]


class EvidenceExtractionMode(StrEnum):
    PDF_EMBEDDED = "pdf_embedded"
    PDF_OCR = "pdf_ocr"
    PDF_MIXED = "pdf_mixed"


class EvidenceAtom(ImmutableStrictModel):
    atom_id: AtomId
    ordinal: int = Field(ge=0)
    page: int = Field(ge=1)
    bbox: SourceBox | None = None
    text: str = Field(min_length=1, max_length=2)
    kind: Literal["character", "whitespace", "line_break"]
    authority: Literal["embedded", "ocr"]
    source_object: int = Field(ge=0)
    source_index: int = Field(ge=0)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_kind(self) -> EvidenceAtom:
        valid = (
            (self.kind == "line_break" and self.text == "\n")
            or (
                self.kind == "whitespace"
                and self.text != "\n"
                and self.text.isspace()
            )
            or (self.kind == "character" and not any(value.isspace() for value in self.text))
        )
        if not valid:
            raise ValueError("EVIDENCE_ATOM_KIND_INVALID")
        return self


class RecognitionHypothesis(ImmutableStrictModel):
    hypothesis_id: HypothesisId
    region_id: RegionId
    page: int = Field(ge=1)
    bbox: SourceBox
    text: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class EvidenceRegion(ImmutableStrictModel):
    region_id: RegionId
    page: int = Field(ge=1)
    bbox: SourceBox
    atom_ids: tuple[AtomId, ...] = ()
    hypothesis_ids: tuple[HypothesisId, ...] = ()
    authority: Literal["embedded", "ocr", "ambiguous"]
    requires_review: bool = False

    @model_validator(mode="after")
    def validate_authority(self) -> EvidenceRegion:
        if self.authority == "ambiguous" and not self.requires_review:
            raise ValueError("EVIDENCE_REGION_REVIEW_REQUIRED")
        if not self.atom_ids and not self.hypothesis_ids:
            raise ValueError("EVIDENCE_REGION_EMPTY")
        return self


class ExtractedEvidence(ImmutableStrictModel):
    schema_version: Literal["semantic-evidence-v2"] = "semantic-evidence-v2"
    source_hash: Sha256
    extraction_mode: EvidenceExtractionMode
    page_count: int = Field(ge=1)
    parser_versions: dict[str, str]
    atoms: tuple[EvidenceAtom, ...]
    hypotheses: tuple[RecognitionHypothesis, ...] = ()
    regions: tuple[EvidenceRegion, ...]

    @model_validator(mode="after")
    def validate_evidence(self) -> ExtractedEvidence:
        _validate_evidence(self, require_resolved=False)
        return self


class EvidenceDocument(ImmutableStrictModel):
    schema_version: Literal["semantic-evidence-v2"] = "semantic-evidence-v2"
    source_hash: Sha256
    extraction_mode: EvidenceExtractionMode
    page_count: int = Field(ge=1)
    parser_versions: dict[str, str]
    atoms: tuple[EvidenceAtom, ...]
    hypotheses: tuple[RecognitionHypothesis, ...] = ()
    regions: tuple[EvidenceRegion, ...]

    @model_validator(mode="after")
    def validate_evidence(self) -> EvidenceDocument:
        _validate_evidence(self, require_resolved=True)
        return self


def make_evidence_id(
    prefix: Literal["atom", "region", "hyp"],
    *parts: object,
) -> str:
    digest = canonical_hash([str(part) for part in parts])
    return f"{prefix}_{digest[:16]}"


def authoritative_non_whitespace(document: EvidenceDocument) -> str:
    return "".join(
        atom.text
        for atom in sorted(document.atoms, key=lambda item: item.ordinal)
        if not atom.text.isspace()
    )


def _validate_evidence(
    evidence: ExtractedEvidence | EvidenceDocument,
    *,
    require_resolved: bool,
) -> None:
    atom_ids = [atom.atom_id for atom in evidence.atoms]
    ordinals = [atom.ordinal for atom in evidence.atoms]
    region_ids = [region.region_id for region in evidence.regions]
    hypothesis_ids = [hypothesis.hypothesis_id for hypothesis in evidence.hypotheses]
    if len(atom_ids) != len(set(atom_ids)) or len(ordinals) != len(set(ordinals)):
        raise ValueError("EVIDENCE_ATOMS_NOT_UNIQUE")
    if len(region_ids) != len(set(region_ids)):
        raise ValueError("EVIDENCE_REGIONS_NOT_UNIQUE")
    if len(hypothesis_ids) != len(set(hypothesis_ids)):
        raise ValueError("EVIDENCE_HYPOTHESES_NOT_UNIQUE")
    if any(atom.page > evidence.page_count for atom in evidence.atoms):
        raise ValueError("EVIDENCE_ATOM_PAGE_INVALID")
    if any(region.page > evidence.page_count for region in evidence.regions):
        raise ValueError("EVIDENCE_REGION_PAGE_INVALID")
    if any(hypothesis.page > evidence.page_count for hypothesis in evidence.hypotheses):
        raise ValueError("EVIDENCE_HYPOTHESIS_PAGE_INVALID")

    atom_catalog = set(atom_ids)
    hypothesis_catalog = {item.hypothesis_id: item for item in evidence.hypotheses}
    region_catalog = set(region_ids)
    ownership: list[str] = []
    for item in evidence.regions:
        if require_resolved and item.authority == "ambiguous":
            raise ValueError("EVIDENCE_REGION_AUTHORITY_UNRESOLVED")
        if require_resolved and not item.atom_ids:
            raise ValueError("EVIDENCE_REGION_ATOMS_REQUIRED")
        if not set(item.atom_ids).issubset(atom_catalog):
            raise ValueError("EVIDENCE_REGION_ATOM_UNKNOWN")
        if not set(item.hypothesis_ids).issubset(hypothesis_catalog):
            raise ValueError("EVIDENCE_REGION_HYPOTHESIS_UNKNOWN")
        ownership.extend(item.atom_ids)
        for hypothesis_id in item.hypothesis_ids:
            hypothesis = hypothesis_catalog[hypothesis_id]
            if hypothesis.region_id != item.region_id or hypothesis.page != item.page:
                raise ValueError("EVIDENCE_HYPOTHESIS_REGION_MISMATCH")

    if any(item.region_id not in region_catalog for item in evidence.hypotheses):
        raise ValueError("EVIDENCE_HYPOTHESIS_REGION_UNKNOWN")
    if len(ownership) != len(set(ownership)):
        raise ValueError("EVIDENCE_ATOM_OWNERSHIP_NOT_UNIQUE")
    if set(ownership) != atom_catalog:
        raise ValueError("EVIDENCE_ATOM_OWNERSHIP_INCOMPLETE")
