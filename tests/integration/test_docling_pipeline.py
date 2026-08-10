from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ard_ossie.docling_parser import DoclingParser
from ard_ossie.ingestion import SourceFile, SourceRole


class FakeDocument:
    def export_to_markdown(self) -> str:
        return "# Sales Order\n\nNet revenue excludes tax."

    def iterate_items(self):
        bbox = SimpleNamespace(l=10.0, t=20.0, r=100.0, b=40.0)
        provenance = SimpleNamespace(page_no=2, bbox=bbox, charspan=(0, 31))
        item = SimpleNamespace(text="Net revenue excludes tax.", prov=[provenance])
        yield item, 1


class FakeConverter:
    def __init__(self) -> None:
        self.converted_path: str | None = None

    def convert(self, source: str) -> object:
        self.converted_path = source
        return SimpleNamespace(document=FakeDocument())


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
        "item_index": 0,
        "level": 1,
        "page": 2,
        "bbox": {"left": 10.0, "top": 20.0, "right": 100.0, "bottom": 40.0},
        "charspan": [0, 31],
    }
    assert converter.converted_path == str(path)


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
