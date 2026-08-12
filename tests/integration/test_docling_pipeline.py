from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ard_ossie.docling_parser import DoclingParser, Evidence, ParsedDocument, _parse_embedded_pdf
from ard_ossie.ingestion import SourceFile, SourceRole


class FakeDocument:
    def export_to_markdown(self) -> str:
        return "# Sales Order\n\nNet revenue excludes tax."

    def iterate_items(self):
        bbox = SimpleNamespace(l=10.0, t=20.0, r=100.0, b=40.0)
        provenance = SimpleNamespace(page_no=2, bbox=bbox, charspan=(0, 31))
        item = SimpleNamespace(text="Net revenue excludes tax.", prov=[provenance])
        yield item, 1


class FakeHtmlDocument:
    def export_to_markdown(self) -> str:
        return "# 제품 개요\n\n사용자가 입력한 제품 목적"

    def iterate_items(self):
        item = SimpleNamespace(text="사용자가 입력한 제품 목적", prov=[])
        yield item, 2


class FakeAiGeneratedHtmlDocument:
    def export_to_markdown(self) -> str:
        return "# 제품 개요\n\n(AI 자동생성) 데이터 요약\n\n자동 요약 값\n\n사용자 설명"

    def iterate_items(self):
        for text in (
            "(AI 자동생성) 데이터 요약",
            "자동 요약 값",
            "사용자 설명",
        ):
            yield SimpleNamespace(text=text, prov=[]), 5


class FakeConverter:
    def __init__(self, document: object | None = None) -> None:
        self.converted_path: str | None = None
        self.document = document or FakeDocument()

    def convert(self, source: str) -> object:
        self.converted_path = source
        return SimpleNamespace(document=self.document)


class ExplodingConverter:
    def convert(self, source: str) -> object:
        raise AssertionError(f"Docling fallback was unexpectedly called for {source}")


class FakePdfiumError(Exception):
    pass


class FakeTextPage:
    def __init__(self, text: str, *, get_text_range_error: Exception | None = None) -> None:
        self._text = text
        self._get_text_range_error = get_text_range_error
        self.closed = False

    def get_text_range(self) -> str:
        if self._get_text_range_error is not None:
            raise self._get_text_range_error
        return self._text

    def close(self) -> None:
        self.closed = True


class FakePdfPage:
    def __init__(
        self,
        text: str,
        *,
        get_textpage_error: Exception | None = None,
        get_text_range_error: Exception | None = None,
    ) -> None:
        self._text = text
        self._get_textpage_error = get_textpage_error
        self._get_text_range_error = get_text_range_error
        self.text_page: FakeTextPage | None = None
        self.closed = False

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


def assert_fake_pdf_handles_closed(pdfium: FakePdfium) -> None:
    assert pdfium.document is not None
    assert pdfium.document.closed is True
    for page in pdfium.document.pages:
        assert page.closed is True
        if page.text_page is not None:
            assert page.text_page.closed is True


def semantic_pdf_source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "semantic.pdf"
    path.write_bytes(b"%PDF-test")
    return SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path="semantic/semantic.pdf",
        sha256="a" * 64,
        size_bytes=9,
    )


def test_docling_adapter_preserves_markdown_and_page_provenance(tmp_path: Path) -> None:
    source = semantic_pdf_source(tmp_path)
    converter = FakeConverter()

    parsed = DoclingParser(converter=converter).parse(source)

    assert parsed.markdown.startswith("# Sales Order")
    assert parsed.evidence[0].locator == {
        "document": "semantic/semantic.pdf",
        "item_index": 0,
        "level": 1,
        "page": 2,
        "bbox": {"left": 10.0, "top": 20.0, "right": 100.0, "bottom": 40.0},
        "charspan": [0, 31],
    }
    assert converter.converted_path == str(source.path)


def test_embedded_pdf_preserves_internal_text_and_records_page_evidence(
    tmp_path: Path,
) -> None:
    source = semantic_pdf_source(tmp_path)
    pdfium = FakePdfium(["  개인정보\r\n유효성  ", "둘째 페이지\r끝  "])
    parsed = _parse_embedded_pdf(
        source,
        pdfium=pdfium,
    )

    assert parsed is not None
    assert parsed.markdown == "개인정보\n유효성\n\n둘째 페이지\n끝"
    assert "개 인정보" not in parsed.markdown
    assert "유 효 성" not in parsed.markdown
    assert [item.locator for item in parsed.evidence] == [
        {"document": "semantic/semantic.pdf", "page": 1},
        {"document": "semantic/semantic.pdf", "page": 2},
    ]
    assert [item.excerpt for item in parsed.evidence] == [
        "개인정보\n유효성",
        "둘째 페이지\n끝",
    ]
    assert_fake_pdf_handles_closed(pdfium)


@pytest.mark.parametrize(
    "pdfium",
    [
        pytest.param(
            FakePdfium(["page one", "   "]),
            id="empty-normalized-page",
        ),
        pytest.param(
            FakePdfium(
                document=FakePdfDocument(
                    pages=[
                        FakePdfPage(
                            "page one",
                            get_textpage_error=FakePdfiumError("text page unavailable"),
                        )
                    ]
                )
            ),
            id="get-textpage-error",
        ),
        pytest.param(
            FakePdfium(
                document=FakePdfDocument(
                    pages=[
                        FakePdfPage(
                            "page one",
                            get_text_range_error=FakePdfiumError("text unavailable"),
                        )
                    ]
                )
            ),
            id="get-text-range-error",
        ),
    ],
)
def test_docling_adapter_falls_back_after_embedded_pdf_failure_and_closes_handles(
    tmp_path: Path,
    pdfium: FakePdfium,
) -> None:
    converter = FakeConverter()

    parsed = DoclingParser(
        converter=converter,
        embedded_pdf_parser=lambda source: _parse_embedded_pdf(source, pdfium=pdfium),
    ).parse(semantic_pdf_source(tmp_path))

    assert parsed.markdown.startswith("# Sales Order")
    assert converter.converted_path is not None
    assert_fake_pdf_handles_closed(pdfium)


def test_docling_adapter_falls_back_when_embedded_pdf_cannot_open(tmp_path: Path) -> None:
    converter = FakeConverter()
    pdfium = FakePdfium(document_open_error=True)

    parsed = DoclingParser(
        converter=converter,
        embedded_pdf_parser=lambda source: _parse_embedded_pdf(source, pdfium=pdfium),
    ).parse(semantic_pdf_source(tmp_path))

    assert parsed.markdown.startswith("# Sales Order")
    assert converter.converted_path is not None
    assert pdfium.document is None


def test_docling_adapter_prefers_complete_embedded_pdf_text(tmp_path: Path) -> None:
    expected = ParsedDocument(
        role=SourceRole.SEMANTIC_DOCUMENT,
        source_hash="a" * 64,
        markdown="개인정보와 유효성",
        evidence=[],
    )

    parsed = DoclingParser(
        converter=ExplodingConverter(),
        embedded_pdf_parser=lambda _source: expected,
    ).parse(semantic_pdf_source(tmp_path))

    assert parsed == expected


def test_docling_adapter_records_item_evidence_without_page_provenance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "product.html"
    path.write_text("<html><body>사용자가 입력한 제품 목적</body></html>", encoding="utf-8")
    source = SourceFile(
        role=SourceRole.PRODUCT_HTML,
        path=path,
        relative_path="product-info/product.html",
        sha256="a" * 64,
        size_bytes=path.stat().st_size,
    )

    parsed = DoclingParser(converter=FakeConverter(FakeHtmlDocument())).parse(source)

    assert parsed.evidence == [
        Evidence(
            source_hash="a" * 64,
            role=SourceRole.PRODUCT_HTML,
            locator={
                "document": "product-info/product.html",
                "item_index": 0,
                "level": 2,
            },
            excerpt="사용자가 입력한 제품 목적",
        )
    ]


def test_docling_adapter_excludes_ai_generated_label_and_adjacent_value_from_fact_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "product.html"
    path.write_text("<html><body>AI summary fixture</body></html>", encoding="utf-8")
    source = SourceFile(
        role=SourceRole.PRODUCT_HTML,
        path=path,
        relative_path="product-info/product.html",
        sha256="b" * 64,
        size_bytes=path.stat().st_size,
    )

    parsed = DoclingParser(converter=FakeConverter(FakeAiGeneratedHtmlDocument())).parse(source)

    assert [item.excerpt for item in parsed.evidence] == ["사용자 설명"]
    assert [item.excerpt for item in parsed.excluded_product_fact_evidence] == [
        "(AI 자동생성) 데이터 요약",
        "자동 요약 값",
    ]
    assert "excluded_product_fact_evidence" not in parsed.model_dump(mode="json")


def test_real_docling_excludes_ai_generated_heading_and_direct_child_value(
    tmp_path: Path,
) -> None:
    path = tmp_path / "product.html"
    path.write_text(
        """<html><body>
        <h2>(AI 자동생성) 데이터 요약</h2>
        <p>자동 생성된 요약 값</p>
        <h2>사용자 설명</h2>
        <p>사용자가 작성한 설명 값</p>
        </body></html>""",
        encoding="utf-8",
    )
    source = SourceFile(
        role=SourceRole.PRODUCT_HTML,
        path=path,
        relative_path="product-info/product.html",
        sha256="c" * 64,
        size_bytes=path.stat().st_size,
    )

    parsed = DoclingParser().parse(source)

    assert [item.excerpt for item in parsed.excluded_product_fact_evidence] == [
        "(AI 자동생성) 데이터 요약",
        "자동 생성된 요약 값",
    ]
    assert [item.excerpt for item in parsed.evidence] == [
        "사용자 설명",
        "사용자가 작성한 설명 값",
    ]


def test_real_docling_converts_html_and_docx_without_remote_service(tmp_path: Path) -> None:
    from docx import Document

    html_path = tmp_path / "product.html"
    html_path.write_text(
        "<html><body><h1>Sales Order</h1><p>Order analytics product.</p></body></html>",
        encoding="utf-8",
    )
    docx_path = tmp_path / "semantic.docx"
    document = Document()
    document.add_heading("Net Revenue", level=1)
    document.add_paragraph("Net revenue excludes tax.")
    document.save(docx_path)

    parser = DoclingParser()
    html = parser.parse(
        SourceFile(
            role=SourceRole.PRODUCT_HTML,
            path=html_path,
            relative_path="product-info/product.html",
            sha256="a" * 64,
            size_bytes=html_path.stat().st_size,
        )
    )
    docx = parser.parse(
        SourceFile(
            role=SourceRole.SEMANTIC_DOCUMENT,
            path=docx_path,
            relative_path="semantic/semantic.docx",
            sha256="b" * 64,
            size_bytes=docx_path.stat().st_size,
        )
    )

    assert "Sales Order" in html.markdown
    assert "Net Revenue" in docx.markdown
    assert "excludes tax" in docx.markdown
