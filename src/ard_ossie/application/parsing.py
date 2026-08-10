from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from ard_ossie.docling_parser import DoclingParser, Evidence, ParsedDocument
from ard_ossie.excel_adapter import DictionaryTable, parse_dictionary
from ard_ossie.ingestion import SourceFile, SourceRole, SourceValidationError
from ard_ossie.models import Sha256, StrictModel
from ard_ossie.ports.filesystem import FileSystemPort


class DocumentParser(Protocol):
    def parse(self, source: SourceFile) -> ParsedDocument: ...


class ParsedDocumentResult(StrictModel):
    parser_kind: Literal["docling"] = "docling"
    role: SourceRole
    source_hash: Sha256
    markdown: str
    evidence: list[Evidence] = Field(default_factory=list)


class ParsedDictionaryResult(StrictModel):
    parser_kind: Literal["cell-preserving-excel"] = "cell-preserving-excel"
    role: Literal[SourceRole.DICTIONARY_EXCEL] = SourceRole.DICTIONARY_EXCEL
    source_hash: Sha256
    tables: list[DictionaryTable]


class ParsingService:
    def __init__(
        self,
        paths: FileSystemPort,
        *,
        parser: DocumentParser | None = None,
    ) -> None:
        self.paths = paths
        self.parser = parser or DoclingParser()

    def parse_product_html(self, path: str | Path) -> ParsedDocumentResult:
        source = self._source(path, SourceRole.PRODUCT_HTML)
        return self._parse_document(source)

    def parse_semantic_document(self, path: str | Path) -> ParsedDocumentResult:
        source = self._source(path, SourceRole.SEMANTIC_DOCUMENT)
        return self._parse_document(source)

    def parse_dictionary_workbook(self, path: str | Path) -> ParsedDictionaryResult:
        source = self._source(path, SourceRole.DICTIONARY_EXCEL)
        parsed = parse_dictionary(source.path, source_hash=source.sha256)
        return ParsedDictionaryResult(source_hash=parsed.source_hash, tables=parsed.tables)

    def _parse_document(self, source: SourceFile) -> ParsedDocumentResult:
        parsed = self.parser.parse(source)
        return ParsedDocumentResult(
            role=parsed.role,
            source_hash=parsed.source_hash,
            markdown=parsed.markdown,
            evidence=parsed.evidence,
        )

    def _source(self, path: str | Path, role: SourceRole) -> SourceFile:
        resolved = self.paths.resolve_read(path)
        if not resolved.is_file():
            raise SourceValidationError("SOURCE_IS_NOT_A_FILE")
        _validate_role_signature(resolved, role)
        return SourceFile(
            role=role,
            path=resolved,
            relative_path=resolved.relative_to(self.paths.root).as_posix(),
            sha256=_sha256(resolved),
            size_bytes=resolved.stat().st_size,
        )


def _validate_role_signature(path: Path, role: SourceRole) -> None:
    suffix = path.suffix.casefold()
    allowed = {
        SourceRole.PRODUCT_HTML: {".html", ".htm"},
        SourceRole.SEMANTIC_DOCUMENT: {".docx", ".pdf"},
        SourceRole.DICTIONARY_EXCEL: {".xlsx"},
    }[role]
    if suffix not in allowed:
        raise SourceValidationError(f"SOURCE_EXTENSION_NOT_ALLOWED: {suffix}")
    with path.open("rb") as stream:
        head = stream.read(8)
    if suffix in {".docx", ".xlsx"} and not head.startswith(b"PK\x03\x04"):
        raise SourceValidationError("SOURCE_SIGNATURE_MISMATCH")
    if suffix == ".pdf" and not head.startswith(b"%PDF"):
        raise SourceValidationError("SOURCE_SIGNATURE_MISMATCH")
    if suffix in {".html", ".htm"} and b"<" not in head:
        raise SourceValidationError("SOURCE_SIGNATURE_MISMATCH")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
