from __future__ import annotations

from typing import Any

from pydantic import Field, JsonValue

from ard_ossie.ingestion import SourceFile, SourceRole
from ard_ossie.models import Sha256, StrictModel


class Evidence(StrictModel):
    source_hash: Sha256
    role: SourceRole
    locator: dict[str, JsonValue]
    excerpt: str | None = None


class ParsedDocument(StrictModel):
    role: SourceRole
    source_hash: Sha256
    markdown: str
    evidence: list[Evidence] = Field(default_factory=list)


class DoclingParser:
    def __init__(self, *, converter: Any | None = None) -> None:
        self._converter = converter

    def parse(self, source: SourceFile) -> ParsedDocument:
        if source.role is SourceRole.DICTIONARY_EXCEL:
            raise ValueError("dictionary Excel is handled by the cell-preserving adapter")

        converter = self._converter or _new_converter()
        result = converter.convert(str(source.path))
        document = result.document
        markdown = document.export_to_markdown()
        evidence = _collect_evidence(document, source)
        if not evidence:
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
        for provenance in provenance_items:
            locator: dict[str, JsonValue] = {
                "item_index": item_index,
                "level": level,
            }
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
