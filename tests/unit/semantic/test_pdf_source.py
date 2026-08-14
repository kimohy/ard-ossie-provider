from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from ard_ossie.ingestion import SourceFile, SourceRole
from ard_ossie.semantic.models import ExtractionMode, make_span_id
from ard_ossie.semantic.sources import SemanticSourceError, extract_ocr_native, extract_pdf_native


class FakePdfiumError(Exception):
    pass


class FakeTextObject:
    def __init__(self, raw: ctypes._Pointer[ctypes.c_int]) -> None:
        self.raw = raw


class FakeTextPage:
    def __init__(self, text: str, *, get_text_range_error: Exception | None = None) -> None:
        self._text = text
        self._get_text_range_error = get_text_range_error
        self._raw_handles: dict[int, ctypes._Pointer[ctypes.c_int]] = {}
        self.closed = False

    def count_chars(self) -> int:
        return len(self._text)

    def get_text_range(self, index: int = 0, count: int | None = None) -> str:
        if self._get_text_range_error is not None:
            raise self._get_text_range_error
        return self._text[index:] if count is None else self._text[index : index + count]

    def get_charbox(self, index: int) -> tuple[float, float, float, float]:
        return (float(index), 10.0, float(index + 1), 20.0)

    def get_textobj(self, index: int) -> FakeTextObject | None:
        if self._text[index].isspace():
            return None
        raw_handle = self._raw_handles.setdefault(
            index // 4,
            ctypes.pointer(ctypes.c_int(index // 4)),
        )
        return FakeTextObject(raw=raw_handle)

    def close(self) -> None:
        self.closed = True


class FakePdfPage:
    def __init__(
        self,
        text: str,
        *,
        get_textpage_error: Exception | None = None,
        get_text_range_error: Exception | None = None,
        bbox: tuple[float, float, float, float] = (0.0, 0.0, 100.0, 200.0),
    ) -> None:
        self._text = text
        self._get_textpage_error = get_textpage_error
        self._get_text_range_error = get_text_range_error
        self._bbox = bbox
        self.text_page: FakeTextPage | None = None
        self.closed = False

    def get_bbox(self) -> tuple[float, float, float, float]:
        return self._bbox

    def get_textpage(self) -> FakeTextPage:
        if self._get_textpage_error is not None:
            raise self._get_textpage_error
        self.text_page = FakeTextPage(
            self._text,
            get_text_range_error=self._get_text_range_error,
        )
        return self.text_page

    def close(self) -> None:
        self.closed = True


class FakePdfDocument:
    def __init__(
        self,
        texts: list[str] | None = None,
        *,
        pages: list[FakePdfPage] | None = None,
    ) -> None:
        self.pages = pages or [FakePdfPage(text) for text in texts or []]
        self.closed = False

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, page_index: int) -> FakePdfPage:
        return self.pages[page_index]

    def close(self) -> None:
        self.closed = True


class FakePdfium:
    PdfiumError = FakePdfiumError

    def __init__(
        self,
        texts: list[str] | None = None,
        *,
        document: FakePdfDocument | None = None,
        document_open_error: bool = False,
    ) -> None:
        self._document_open_error = document_open_error
        self.document = document if document is not None else (
            None if document_open_error else FakePdfDocument(texts)
        )

    def PdfDocument(self, source: Path) -> FakePdfDocument:
        if self._document_open_error:
            raise FakePdfiumError("not a PDF")
        assert self.document is not None
        return self.document


def assert_pdf_handles_closed(pdfium: FakePdfium) -> None:
    assert pdfium.document is not None
    assert pdfium.document.closed is True
    for page in pdfium.document.pages:
        assert page.closed is True
        if page.text_page is not None:
            assert page.text_page.closed is True


def semantic_pdf_source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "semantic.pdf"
    contents = b"%PDF-test"
    path.write_bytes(contents)
    return SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path="semantic/semantic.pdf",
        sha256=hashlib.sha256(contents).hexdigest(),
        size_bytes=9,
        snapshot=contents,
    )


class TableItem:
    def __init__(self, cells: list[object]) -> None:
        self.text = "aggregate table text must not be emitted"
        self.data = SimpleNamespace(num_rows=1, num_cols=1, table_cells=cells)
        self.prov = [provenance()]


def provenance() -> SimpleNamespace:
    return SimpleNamespace(
        page_no=1,
        bbox=SimpleNamespace(l=0.0, b=0.0, r=100.0, t=100.0),
    )


def structured_ocr_document() -> object:
    text_item = SimpleNamespace(orig="개인정보\r\n", text="wrong hint", prov=[provenance()])
    cell = SimpleNamespace(
        text="항목값",
        start_row_offset_idx=0,
        end_row_offset_idx=1,
        start_col_offset_idx=0,
        end_col_offset_idx=1,
        column_header=True,
        bbox=SimpleNamespace(l=0.0, b=0.0, r=50.0, t=50.0),
    )
    table = TableItem([cell, cell])

    class Document:
        pages = {1: SimpleNamespace(size=SimpleNamespace(width=100.0, height=100.0))}

        @staticmethod
        def iterate_items():
            yield text_item, 1
            yield table, 1

    return Document()


def test_pdf_source_preserves_exact_text_and_positions(tmp_path: Path) -> None:
    source = semantic_pdf_source(tmp_path)
    pdfium = FakePdfium(["개인정보  유효성\r\n두 번째 줄"])

    native = extract_pdf_native(source, pdfium=pdfium)

    assert native is not None
    assert native.extraction_mode is ExtractionMode.PDF_EMBEDDED
    assert "".join(span.text for span in native.spans) == "개인정보  유효성\n두 번째 줄"
    assert [span.ordinal for span in native.spans] == list(range(len(native.spans)))
    assert [span.span_id for span in native.spans] == [
        make_span_id(source.sha256, ordinal) for ordinal in range(len(native.spans))
    ]
    assert all(
        span.text_hash == hashlib.sha256(span.text.encode()).hexdigest() for span in native.spans
    )
    assert all(span.bbox is not None for span in native.spans)
    assert native.spans[0].bbox is not None
    assert native.spans[0].bbox.model_dump() == {
        "left": 0.0,
        "bottom": 0.05,
        "right": 0.04,
        "top": 0.1,
    }
    assert_pdf_handles_closed(pdfium)


def test_pdf_source_groups_production_shaped_text_object_handles(tmp_path: Path) -> None:
    native = extract_pdf_native(semantic_pdf_source(tmp_path), pdfium=FakePdfium(["abcdef"]))

    assert native is not None
    assert [span.text for span in native.spans] == ["abcd", "ef"]


@pytest.mark.parametrize(
    "pdfium",
    [
        pytest.param(FakePdfium(["page one", "   "]), id="empty-page"),
        pytest.param(
            FakePdfium(
                document=FakePdfDocument(
                    pages=[FakePdfPage("page one", get_textpage_error=FakePdfiumError("bad"))]
                )
            ),
            id="text-page-error",
        ),
        pytest.param(
            FakePdfium(
                document=FakePdfDocument(
                    pages=[FakePdfPage("page one", get_text_range_error=FakePdfiumError("bad"))]
                )
            ),
            id="text-range-error",
        ),
    ],
)
def test_pdf_source_rejects_unreadable_catalog_and_closes_handles(
    tmp_path: Path, pdfium: FakePdfium
) -> None:
    assert extract_pdf_native(semantic_pdf_source(tmp_path), pdfium=pdfium) is None
    assert_pdf_handles_closed(pdfium)


@pytest.mark.parametrize(
    "bbox",
    [
        (0.0, 0.0, 0.0, 200.0),
        (0.0, 0.0, float("inf"), 200.0),
        (0.0, 0.0, 100.0, float("nan")),
    ],
)
def test_pdf_source_degrades_invalid_page_geometry_to_whole_document_ocr(
    tmp_path: Path,
    bbox: tuple[float, float, float, float],
) -> None:
    pdfium = FakePdfium(document=FakePdfDocument(pages=[FakePdfPage("text", bbox=bbox)]))

    assert extract_pdf_native(semantic_pdf_source(tmp_path), pdfium=pdfium) is None
    assert_pdf_handles_closed(pdfium)


def test_ocr_source_uses_one_authoritative_docling_catalog(tmp_path: Path) -> None:
    source = semantic_pdf_source(tmp_path)

    native = extract_ocr_native(source, structured_ocr_document())

    assert native.extraction_mode is ExtractionMode.OCR
    assert "".join(span.text for span in native.spans) == "개인정보\n항목값"
    assert [group.kind for group in native.groups] == ["paragraph", "table"]
    assert len(native.tables) == 1
    assert native.tables[0].cells[0].span_ids == (native.spans[1].span_id,)


def test_ocr_source_degrades_overlapping_table_cells_without_losing_text(
    tmp_path: Path,
) -> None:
    cells = [
        SimpleNamespace(
            text=text,
            start_row_offset_idx=0,
            end_row_offset_idx=1,
            start_col_offset_idx=0,
            end_col_offset_idx=1,
            column_header=True,
            bbox=SimpleNamespace(l=left, b=0.0, r=right, t=50.0),
        )
        for text, left, right in (("첫 번째", 0.0, 50.0), ("두 번째", 50.0, 100.0))
    ]
    table = TableItem(cells)

    class OverlappingTableDocument:
        pages = {1: SimpleNamespace(size=SimpleNamespace(width=100.0, height=100.0))}

        @staticmethod
        def iterate_items():
            yield table, 1

    native = extract_ocr_native(semantic_pdf_source(tmp_path), OverlappingTableDocument())

    assert native.tables == ()
    assert [span.text for span in native.spans] == ["첫 번째", "두 번째"]
    assert [group.kind for group in native.groups] == ["paragraph", "paragraph"]
    assert [group.span_ids for group in native.groups] == [
        (native.spans[0].span_id,),
        (native.spans[1].span_id,),
    ]


def test_ocr_source_rejects_catalog_without_readable_text(tmp_path: Path) -> None:
    class EmptyDocument:
        @staticmethod
        def iterate_items():
            yield SimpleNamespace(text=" \r\n", prov=[]), 1

    with pytest.raises(SemanticSourceError, match="SEMANTIC_OCR_UNREADABLE") as exc_info:
        extract_ocr_native(semantic_pdf_source(tmp_path), EmptyDocument())

    assert exc_info.value.code == "SEMANTIC_OCR_UNREADABLE"
