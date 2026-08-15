"""Evidence extraction adapters for the candidate semantic PDF pipeline."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from typing import Any

from ard_ossie.ingestion import SourceFile, source_bytes
from ard_ossie.semantic.evidence import (
    AtomId,
    EvidenceAtom,
    EvidenceDocument,
    EvidenceExtractionMode,
    EvidenceRegion,
    ExtractedEvidence,
    HypothesisId,
    RecognitionHypothesis,
    RegionId,
    make_evidence_id,
)
from ard_ossie.semantic.models import SourceBox
from ard_ossie.semantic.sources import (
    SemanticSourceError,
    _normalized_pdf_box,
    _PdfGeometryError,
    _text_object_ranges,
    extract_ocr_native,
)

BASELINE_TOLERANCE = 0.35
OCR_DUPLICATE_OVERLAP = 0.60
OCR_SUPPLEMENT_OVERLAP = 0.20


def extract_pdf_evidence(
    source: SourceFile,
    *,
    pdfium: Any | None = None,
    ocr_document: Any | None = None,
) -> ExtractedEvidence:
    if pdfium is None:
        import pypdfium2

        pdfium = pypdfium2

    atoms: list[EvidenceAtom] = []
    regions: list[EvidenceRegion] = []
    parser_versions = {
        "semantic_parser": "semantic-evidence-v2",
        "pypdfium2": importlib.metadata.version("pypdfium2"),
    }
    try:
        document = pdfium.PdfDocument(source_bytes(source))
        try:
            page_count = len(document)
            if page_count < 1:
                raise SemanticSourceError("SEMANTIC_PDF_EVIDENCE_UNREADABLE")
            for page_index in range(page_count):
                page = document[page_index]
                try:
                    page_box = page.get_bbox()
                    text_page = page.get_textpage()
                    try:
                        page_atoms = _embedded_page_atoms(
                            text_page=text_page,
                            page_box=page_box,
                            page=page_index + 1,
                            source_hash=source.sha256,
                            ordinal_start=len(atoms),
                        )
                    finally:
                        text_page.close()
                finally:
                    page.close()
                atoms.extend(page_atoms)
                regions.extend(_embedded_regions(source.sha256, page_atoms))
        finally:
            document.close()
    except (pdfium.PdfiumError, _PdfGeometryError) as error:
        if ocr_document is None:
            raise SemanticSourceError("SEMANTIC_PDF_EVIDENCE_UNREADABLE") from error
        page_count = len(getattr(ocr_document, "pages", {}))

    hypotheses: list[RecognitionHypothesis] = []
    if ocr_document is not None:
        ocr_regions, hypotheses, ocr_versions = _ocr_evidence(source, ocr_document)
        regions.extend(ocr_regions)
        parser_versions.update(ocr_versions)

    if not atoms and not hypotheses:
        raise SemanticSourceError("SEMANTIC_PDF_EVIDENCE_UNREADABLE")
    extraction_mode = _extraction_mode(bool(atoms), bool(hypotheses))
    return ExtractedEvidence(
        source_hash=source.sha256,
        extraction_mode=extraction_mode,
        page_count=page_count,
        parser_versions=parser_versions,
        atoms=tuple(atoms),
        hypotheses=tuple(hypotheses),
        regions=tuple(regions),
    )


def resolve_evidence_authority(
    extracted: ExtractedEvidence,
    *,
    selected_hypotheses: Mapping[RegionId, HypothesisId] | None = None,
) -> EvidenceDocument:
    selections = dict(selected_hypotheses or {})
    region_catalog = {region.region_id: region for region in extracted.regions}
    hypothesis_catalog = {
        hypothesis.hypothesis_id: hypothesis for hypothesis in extracted.hypotheses
    }
    for region_id, hypothesis_id in selections.items():
        region = region_catalog.get(region_id)
        if region is None or hypothesis_id not in region.hypothesis_ids:
            raise ValueError("EVIDENCE_HYPOTHESIS_SELECTION_INVALID")

    source_atoms = {atom.atom_id: atom for atom in extracted.atoms}
    embedded = {
        region.region_id: region
        for region in extracted.regions
        if region.authority == "embedded" and region.atom_ids
    }
    selected_regions: list[EvidenceRegion] = []
    selected_atoms: dict[AtomId, EvidenceAtom] = dict(source_atoms)
    selected_regions.extend(embedded.values())

    for region in extracted.regions:
        if not region.hypothesis_ids or region.atom_ids:
            continue
        overlap, embedded_region_id = _maximum_embedded_overlap(region, tuple(embedded.values()))
        if overlap >= OCR_DUPLICATE_OVERLAP:
            continue
        if overlap > OCR_SUPPLEMENT_OVERLAP and embedded_region_id is not None:
            current = embedded[embedded_region_id]
            updated = current.model_copy(
                update={
                    "hypothesis_ids": tuple(
                        dict.fromkeys((*current.hypothesis_ids, *region.hypothesis_ids))
                    ),
                    "requires_review": True,
                }
            )
            embedded[embedded_region_id] = updated
            selected_regions = [
                updated if item.region_id == embedded_region_id else item
                for item in selected_regions
            ]
            continue

        chosen_id = selections.get(region.region_id)
        chosen = (
            hypothesis_catalog[chosen_id]
            if chosen_id is not None
            else max(
                (hypothesis_catalog[item] for item in region.hypothesis_ids),
                key=lambda item: (item.confidence, item.hypothesis_id),
            )
        )
        materialized = _materialize_ocr_atoms(
            extracted.source_hash,
            region,
            chosen,
            source_object=len(selected_regions),
        )
        for atom in materialized:
            selected_atoms[atom.atom_id] = atom
        selected_regions.append(
            region.model_copy(
                update={
                    "atom_ids": tuple(atom.atom_id for atom in materialized),
                    "authority": "ocr",
                    "requires_review": len(region.hypothesis_ids) > 1 and chosen_id is None,
                }
            )
        )

    ordered_regions = tuple(sorted(selected_regions, key=_region_order_key))
    ordered_atoms: list[EvidenceAtom] = []
    for region in ordered_regions:
        region_atoms = sorted(
            (selected_atoms[atom_id] for atom_id in region.atom_ids),
            key=lambda item: (item.source_object, item.source_index, item.ordinal),
        )
        ordered_atoms.extend(region_atoms)
    ordered_atoms = [
        atom.model_copy(update={"ordinal": ordinal})
        for ordinal, atom in enumerate(ordered_atoms)
    ]
    referenced_hypotheses = {
        hypothesis_id for region in ordered_regions for hypothesis_id in region.hypothesis_ids
    }
    authorities = {region.authority for region in ordered_regions}
    if authorities == {"embedded"}:
        extraction_mode = EvidenceExtractionMode.PDF_EMBEDDED
    elif authorities == {"ocr"}:
        extraction_mode = EvidenceExtractionMode.PDF_OCR
    else:
        extraction_mode = EvidenceExtractionMode.PDF_MIXED
    return EvidenceDocument(
        source_hash=extracted.source_hash,
        extraction_mode=extraction_mode,
        page_count=extracted.page_count,
        parser_versions=extracted.parser_versions,
        atoms=tuple(ordered_atoms),
        hypotheses=tuple(
            hypothesis
            for hypothesis in extracted.hypotheses
            if hypothesis.hypothesis_id in referenced_hypotheses
        ),
        regions=ordered_regions,
    )


def _embedded_page_atoms(
    *,
    text_page: Any,
    page_box: tuple[float, float, float, float],
    page: int,
    source_hash: str,
    ordinal_start: int,
) -> list[EvidenceAtom]:
    ranges = _text_object_ranges(text_page, text_page.count_chars())
    units: list[tuple[str, int, int]] = []
    for source_object, (start, length) in enumerate(ranges):
        value = text_page.get_text_range(start, length)
        units.extend(
            (character, start + offset, source_object)
            for offset, character in enumerate(value)
        )

    normalized: list[tuple[str, int, int]] = []
    index = 0
    while index < len(units):
        character, source_index, source_object = units[index]
        if character == "\r":
            normalized.append(("\n", source_index, source_object))
            if index + 1 < len(units) and units[index + 1][0] == "\n":
                index += 1
        else:
            normalized.append((character, source_index, source_object))
        index += 1

    atoms: list[EvidenceAtom] = []
    for character, source_index, source_object in normalized:
        if not character:
            continue
        raw_box = text_page.get_charbox(source_index)
        bbox = None if raw_box is None else _normalized_pdf_box(raw_box, page_box)
        ordinal = ordinal_start + len(atoms)
        atoms.append(
            EvidenceAtom(
                atom_id=make_evidence_id(
                    "atom",
                    source_hash,
                    page,
                    source_index,
                    source_object,
                    character,
                ),
                ordinal=ordinal,
                page=page,
                bbox=bbox,
                text=character,
                kind=_atom_kind(character),
                authority="embedded",
                source_object=source_object,
                source_index=source_index,
            )
        )
    return atoms


def _embedded_regions(source_hash: str, atoms: list[EvidenceAtom]) -> list[EvidenceRegion]:
    groups: list[list[EvidenceAtom]] = []
    current: list[EvidenceAtom] = []
    last_visible: EvidenceAtom | None = None
    for atom in atoms:
        if (
            current
            and atom.kind != "line_break"
            and last_visible is not None
            and not _same_baseline(last_visible.bbox, atom.bbox)
        ):
            groups.append(current)
            current = []
        current.append(atom)
        if atom.kind == "line_break":
            groups.append(current)
            current = []
            last_visible = None
        elif atom.bbox is not None:
            last_visible = atom
    if current:
        groups.append(current)

    result: list[EvidenceRegion] = []
    for group in groups:
        bbox = _union_boxes([atom.bbox for atom in group if atom.bbox is not None])
        if bbox is None or not any(not atom.text.isspace() for atom in group):
            if result:
                previous = result[-1]
                result[-1] = previous.model_copy(
                    update={"atom_ids": (*previous.atom_ids, *(atom.atom_id for atom in group))}
                )
            continue
        result.append(
            EvidenceRegion(
                region_id=make_evidence_id(
                    "region", source_hash, group[0].page, group[0].atom_id, group[-1].atom_id
                ),
                page=group[0].page,
                bbox=bbox,
                atom_ids=tuple(atom.atom_id for atom in group),
                authority="embedded",
            )
        )
    return result


def _ocr_evidence(
    source: SourceFile,
    document: Any,
) -> tuple[list[EvidenceRegion], list[RecognitionHypothesis], dict[str, str]]:
    native = extract_ocr_native(source, document)
    regions: list[EvidenceRegion] = []
    hypotheses: list[RecognitionHypothesis] = []
    for span in native.spans:
        if span.page is None or span.bbox is None or not span.text.strip():
            continue
        region_id = make_evidence_id("region", source.sha256, "ocr", span.page, span.ordinal)
        hypothesis_id = make_evidence_id("hyp", source.sha256, region_id, "docling", span.text_hash)
        hypotheses.append(
            RecognitionHypothesis(
                hypothesis_id=hypothesis_id,
                region_id=region_id,
                page=span.page,
                bbox=span.bbox,
                text=span.text,
                engine="docling-ocr",
                confidence=1.0,
            )
        )
        regions.append(
            EvidenceRegion(
                region_id=region_id,
                page=span.page,
                bbox=span.bbox,
                hypothesis_ids=(hypothesis_id,),
                authority="ocr",
            )
        )
    return regions, hypotheses, native.parser_versions


def _materialize_ocr_atoms(
    source_hash: str,
    region: EvidenceRegion,
    hypothesis: RecognitionHypothesis,
    *,
    source_object: int,
) -> tuple[EvidenceAtom, ...]:
    text = hypothesis.text.replace("\r\n", "\n").replace("\r", "\n")
    return tuple(
        EvidenceAtom(
            atom_id=make_evidence_id(
                "atom", source_hash, region.region_id, hypothesis.hypothesis_id, source_index
            ),
            ordinal=source_index,
            page=region.page,
            bbox=region.bbox,
            text=character,
            kind=_atom_kind(character),
            authority="ocr",
            source_object=source_object,
            source_index=source_index,
            confidence=hypothesis.confidence,
        )
        for source_index, character in enumerate(text)
    )


def _atom_kind(character: str) -> str:
    if character == "\n":
        return "line_break"
    if character.isspace():
        return "whitespace"
    return "character"


def _same_baseline(first: SourceBox | None, second: SourceBox | None) -> bool:
    if first is None or second is None:
        return True
    overlap = max(0.0, min(first.top, second.top) - max(first.bottom, second.bottom))
    minimum_height = min(first.top - first.bottom, second.top - second.bottom)
    return minimum_height > 0 and overlap / minimum_height >= BASELINE_TOLERANCE


def _union_boxes(boxes: list[SourceBox]) -> SourceBox | None:
    if not boxes:
        return None
    return SourceBox(
        left=min(box.left for box in boxes),
        bottom=min(box.bottom for box in boxes),
        right=max(box.right for box in boxes),
        top=max(box.top for box in boxes),
    )


def _maximum_embedded_overlap(
    region: EvidenceRegion,
    embedded: tuple[EvidenceRegion, ...],
) -> tuple[float, RegionId | None]:
    matches = [
        (_box_overlap_fraction(region.bbox, candidate.bbox), candidate.region_id)
        for candidate in embedded
        if candidate.page == region.page
    ]
    return max(matches, default=(0.0, None), key=lambda item: (item[0], item[1] or ""))


def _box_overlap_fraction(subject: SourceBox, other: SourceBox) -> float:
    width = max(0.0, min(subject.right, other.right) - max(subject.left, other.left))
    height = max(0.0, min(subject.top, other.top) - max(subject.bottom, other.bottom))
    subject_area = (subject.right - subject.left) * (subject.top - subject.bottom)
    return 0.0 if subject_area <= 0 else width * height / subject_area


def _region_order_key(region: EvidenceRegion) -> tuple[int, float, float, str]:
    return region.page, -region.bbox.top, region.bbox.left, region.region_id


def _extraction_mode(
    has_embedded: bool,
    has_ocr: bool,
) -> EvidenceExtractionMode:
    if has_embedded and has_ocr:
        return EvidenceExtractionMode.PDF_MIXED
    if has_embedded:
        return EvidenceExtractionMode.PDF_EMBEDDED
    return EvidenceExtractionMode.PDF_OCR
