"""Source-native immutable span catalogs for semantic documents."""

from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata
import lzma
import math
import zipfile
import zlib
from io import BytesIO
from typing import Any
from xml.etree import ElementTree

from ard_ossie.ingestion import SourceFile, source_bytes
from ard_ossie.semantic.models import (
    MAX_TABLE_CELLS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_GRID_AREA,
    MAX_TABLE_ROWS,
    SEMANTIC_PARSER_VERSION,
    ExtractionMode,
    NativeDocument,
    NativeGroup,
    NativeTable,
    NativeTableCell,
    SourceBox,
    SourceSpan,
    make_span_id,
)


class SemanticSourceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
_W = f"{{{_NS['w']}}}"
_WP = f"{{{_NS['wp']}}}"
_DOCX_PARTS = ("word/document.xml", "word/numbering.xml")
_DOCX_MAX_PART_BYTES = 16 * 1024 * 1024
_DOCX_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_DOCX_MAX_COMPRESSION_RATIO = 100


def extract_docx_native(source: SourceFile) -> NativeDocument:
    """Extract an ordered, relationship-free WordprocessingML text catalog."""
    try:
        with zipfile.ZipFile(BytesIO(source_bytes(source))) as package:
            parts = _docx_parts(package)
            document = ElementTree.fromstring(parts["word/document.xml"])
            numbering = _docx_numbering_formats(parts.get("word/numbering.xml"))
        return _docx_native_document(source, document, numbering)
    except (
        OSError,
        EOFError,
        KeyError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        ElementTree.ParseError,
        NotImplementedError,
        RuntimeError,
        zlib.error,
        lzma.LZMAError,
    ) as exc:
        if isinstance(exc, SemanticSourceError):
            raise
        raise SemanticSourceError("SEMANTIC_DOCX_UNREADABLE") from exc


def _docx_parts(package: zipfile.ZipFile) -> dict[str, bytes]:
    infos: dict[str, zipfile.ZipInfo] = {}
    total_bytes = 0
    for name in _DOCX_PARTS:
        try:
            info = package.getinfo(name)
        except KeyError:
            if name == "word/document.xml":
                raise
            continue
        if info.file_size > _DOCX_MAX_PART_BYTES:
            raise ValueError("DOCX_PART_TOO_LARGE")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size > _DOCX_MAX_COMPRESSION_RATIO * info.compress_size
        ):
            raise ValueError("DOCX_PART_COMPRESSION_RATIO_EXCEEDED")
        total_bytes += info.file_size
        if total_bytes > _DOCX_MAX_TOTAL_BYTES:
            raise ValueError("DOCX_TOTAL_TOO_LARGE")
        infos[name] = info
    return {name: package.read(info) for name, info in infos.items()}


def _docx_native_document(
    source: SourceFile,
    document: ElementTree.Element,
    numbering: dict[tuple[str, int], str],
) -> NativeDocument:
    body = document.find("./w:body", _NS)
    if body is None:
        raise ValueError("DOCX_BODY_MISSING")

    spans: list[SourceSpan] = []
    groups: list[NativeGroup | None] = []
    tables: list[NativeTable | None] = []
    for child in _iter_docx_visible_blocks(body):
        if child.tag == f"{_W}p":
            _append_docx_paragraph(
                source_hash=source.sha256,
                paragraph=child,
                numbering=numbering,
                spans=spans,
                groups=groups,
            )
        elif child.tag == f"{_W}tbl":
            _append_docx_table(
                source_hash=source.sha256,
                table_element=child,
                spans=spans,
                groups=groups,
                tables=tables,
            )

    if any(group is None for group in groups) or any(table is None for table in tables):
        raise SemanticSourceError("SEMANTIC_DOCX_UNREADABLE")

    return NativeDocument(
        source_hash=source.sha256,
        extraction_mode=ExtractionMode.DOCX_XML,
        page_count=0,
        parser_versions={
            "semantic_parser": SEMANTIC_PARSER_VERSION,
            "ooxml": "wordprocessingml-2006",
        },
        spans=tuple(spans),
        groups=tuple(group for group in groups if group is not None),
        tables=tuple(table for table in tables if table is not None),
    )


def _iter_docx_visible_blocks(container: ElementTree.Element):
    """Yield visible block elements without re-yielding table descendants."""
    for child in container:
        if child.tag in {f"{_W}p", f"{_W}tbl"}:
            yield child
            continue
        if child.tag == f"{_W}sdt":
            content = child.find("./w:sdtContent", _NS)
            if content is not None:
                yield from _iter_docx_visible_blocks(content)
            continue
        if child.tag in {
            f"{_W}customXml",
            f"{_W}sdtContent",
            f"{_W}ins",
            f"{_W}moveTo",
            f"{_W}smartTag",
        }:
            yield from _iter_docx_visible_blocks(child)
            continue
        if child.tag in {f"{_W}del", f"{_W}moveFrom"}:
            continue
        if child.tag == f"{_W}altChunk" or child.find(f".//{_W}p") is not None or child.find(
            f".//{_W}tbl"
        ) is not None:
            raise SemanticSourceError("SEMANTIC_DOCX_VISIBLE_CONTAINER_UNSUPPORTED")


def _append_docx_paragraph(
    *,
    source_hash: str,
    paragraph: ElementTree.Element,
    numbering: dict[tuple[str, int], str],
    spans: list[SourceSpan],
    groups: list[NativeGroup],
) -> None:
    style_name = _docx_paragraph_style(paragraph)
    list_metadata = _docx_list_metadata(paragraph, numbering)
    text = _paragraph_text(paragraph)
    if text:
        span = _source_span(
            source_hash=source_hash,
            ordinal=len(spans),
            page=None,
            bbox=None,
            text=text,
        )
        spans.append(span)
        kind = (
            "caption"
            if style_name is not None and "caption" in style_name.casefold()
            else "paragraph"
        )
        group_kwargs: dict[str, Any] = {"style_name": style_name}
        if kind != "caption" and list_metadata is not None:
            kind = "list_item"
            group_kwargs.update(list_metadata)
        groups.append(
            NativeGroup(
                order=len(groups),
                kind=kind,
                span_ids=(span.span_id,),
                **group_kwargs,
            )
        )

    for kind, auxiliary_text in _docx_auxiliary_texts(paragraph):
        span = _source_span(
            source_hash=source_hash,
            ordinal=len(spans),
            page=None,
            bbox=None,
            text=auxiliary_text,
        )
        spans.append(span)
        groups.append(
            NativeGroup(
                order=len(groups),
                kind=kind,
                span_ids=(span.span_id,),
            )
        )


def _docx_auxiliary_texts(paragraph: ElementTree.Element) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for text_box in paragraph.findall(f".//{_W}txbxContent"):
        for text_box_paragraph in text_box.findall(f"./{_W}p"):
            text_box_text = _paragraph_text(text_box_paragraph)
            if text_box_text:
                values.append(("text_box", text_box_text))
    for doc_pr in paragraph.findall(f".//{_WP}docPr"):
        for value in (doc_pr.get("descr"), doc_pr.get("title")):
            if value:
                values.append(("alt_text", normalize_line_endings(value)))
    return values


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []

    def visit(element: ElementTree.Element) -> None:
        if element.tag == f"{_W}txbxContent":
            return
        if element.tag == f"{_W}t":
            parts.append(element.text or "")
        elif element.tag == f"{_W}tab":
            parts.append("\t")
        elif element.tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
        for child in element:
            visit(child)

    visit(paragraph)
    return normalize_line_endings("".join(parts))


def _docx_paragraph_style(paragraph: ElementTree.Element) -> str | None:
    style = paragraph.find("./w:pPr/w:pStyle", _NS)
    return None if style is None else style.get(f"{_W}val")


def _docx_list_metadata(
    paragraph: ElementTree.Element, numbering: dict[tuple[str, int], str]
) -> dict[str, Any] | None:
    properties = paragraph.find("./w:pPr/w:numPr", _NS)
    if properties is None:
        return None
    number_id = properties.find("./w:numId", _NS)
    level = properties.find("./w:ilvl", _NS)
    if number_id is None:
        return None
    level_value = int(level.get(f"{_W}val", "0")) if level is not None else 0
    number_id_value = number_id.get(f"{_W}val")
    if number_id_value is None or number_id_value == "0":
        return None
    return {
        "list_kind": numbering.get((number_id_value, level_value), "ordered"),
        "list_depth": level_value,
    }


def _docx_numbering_formats(numbering_part: bytes | None) -> dict[tuple[str, int], str]:
    if numbering_part is None:
        return {}
    numbering = ElementTree.fromstring(numbering_part)
    abstract_formats: dict[tuple[str, int], str] = {}
    for abstract_number in numbering.findall("./w:abstractNum", _NS):
        abstract_id = abstract_number.get(f"{_W}abstractNumId")
        if abstract_id is None:
            continue
        for level in abstract_number.findall("./w:lvl", _NS):
            level_value = int(level.get(f"{_W}ilvl", "0"))
            number_format = level.find("./w:numFmt", _NS)
            if number_format is None:
                continue
            abstract_formats[(abstract_id, level_value)] = (
                "unordered" if number_format.get(f"{_W}val") == "bullet" else "ordered"
            )

    formats: dict[tuple[str, int], str] = {}
    for number in numbering.findall("./w:num", _NS):
        number_id = number.get(f"{_W}numId")
        abstract_id = number.find("./w:abstractNumId", _NS)
        if number_id is None or abstract_id is None:
            continue
        abstract_id_value = abstract_id.get(f"{_W}val")
        if abstract_id_value is None:
            continue
        for (candidate_abstract_id, level), list_kind in abstract_formats.items():
            if candidate_abstract_id == abstract_id_value:
                formats[(number_id, level)] = list_kind
        for override in number.findall("./w:lvlOverride", _NS):
            level_value = int(override.get(f"{_W}ilvl", "0"))
            override_level = override.find("./w:lvl", _NS)
            if override_level is None:
                continue
            number_format = override_level.find("./w:numFmt", _NS)
            if number_format is not None:
                formats[(number_id, level_value)] = (
                    "unordered" if number_format.get(f"{_W}val") == "bullet" else "ordered"
                )
    return formats


def _append_docx_table(
    *,
    source_hash: str,
    table_element: ElementTree.Element,
    spans: list[SourceSpan],
    groups: list[NativeGroup | None],
    tables: list[NativeTable | None],
) -> None:
    rows = table_element.findall("./w:tr", _NS)
    if not rows:
        raise ValueError("DOCX_TABLE_ROWS_MISSING")
    declared_columns = len(table_element.findall("./w:tblGrid/w:gridCol", _NS))
    _require_table_dimensions(len(rows), max(1, declared_columns))
    column_count = _docx_table_column_count(rows, declared_columns)
    _require_table_dimensions(len(rows), column_count)

    table_order = len(tables)
    group_order = len(groups)
    tables.append(None)
    groups.append(None)
    cells: list[dict[str, Any]] = []
    active_merges: dict[int, dict[str, Any]] = {}

    for row_index, row in enumerate(rows):
        row_is_header = _docx_row_is_header(row)
        cursor = _docx_grid_before(row)
        current_merges: dict[int, dict[str, Any]] = {}
        occupied_regions: list[tuple[int, int]] = []
        for table_cell in row.findall("./w:tc", _NS):
            merge = _docx_vertical_merge(table_cell)
            width = _docx_grid_span(table_cell)
            if merge == "continue" and _docx_has_grid_span(table_cell) is False:
                origin = active_merges.get(cursor)
                if origin is not None:
                    width = int(origin["end_column"]) - int(origin["start_column"])
            end_column = cursor + width
            if width <= 0 or end_column > column_count:
                raise ValueError("DOCX_TABLE_GRID_INVALID")
            if merge == "continue":
                origin = active_merges.get(cursor)
                if origin is None or any(
                    active_merges.get(column) is not origin for column in range(cursor, end_column)
                ):
                    raise ValueError("DOCX_VERTICAL_MERGE_ORIGIN_MISSING")
                origin["end_row"] = row_index + 1
                for column in range(cursor, end_column):
                    current_merges[column] = origin
            else:
                span_ids: list[str] = []
                paragraph_count = 0
                for block in _iter_docx_visible_blocks(table_cell):
                    if block.tag == f"{_W}tbl":
                        _append_docx_table(
                            source_hash=source_hash,
                            table_element=block,
                            spans=spans,
                            groups=groups,
                            tables=tables,
                        )
                        continue
                    cell_text = _paragraph_text(block)
                    if cell_text:
                        span = _source_span(
                            source_hash=source_hash,
                            ordinal=len(spans),
                            page=None,
                            bbox=None,
                            text=cell_text,
                            paragraph_break_before=paragraph_count > 0,
                        )
                        spans.append(span)
                        span_ids.append(span.span_id)
                        paragraph_count += 1
                    for kind, auxiliary_text in _docx_auxiliary_texts(block):
                        span = _source_span(
                            source_hash=source_hash,
                            ordinal=len(spans),
                            page=None,
                            bbox=None,
                            text=auxiliary_text,
                        )
                        spans.append(span)
                        groups.append(
                            NativeGroup(
                                order=len(groups),
                                kind=kind,
                                span_ids=(span.span_id,),
                            )
                        )
                cell = {
                    "start_row": row_index,
                    "end_row": row_index + 1,
                    "start_column": cursor,
                    "end_column": end_column,
                    "span_ids": tuple(span_ids),
                    "column_header": row_is_header,
                }
                cells.append(cell)
                if merge == "restart":
                    for column in range(cursor, end_column):
                        current_merges[column] = cell
            occupied_regions.append((cursor, end_column))
            cursor = end_column
        if cursor + _docx_grid_after(row) > column_count:
            raise ValueError("DOCX_TABLE_GRID_INVALID")
        _append_docx_blank_cells(
            cells=cells,
            row_index=row_index,
            column_count=column_count,
            occupied_regions=occupied_regions,
            column_header=row_is_header,
        )
        if len(cells) > MAX_TABLE_CELLS:
            raise SemanticSourceError("SEMANTIC_TABLE_CELLS_LIMIT_EXCEEDED")
        active_merges = current_merges

    native_cells = tuple(
        NativeTableCell(**cell)
        for cell in sorted(
            cells,
            key=lambda cell: (
                int(cell["start_row"]),
                int(cell["start_column"]),
                int(cell["end_row"]),
                int(cell["end_column"]),
            ),
        )
    )
    table = NativeTable(
        order=table_order,
        row_count=len(rows),
        column_count=column_count,
        cells=native_cells,
    )
    tables[table_order] = table
    groups[group_order] = NativeGroup(
        order=group_order,
        kind="table",
        span_ids=tuple(span_id for cell in table.cells for span_id in cell.span_ids),
        table_index=table.order,
    )


def _require_table_dimensions(row_count: int, column_count: int) -> None:
    if row_count > MAX_TABLE_ROWS:
        raise SemanticSourceError("SEMANTIC_TABLE_ROWS_LIMIT_EXCEEDED")
    if column_count > MAX_TABLE_COLUMNS:
        raise SemanticSourceError("SEMANTIC_TABLE_COLUMNS_LIMIT_EXCEEDED")
    if row_count * column_count > MAX_TABLE_GRID_AREA:
        raise SemanticSourceError("SEMANTIC_TABLE_GRID_AREA_LIMIT_EXCEEDED")


def _docx_row_is_header(row: ElementTree.Element) -> bool:
    header = row.find("./w:trPr/w:tblHeader", _NS)
    if header is None:
        return False
    return header.get(f"{_W}val", "true").casefold() not in {"0", "false", "off", "no"}


def _docx_table_column_count(rows: list[ElementTree.Element], declared_columns: int) -> int:
    inferred_columns = 0
    for row in rows:
        inferred_columns = max(
            inferred_columns,
            _docx_grid_before(row)
            + sum(_docx_grid_span(cell) for cell in row.findall("./w:tc", _NS))
            + _docx_grid_after(row),
        )
    _require_table_dimensions(len(rows), max(declared_columns, inferred_columns))
    if declared_columns and declared_columns < inferred_columns:
        raise ValueError("DOCX_TABLE_DECLARED_GRID_TOO_SMALL")
    return declared_columns or inferred_columns


def _append_docx_blank_cells(
    *,
    cells: list[dict[str, Any]],
    row_index: int,
    column_count: int,
    occupied_regions: list[tuple[int, int]],
    column_header: bool,
) -> None:
    cursor = 0
    for start_column, end_column in occupied_regions:
        if start_column < cursor:
            raise ValueError("DOCX_TABLE_GRID_OVERLAP")
        if cursor < start_column:
            cells.append(
                _docx_blank_cell(row_index, cursor, start_column, column_header)
            )
        cursor = end_column
    if cursor < column_count:
        cells.append(_docx_blank_cell(row_index, cursor, column_count, column_header))


def _docx_blank_cell(
    row_index: int,
    start_column: int,
    end_column: int,
    column_header: bool,
) -> dict[str, Any]:
    return {
        "start_row": row_index,
        "end_row": row_index + 1,
        "start_column": start_column,
        "end_column": end_column,
        "span_ids": (),
        "column_header": column_header,
    }


def _docx_grid_span(table_cell: ElementTree.Element) -> int:
    grid_span = table_cell.find("./w:tcPr/w:gridSpan", _NS)
    return 1 if grid_span is None else int(grid_span.get(f"{_W}val", "1"))


def _docx_has_grid_span(table_cell: ElementTree.Element) -> bool:
    return table_cell.find("./w:tcPr/w:gridSpan", _NS) is not None


def _docx_vertical_merge(table_cell: ElementTree.Element) -> str | None:
    merge = table_cell.find("./w:tcPr/w:vMerge", _NS)
    if merge is None:
        return None
    return "restart" if merge.get(f"{_W}val") == "restart" else "continue"


def _docx_grid_before(row: ElementTree.Element) -> int:
    grid_before = row.find("./w:trPr/w:gridBefore", _NS)
    return 0 if grid_before is None else int(grid_before.get(f"{_W}val", "0"))


def _docx_grid_after(row: ElementTree.Element) -> int:
    grid_after = row.find("./w:trPr/w:gridAfter", _NS)
    return 0 if grid_after is None else int(grid_after.get(f"{_W}val", "0"))


def normalize_line_endings(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _text_object_key(text_page: Any, index: int) -> int | None:
    text_object = text_page.get_textobj(index)
    if text_object is None:
        return None
    address = ctypes.cast(text_object.raw, ctypes.c_void_p).value
    return None if address is None else int(address)


def _normalized_pdf_box(
    box: tuple[float, float, float, float],
    page_box: tuple[float, float, float, float],
) -> SourceBox:
    page_left, page_bottom, page_right, page_top = page_box
    if not all(math.isfinite(value) for value in page_box):
        raise _PdfGeometryError
    width = page_right - page_left
    height = page_top - page_bottom
    if width <= 0 or height <= 0:
        raise _PdfGeometryError
    left, bottom, right, top = box
    if not all(math.isfinite(value) for value in box):
        raise _PdfGeometryError
    normalized = (
        (left - page_left) / width,
        (bottom - page_bottom) / height,
        (right - page_left) / width,
        (top - page_bottom) / height,
    )
    if not all(0 <= value <= 1 for value in normalized):
        raise _PdfGeometryError
    return SourceBox(
        left=normalized[0],
        bottom=normalized[1],
        right=normalized[2],
        top=normalized[3],
    )


class _PdfGeometryError(ValueError):
    pass


def extract_pdf_native(source: SourceFile, *, pdfium: Any | None = None) -> NativeDocument | None:
    """Extract one embedded-text catalog only when every PDF page is readable."""
    if pdfium is None:
        import pypdfium2

        pdfium = pypdfium2

    try:
        document = pdfium.PdfDocument(source_bytes(source))
        try:
            page_count = len(document)
            if page_count == 0:
                return None

            spans: list[SourceSpan] = []
            group_records: list[dict[str, Any]] = []
            for page_index in range(page_count):
                page = document[page_index]
                try:
                    page_box = page.get_bbox()
                    text_page = page.get_textpage()
                    try:
                        page_spans = _pdf_page_spans(
                            text_page=text_page,
                            page_box=page_box,
                            page=page_index + 1,
                            source_hash=source.sha256,
                            ordinal_start=len(spans),
                        )
                    finally:
                        text_page.close()
                finally:
                    page.close()

                if not any(not span.text.isspace() for span in page_spans):
                    return None
                spans.extend(page_spans)
                _append_pdf_page_groups(group_records, page_spans)
        finally:
            document.close()
    except (pdfium.PdfiumError, _PdfGeometryError):
        return None

    return NativeDocument(
        source_hash=source.sha256,
        extraction_mode=ExtractionMode.PDF_EMBEDDED,
        page_count=page_count,
        parser_versions={
            "semantic_parser": SEMANTIC_PARSER_VERSION,
            "pypdfium2": importlib.metadata.version("pypdfium2"),
        },
        spans=tuple(spans),
        groups=tuple(
            NativeGroup(
                order=order,
                kind="paragraph",
                span_ids=tuple(record["span_ids"]),
                page=record["page"],
                bbox=_union_boxes(record["boxes"]),
            )
            for order, record in enumerate(group_records)
        ),
        tables=(),
    )


def _pdf_page_spans(
    *,
    text_page: Any,
    page_box: tuple[float, float, float, float],
    page: int,
    source_hash: str,
    ordinal_start: int,
) -> list[SourceSpan]:
    count = text_page.count_chars()
    ranges = _text_object_ranges(text_page, count)
    raw_ranges = [text_page.get_text_range(start, length) for start, length in ranges]
    normalized_ranges = _normalize_ranges(raw_ranges)
    if not normalize_line_endings("".join(raw_ranges)).strip():
        return []

    spans: list[SourceSpan] = []
    for (start, length), text in zip(ranges, normalized_ranges, strict=True):
        if not text:
            continue
        boxes = [
            _normalized_pdf_box(box, page_box)
            for index in range(start, start + length)
            if (box := text_page.get_charbox(index)) is not None
        ]
        ordinal = ordinal_start + len(spans)
        spans.append(
            SourceSpan(
                span_id=make_span_id(source_hash, ordinal),
                ordinal=ordinal,
                page=page,
                bbox=_union_boxes(boxes),
                text=text,
                text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return spans


def _text_object_ranges(text_page: Any, count: int) -> list[tuple[int, int]]:
    if count == 0:
        return []
    ranges: list[tuple[int, int]] = []
    start = 0
    key = _text_object_key(text_page, 0)
    for index in range(1, count):
        next_key = _text_object_key(text_page, index)
        if next_key != key:
            ranges.append((start, index - start))
            start = index
            key = next_key
    ranges.append((start, count - start))
    return ranges


def _normalize_ranges(raw_ranges: list[str]) -> list[str]:
    """Normalize line endings once while retaining the owning text-object range."""
    normalized = ["" for _ in raw_ranges]
    preceding_character_was_cr = False
    for range_index, raw_text in enumerate(raw_ranges):
        for value in raw_text:
            if value == "\r":
                normalized[range_index] += "\n"
                preceding_character_was_cr = True
            elif value == "\n" and preceding_character_was_cr:
                preceding_character_was_cr = False
            else:
                normalized[range_index] += value
                preceding_character_was_cr = False
    return normalized


def _append_pdf_page_groups(
    records: list[dict[str, Any]], page_spans: list[SourceSpan]
) -> None:
    leading_whitespace: list[SourceSpan] = []
    current: dict[str, Any] | None = None
    for span in page_spans:
        if span.text.isspace():
            if current is None:
                leading_whitespace.append(span)
            else:
                _append_span_to_group(current, span)
            continue
        current = {
            "page": span.page,
            "span_ids": [candidate.span_id for candidate in leading_whitespace],
            "boxes": [
                candidate.bbox for candidate in leading_whitespace if candidate.bbox is not None
            ],
        }
        _append_span_to_group(current, span)
        records.append(current)
        leading_whitespace = []


def _append_span_to_group(record: dict[str, Any], span: SourceSpan) -> None:
    record["span_ids"].append(span.span_id)
    if span.bbox is not None:
        record["boxes"].append(span.bbox)


def extract_ocr_native(source: SourceFile, document: Any) -> NativeDocument:
    """Build the one authoritative whole-document OCR catalog from Docling output."""
    spans: list[SourceSpan] = []
    groups: list[NativeGroup] = []
    tables: list[NativeTable] = []

    for item, _level in document.iterate_items():
        page, bbox = _item_location(item, document)
        if _is_table_item(item):
            table, group, table_spans = _ocr_table(
                item=item,
                document=document,
                page=page,
                bbox=bbox,
                source_hash=source.sha256,
                ordinal_start=len(spans),
                order=len(groups),
                table_index=len(tables),
            )
            spans.extend(table_spans)
            tables.append(table)
            groups.append(group)
            continue

        value = getattr(item, "orig", None)
        raw_text = value if value is not None else getattr(item, "text", "")
        text = normalize_line_endings(str(raw_text))
        if text:
            span = _source_span(
                source_hash=source.sha256,
                ordinal=len(spans),
                page=page,
                bbox=bbox,
                text=text,
            )
            spans.append(span)
            groups.append(
                NativeGroup(
                    order=len(groups),
                    kind="paragraph",
                    span_ids=(span.span_id,),
                    page=page,
                    bbox=bbox,
                )
            )

    if not any(not span.text.isspace() for span in spans):
        raise SemanticSourceError("SEMANTIC_OCR_UNREADABLE")

    return NativeDocument(
        source_hash=source.sha256,
        extraction_mode=ExtractionMode.OCR,
        page_count=len(getattr(document, "pages", {})),
        parser_versions={
            "semantic_parser": SEMANTIC_PARSER_VERSION,
            "docling": importlib.metadata.version("docling"),
            "docling-core": importlib.metadata.version("docling-core"),
        },
        spans=tuple(spans),
        groups=tuple(groups),
        tables=tuple(tables),
    )


def _is_table_item(item: Any) -> bool:
    data = getattr(item, "data", None)
    return item.__class__.__name__ == "TableItem" and hasattr(data, "table_cells")


def _ocr_table(
    *,
    item: Any,
    document: Any,
    page: int | None,
    bbox: SourceBox | None,
    source_hash: str,
    ordinal_start: int,
    order: int,
    table_index: int,
) -> tuple[NativeTable, NativeGroup, list[SourceSpan]]:
    row_count = int(item.data.num_rows)
    column_count = int(item.data.num_cols)
    _require_table_dimensions(row_count, column_count)
    raw_cells = item.data.table_cells
    try:
        raw_cell_count = len(raw_cells)
    except TypeError as exc:
        raise SemanticSourceError("SEMANTIC_TABLE_CELLS_UNSIZED") from exc
    if raw_cell_count > MAX_TABLE_CELLS:
        raise SemanticSourceError("SEMANTIC_TABLE_CELLS_LIMIT_EXCEEDED")
    cells = _unique_table_cells(raw_cells)
    spans: list[SourceSpan] = []
    native_cells: list[NativeTableCell] = []
    for cell in cells:
        text = normalize_line_endings(str(getattr(cell, "text", "")))
        cell_bbox = _normalized_docling_box(getattr(cell, "bbox", None), document, page)
        span_ids: tuple[str, ...] = ()
        if text:
            span = _source_span(
                source_hash=source_hash,
                ordinal=ordinal_start + len(spans),
                page=page,
                bbox=cell_bbox,
                text=text,
            )
            spans.append(span)
            span_ids = (span.span_id,)
        native_cells.append(
            NativeTableCell(
                start_row=int(cell.start_row_offset_idx),
                end_row=int(cell.end_row_offset_idx),
                start_column=int(cell.start_col_offset_idx),
                end_column=int(cell.end_col_offset_idx),
                span_ids=span_ids,
                column_header=bool(getattr(cell, "column_header", False)),
                bbox=cell_bbox,
            )
        )
    table = NativeTable(
        order=table_index,
        row_count=row_count,
        column_count=column_count,
        cells=tuple(native_cells),
    )
    group = NativeGroup(
        order=order,
        kind="table",
        span_ids=tuple(span.span_id for span in spans),
        page=page,
        bbox=bbox,
        table_index=table_index,
    )
    return table, group, spans


def _unique_table_cells(cells: Any) -> list[Any]:
    unique: dict[int, Any] = {}
    for cell in cells:
        unique.setdefault(id(cell), cell)
    return sorted(
        unique.values(),
        key=lambda cell: (
            int(cell.start_row_offset_idx),
            int(cell.start_col_offset_idx),
            int(cell.end_row_offset_idx),
            int(cell.end_col_offset_idx),
        ),
    )


def _item_location(item: Any, document: Any) -> tuple[int | None, SourceBox | None]:
    provenance = next(iter(getattr(item, "prov", None) or ()), None)
    if provenance is None:
        return None, None
    page = getattr(provenance, "page_no", None)
    return page, _normalized_docling_box(getattr(provenance, "bbox", None), document, page)


def _normalized_docling_box(box: Any, document: Any, page: int | None) -> SourceBox | None:
    if box is None or page is None:
        return None
    pages = getattr(document, "pages", {})
    try:
        page_size = pages[page].size
    except (KeyError, IndexError, TypeError):
        return None
    width = float(page_size.width)
    height = float(page_size.height)
    bottom_left_box = (
        box.to_bottom_left_origin(height) if hasattr(box, "to_bottom_left_origin") else box
    )
    return SourceBox(
        left=float(bottom_left_box.l) / width,
        bottom=float(bottom_left_box.b) / height,
        right=float(bottom_left_box.r) / width,
        top=float(bottom_left_box.t) / height,
    )


def _source_span(
    *, source_hash: str,
    ordinal: int,
    page: int | None,
    bbox: SourceBox | None,
    text: str,
    paragraph_break_before: bool = False,
) -> SourceSpan:
    return SourceSpan(
        span_id=make_span_id(source_hash, ordinal),
        ordinal=ordinal,
        page=page,
        bbox=bbox,
        text=text,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        paragraph_break_before=paragraph_break_before,
    )


def _union_boxes(boxes: list[SourceBox]) -> SourceBox | None:
    if not boxes:
        return None
    return SourceBox(
        left=min(box.left for box in boxes),
        bottom=min(box.bottom for box in boxes),
        right=max(box.right for box in boxes),
        top=max(box.top for box in boxes),
    )
