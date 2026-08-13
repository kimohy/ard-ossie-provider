from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import Field, JsonValue

from ard_ossie.ingestion import SourceFile, SourceRole, materialized_source_path
from ard_ossie.models import Sha256, StrictModel
from ard_ossie.semantic.models import SemanticFidelityReport, SemanticStructureRepairRecord

if TYPE_CHECKING:
    from ard_ossie.semantic.repair import SemanticStructureRepairPlanner


class Evidence(StrictModel):
    source_hash: Sha256
    role: SourceRole
    locator: dict[str, JsonValue]
    excerpt: str | None = None


class ParsedDocument(StrictModel):
    role: SourceRole
    source_hash: Sha256
    markdown: str
    semantic_has_content: bool | None = Field(default=None, exclude=True)
    evidence: list[Evidence] = Field(default_factory=list)
    excluded_product_fact_evidence: list[Evidence] = Field(default_factory=list, exclude=True)
    semantic_fidelity: SemanticFidelityReport | None = Field(default=None, exclude=True)
    semantic_repair: SemanticStructureRepairRecord | None = Field(default=None, exclude=True)


class DoclingParser:
    def __init__(
        self,
        *,
        converter: Any | None = None,
        full_page_ocr_converter: Any | None = None,
        structure_repair_planner: SemanticStructureRepairPlanner | None = None,
        trusted_repair_record: SemanticStructureRepairRecord | None = None,
        pdfium: Any | None = None,
    ) -> None:
        self._converter = converter
        self._full_page_ocr_converter = full_page_ocr_converter
        self._structure_repair_planner = structure_repair_planner
        self._trusted_repair_record = trusted_repair_record
        self._pdfium = pdfium

    def parse(self, source: SourceFile) -> ParsedDocument:
        if source.role is SourceRole.DICTIONARY_EXCEL:
            raise ValueError("dictionary Excel is handled by the cell-preserving adapter")
        if source.role is SourceRole.SEMANTIC_DOCUMENT:
            from ard_ossie.semantic.parser import parse_semantic_document

            semantic = parse_semantic_document(
                source,
                converter=self._converter,
                full_page_ocr_converter=self._full_page_ocr_converter,
                repair_planner=self._structure_repair_planner,
                trusted_record=self._trusted_repair_record,
                pdfium=self._pdfium,
            )
            return ParsedDocument(
                role=source.role,
                source_hash=source.sha256,
                markdown=semantic.markdown,
                semantic_has_content=semantic.has_content,
                evidence=list(semantic.evidence),
                semantic_fidelity=semantic.fidelity,
                semantic_repair=semantic.repair_record,
            )

        converter = self._converter or _new_converter()
        with materialized_source_path(source) as private_path:
            result = converter.convert(str(private_path))
        document = result.document
        markdown = document.export_to_markdown()
        evidence = _collect_evidence(document, source)
        evidence, excluded_product_fact_evidence = _partition_product_fact_evidence(
            evidence,
            source.role,
        )
        if not evidence and not excluded_product_fact_evidence:
            evidence = [
                Evidence(
                    source_hash=source.sha256,
                    role=source.role,
                    locator={"document": source.relative_path},
                )
            ]
        return ParsedDocument(
            role=source.role,
            source_hash=source.sha256,
            markdown=markdown,
            evidence=evidence,
            excluded_product_fact_evidence=excluded_product_fact_evidence,
        )


def _new_converter() -> Any:
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def _collect_evidence(document: Any, source: SourceFile) -> list[Evidence]:
    if not hasattr(document, "iterate_items"):
        return []
    collected: list[Evidence] = []
    for item_index, (item, level) in enumerate(document.iterate_items()):
        text = getattr(item, "text", None)
        provenance_items = getattr(item, "prov", None) or []
        base_locator: dict[str, JsonValue] = {
            "document": source.relative_path,
            "item_index": item_index,
            "level": level,
        }
        if not provenance_items and text:
            collected.append(
                Evidence(
                    source_hash=source.sha256,
                    role=source.role,
                    locator=base_locator,
                    excerpt=str(text)[:500],
                )
            )
        for provenance in provenance_items:
            locator = dict(base_locator)
            page_no = getattr(provenance, "page_no", None)
            if page_no is not None:
                locator["page"] = page_no
            bbox = getattr(provenance, "bbox", None)
            if bbox is not None:
                locator["bbox"] = {
                    "left": float(bbox.l),
                    "top": float(bbox.t),
                    "right": float(bbox.r),
                    "bottom": float(bbox.b),
                }
            charspan = getattr(provenance, "charspan", None)
            if charspan is not None:
                locator["charspan"] = list(charspan)
            collected.append(
                Evidence(
                    source_hash=source.sha256,
                    role=source.role,
                    locator=locator,
                    excerpt=str(text)[:500] if text else None,
                )
            )
    return collected


_AI_GENERATED_LABEL = re.compile(
    r"(?:\(\s*)?AI\s*(?:자동\s*생성|generated)(?:\s*\))?",
    re.IGNORECASE,
)


def _partition_product_fact_evidence(
    evidence: list[Evidence],
    role: SourceRole,
) -> tuple[list[Evidence], list[Evidence]]:
    if role is not SourceRole.PRODUCT_HTML:
        return evidence, []

    excluded_positions: set[int] = set()
    for position, item in enumerate(evidence):
        if not item.excerpt or _AI_GENERATED_LABEL.search(item.excerpt) is None:
            continue
        excluded_positions.add(position)
        if position + 1 >= len(evidence):
            continue
        following = evidence[position + 1]
        item_index = item.locator.get("item_index")
        following_index = following.locator.get("item_index")
        item_level = item.locator.get("level")
        following_level = following.locator.get("level")
        if (
            isinstance(item_index, int)
            and not isinstance(item_index, bool)
            and isinstance(item_level, int)
            and not isinstance(item_level, bool)
            and following_index == item_index + 1
            and following.locator.get("document") == item.locator.get("document")
            and following_level in {item_level, item_level + 1}
        ):
            excluded_positions.add(position + 1)

    return (
        [item for position, item in enumerate(evidence) if position not in excluded_positions],
        [item for position, item in enumerate(evidence) if position in excluded_positions],
    )
