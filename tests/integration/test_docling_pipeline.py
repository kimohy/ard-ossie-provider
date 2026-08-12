from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ard_ossie.docling_parser import DoclingParser, Evidence
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


def test_docling_adapter_preserves_markdown_and_page_provenance(tmp_path: Path) -> None:
    path = tmp_path / "semantic.pdf"
    path.write_bytes(b"%PDF-test")
    source = SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path="semantic/semantic.pdf",
        sha256="a" * 64,
        size_bytes=9,
    )
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
    assert converter.converted_path == str(path)


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
