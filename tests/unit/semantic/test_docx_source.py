from __future__ import annotations

import hashlib
import lzma
import zlib
from collections.abc import Callable
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from ard_ossie.ingestion import SourceFile, SourceRole
from ard_ossie.semantic import sources
from ard_ossie.semantic.models import ExtractionMode
from ard_ossie.semantic.sources import SemanticSourceError, extract_docx_native


def build_structured_docx_source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "semantic.docx"
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>제목</w:t></w:r></w:p>
  <w:p><w:r><w:t>본문  원문 | 값</w:t><w:tab/><w:t>탭</w:t><w:br/><w:t>다음 줄</w:t></w:r></w:p>
  <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
   <w:r><w:t>순서 항목</w:t></w:r></w:p>
  <w:p><w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="2"/></w:numPr></w:pPr>
   <w:r><w:t>글머리 항목</w:t></w:r></w:p>
  <w:tbl>
   <w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
   <w:tr>
    <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
     <w:p><w:r><w:t>병합 머리글</w:t></w:r></w:p></w:tc>
    <w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>
     <w:p><w:r><w:t>세로 값</w:t></w:r></w:p></w:tc>
   </w:tr>
   <w:tr>
    <w:tc><w:p><w:r><w:t>왼쪽</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>가운데</w:t></w:r></w:p></w:tc>
    <w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p><w:r><w:t>중복되면 안 됨</w:t></w:r></w:p></w:tc>
   </w:tr>
  </w:tbl>
  <w:p><w:pPr><w:pStyle w:val="Caption"/></w:pPr><w:r><w:t>표 1. 설명</w:t></w:r></w:p>
  <w:p>
   <w:r><w:drawing><wp:inline><wp:docPr id="1" name="box" descr="대체 설명"/>
    <w:txbxContent><w:p><w:r><w:t>텍스트 상자</w:t></w:r></w:p></w:txbxContent>
   </wp:inline></w:drawing></w:r>
  </w:p>
 </w:body>
</w:document>"""
    numbering_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:abstractNum w:abstractNumId="10"><w:lvl w:ilvl="0">
  <w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>
 <w:abstractNum w:abstractNumId="20"><w:lvl w:ilvl="1">
  <w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>
 <w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>
 <w:num w:numId="2"><w:abstractNumId w:val="20"/></w:num>
</w:numbering>"""
    with ZipFile(path, "w") as package:
        package.writestr("word/document.xml", document_xml)
        package.writestr("word/numbering.xml", numbering_xml)
    return _source(path)


def malformed_docx_source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a ZIP package")
    return _source(path)


def malformed_xml_docx_source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "broken-xml.docx"
    with ZipFile(path, "w") as package:
        package.writestr("word/document.xml", "<w:document")
    return _source(path)


def _source(path: Path) -> SourceFile:
    contents = path.read_bytes()
    return SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path=f"semantic/{path.name}",
        sha256=hashlib.sha256(contents).hexdigest(),
        size_bytes=len(contents),
        snapshot=contents,
    )


def xml_document(body: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
 <w:body>{body}</w:body>
</w:document>"""


def docx_source(
    tmp_path: Path,
    *,
    document: str,
    numbering: str | None = None,
    compression: int = 0,
) -> SourceFile:
    path = tmp_path / "fixture.docx"
    with ZipFile(path, "w", compression=compression) as package:
        package.writestr("word/document.xml", document)
        if numbering is not None:
            package.writestr("word/numbering.xml", numbering)
    return _source(path)


def test_docx_source_preserves_document_order_and_merged_cells(tmp_path: Path) -> None:
    native = extract_docx_native(build_structured_docx_source(tmp_path))

    assert native.extraction_mode is ExtractionMode.DOCX_XML
    assert [group.kind for group in native.groups] == [
        "paragraph",
        "paragraph",
        "list_item",
        "list_item",
        "table",
        "caption",
        "text_box",
        "alt_text",
    ]
    assert "본문  원문 | 값\t탭\n다음 줄" in [span.text for span in native.spans]
    assert [group.list_kind for group in native.groups[2:4]] == ["ordered", "unordered"]
    assert native.groups[3].list_depth == 1
    assert [span.text for span in native.spans].count("세로 값") == 1
    table = native.tables[0]
    assert (table.row_count, table.column_count) == (2, 3)
    assert table.cells[0].start_column == 0
    assert table.cells[0].end_column == 2
    assert any(cell.end_row - cell.start_row == 2 for cell in table.cells)


@pytest.mark.parametrize(
    "source_factory",
    [malformed_docx_source, malformed_xml_docx_source],
)
def test_docx_source_rejects_malformed_package(
    tmp_path: Path, source_factory: Callable[[Path], SourceFile]
) -> None:
    with pytest.raises(SemanticSourceError) as error:
        extract_docx_native(source_factory(tmp_path))

    assert error.value.code == "SEMANTIC_DOCX_UNREADABLE"


def test_docx_table_cells_keep_each_nonempty_paragraph_as_a_distinct_span(tmp_path: Path) -> None:
    source = docx_source(
        tmp_path,
        document=xml_document(
            """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc>
 <w:p><w:r><w:t>첫 문단</w:t></w:r></w:p><w:p/><w:p><w:r><w:t>둘째 문단</w:t></w:r></w:p>
</w:tc></w:tr></w:tbl>"""
        ),
    )

    native = extract_docx_native(source)

    cell = native.tables[0].cells[0]
    assert [native.span_catalog()[span_id].text for span_id in cell.span_ids] == [
        "첫 문단",
        "둘째 문단",
    ]
    assert "첫 문단\n\n둘째 문단" not in [span.text for span in native.spans]


def test_docx_source_uses_the_scanned_snapshot_after_path_replacement(tmp_path: Path) -> None:
    source = docx_source(
        tmp_path,
        document=xml_document("<w:p><w:r><w:t>trusted snapshot</w:t></w:r></w:p>"),
    )
    replacement = tmp_path / "replacement.docx"
    with ZipFile(replacement, "w") as package:
        package.writestr(
            "word/document.xml",
            xml_document("<w:p><w:r><w:t>swapped bytes</w:t></w:r></w:p>"),
        )
    replacement.replace(source.path)

    native = extract_docx_native(source)

    assert [span.text for span in native.spans] == ["trusted snapshot"]
    assert native.source_hash == source.sha256


def test_docx_recursively_unwraps_visible_containers_and_nested_tables_in_order(
    tmp_path: Path,
) -> None:
    source = docx_source(
        tmp_path,
        document=xml_document(
            """
<w:sdt><w:sdtContent><w:p><w:r><w:t>A</w:t></w:r></w:p></w:sdtContent></w:sdt>
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc>
 <w:p><w:r><w:t>before</w:t></w:r></w:p>
 <w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc>
  <w:p><w:r><w:t>nested</w:t></w:r></w:p>
 </w:tc></w:tr></w:tbl>
 <w:p><w:r><w:t>after</w:t></w:r></w:p>
</w:tc></w:tr></w:tbl>
<w:customXml><w:p><w:r><w:t>B</w:t></w:r></w:p></w:customXml>"""
        ),
    )

    native = extract_docx_native(source)

    assert [span.text for span in native.spans] == ["A", "before", "nested", "after", "B"]
    assert len(native.tables) == 2
    assert [span.text for span in native.spans].count("nested") == 1
    assert [group.kind for group in native.groups] == [
        "paragraph",
        "table",
        "table",
        "paragraph",
    ]


def test_docx_header_requires_explicit_repeating_header_row_evidence(tmp_path: Path) -> None:
    headerless = docx_source(
        tmp_path,
        document=xml_document(
            """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid>
 <w:tr><w:tc><w:p><w:r><w:t>ordinary first row</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>"""
        ),
    )
    native = extract_docx_native(headerless)
    assert not any(cell.column_header for cell in native.tables[0].cells)

    explicit_path = tmp_path / "explicit.docx"
    with ZipFile(explicit_path, "w") as package:
        package.writestr(
            "word/document.xml",
            xml_document(
                """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid>
 <w:tr><w:trPr><w:tblHeader/></w:trPr>
  <w:tc><w:p><w:r><w:t>header</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>"""
            ),
        )
    explicit = extract_docx_native(_source(explicit_path))
    assert all(cell.column_header for cell in explicit.tables[0].cells)

    disabled_path = tmp_path / "disabled.docx"
    with ZipFile(disabled_path, "w") as package:
        package.writestr(
            "word/document.xml",
            xml_document(
                """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid>
 <w:tr><w:trPr><w:tblHeader w:val="false"/></w:trPr>
  <w:tc><w:p><w:r><w:t>ordinary</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>"""
            ),
        )
    disabled = extract_docx_native(_source(disabled_path))
    assert not any(cell.column_header for cell in disabled.tables[0].cells)


def test_docx_rejects_table_dimensions_before_large_grid_allocation(tmp_path: Path) -> None:
    source = docx_source(
        tmp_path,
        document=xml_document(
            """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc>
 <w:tcPr><w:gridSpan w:val="257"/></w:tcPr><w:p><w:r><w:t>x</w:t></w:r></w:p>
</w:tc></w:tr></w:tbl>"""
        ),
    )

    with pytest.raises(
        SemanticSourceError, match="SEMANTIC_TABLE_COLUMNS_LIMIT_EXCEEDED"
    ):
        extract_docx_native(source)


def test_docx_table_materializes_skipped_and_declared_blank_grid_regions(tmp_path: Path) -> None:
    source = docx_source(
        tmp_path,
        document=xml_document(
            """
<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
 <w:tr><w:trPr><w:gridAfter w:val="2"/></w:trPr>
  <w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>이전</w:t></w:r></w:p></w:tc>
 </w:tr>
 <w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>
  <w:tc><w:p><w:r><w:t>새 값</w:t></w:r></w:p></w:tc>
 </w:tr>
</w:tbl>"""
        ),
    )

    native = extract_docx_native(source)

    table = native.tables[0]
    assert (table.row_count, table.column_count) == (2, 4)
    assert [
        (cell.start_row, cell.end_row, cell.start_column, cell.end_column)
        for cell in table.cells
    ] == [
        (0, 1, 0, 1),
        (0, 1, 1, 4),
        (1, 2, 0, 1),
        (1, 2, 1, 2),
        (1, 2, 2, 4),
    ]
    assert all(
        not cell.span_ids
        for cell in (table.cells[1], table.cells[2], table.cells[4])
    )
    assert table.cells[0].end_row == 1


def test_docx_table_emits_text_box_and_alternative_text_without_cell_duplication(
    tmp_path: Path,
) -> None:
    source = docx_source(
        tmp_path,
        document=xml_document(
            """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc><w:p>
 <w:r><w:t>바깥 셀</w:t></w:r><w:r><w:drawing><wp:inline>
  <wp:docPr id="1" name="image" descr="셀 설명" title="셀 제목"/>
  <w:txbxContent><w:p><w:r><w:t>셀 상자</w:t></w:r></w:p></w:txbxContent>
 </wp:inline></w:drawing></w:r>
</w:p></w:tc></w:tr></w:tbl>"""
        ),
    )

    native = extract_docx_native(source)

    assert [group.kind for group in native.groups] == ["table", "text_box", "alt_text", "alt_text"]
    assert [span.text for span in native.spans] == ["바깥 셀", "셀 상자", "셀 설명", "셀 제목"]
    assert native.tables[0].cells[0].span_ids == (native.spans[0].span_id,)


def test_docx_numbering_uses_instance_override_and_ignores_num_id_zero(tmp_path: Path) -> None:
    numbering = """<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0">
  <w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>
 <w:num w:numId="1"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="0"><w:lvl>
  <w:numFmt w:val="bullet"/></w:lvl></w:lvlOverride></w:num>
</w:numbering>"""
    source = docx_source(
        tmp_path,
        document=xml_document(
            """
<w:p><w:pPr><w:numPr><w:numId w:val="1"/><w:ilvl w:val="0"/></w:numPr></w:pPr>
 <w:r><w:t>재정의</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:numId w:val="0"/></w:numPr></w:pPr><w:r><w:t>목록 아님</w:t></w:r></w:p>"""
        ),
        numbering=numbering,
    )

    native = extract_docx_native(source)

    assert [group.kind for group in native.groups] == ["list_item", "paragraph"]
    assert native.groups[0].list_kind == "unordered"
    assert native.groups[1].list_kind is None


@pytest.mark.parametrize("failure", [NotImplementedError, RuntimeError])
def test_docx_source_normalizes_fixed_part_read_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    source = docx_source(tmp_path, document=xml_document("<w:p><w:r><w:t>x</w:t></w:r></w:p>"))
    original_read = sources.zipfile.ZipFile.read

    def unreadable_part(self: ZipFile, name: str | object, pwd: bytes | None = None) -> bytes:
        part_name = name if isinstance(name, str) else name.filename
        if part_name == "word/document.xml":
            raise failure("fixed part unavailable")
        return original_read(self, name, pwd)

    monkeypatch.setattr(sources.zipfile.ZipFile, "read", unreadable_part)

    with pytest.raises(SemanticSourceError) as error:
        extract_docx_native(source)

    assert error.value.code == "SEMANTIC_DOCX_UNREADABLE"


@pytest.mark.parametrize("failure", [zlib.error, lzma.LZMAError])
def test_docx_source_normalizes_corrupt_compressed_fixed_part_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    source = docx_source(tmp_path, document=xml_document("<w:p><w:r><w:t>x</w:t></w:r></w:p>"))
    original_read = sources.zipfile.ZipFile.read

    def corrupt_part(self: ZipFile, name: str | object, pwd: bytes | None = None) -> bytes:
        part_name = name if isinstance(name, str) else name.filename
        if part_name == "word/document.xml":
            raise failure("corrupt compressed fixed part")
        return original_read(self, name, pwd)

    monkeypatch.setattr(sources.zipfile.ZipFile, "read", corrupt_part)

    with pytest.raises(SemanticSourceError) as error:
        extract_docx_native(source)

    assert error.value.code == "SEMANTIC_DOCX_UNREADABLE"


def test_docx_source_rejects_compression_ratio_bomb_before_part_read(tmp_path: Path) -> None:
    source = docx_source(
        tmp_path,
        document=xml_document(f"<w:p><w:r><w:t>{'x' * 131_072}</w:t></w:r></w:p>"),
        compression=ZIP_DEFLATED,
    )

    with pytest.raises(SemanticSourceError) as error:
        extract_docx_native(source)

    assert error.value.code == "SEMANTIC_DOCX_UNREADABLE"
