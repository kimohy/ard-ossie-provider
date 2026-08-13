from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from ard_ossie.docling_parser import DoclingParser, Evidence, ParsedDocument
from ard_ossie.excel_adapter import DictionaryTable, parse_dictionary
from ard_ossie.ingestion import (
    SourceFile,
    SourceRole,
    SourceValidationError,
    snapshot_source_file,
    source_bytes,
)
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
        parsed = parse_dictionary(
            source.path,
            source_hash=source.sha256,
            source_bytes=source_bytes(source),
        )
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
        _validate_role_extension(resolved, role)
        return snapshot_source_file(
            resolved,
            role=role,
            relative_path=resolved.relative_to(self.paths.root).as_posix(),
        )


def _validate_role_extension(path: Path, role: SourceRole) -> None:
    suffix = path.suffix.casefold()
    allowed = {
        SourceRole.PRODUCT_HTML: {".html", ".htm"},
        SourceRole.SEMANTIC_DOCUMENT: {".docx", ".pdf"},
        SourceRole.DICTIONARY_EXCEL: {".xlsx"},
    }[role]
    if suffix not in allowed:
        raise SourceValidationError(f"SOURCE_EXTENSION_NOT_ALLOWED: {suffix}")
