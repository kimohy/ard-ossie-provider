from __future__ import annotations

from pathlib import Path

import pytest

from ard_ossie.ingestion import SourceLimits, SourceRole, SourceValidationError, scan_sources


def create_source_tree(root: Path, *, semantic_names: tuple[str, ...] = ("semantic.docx",)) -> Path:
    sources = root / "sources"
    (sources / "product-info").mkdir(parents=True)
    (sources / "semantic").mkdir()
    (sources / "dictionary").mkdir()
    (sources / "product-info" / "product.html").write_text(
        "<html><h1>Sales Order</h1></html>", encoding="utf-8"
    )
    for name in semantic_names:
        (sources / "semantic" / name).write_bytes(b"PK\x03\x04semantic")
    (sources / "dictionary" / "dictionary.xlsx").write_bytes(b"PK\x03\x04dictionary")
    return sources


def test_source_set_requires_each_ard_role(tmp_path: Path) -> None:
    sources = create_source_tree(tmp_path)
    (sources / "dictionary" / "dictionary.xlsx").unlink()

    with pytest.raises(SourceValidationError, match="MISSING_DICTIONARY"):
        scan_sources(sources)


def test_source_set_rejects_two_semantic_documents(tmp_path: Path) -> None:
    sources = create_source_tree(tmp_path, semantic_names=("semantic.docx", "semantic.pdf"))

    with pytest.raises(SourceValidationError, match="MULTIPLE_SEMANTIC_DOCUMENTS"):
        scan_sources(sources)


def test_source_set_rejects_symlink(tmp_path: Path) -> None:
    sources = create_source_tree(tmp_path)
    target = sources / "product-info" / "product.html"
    target.rename(sources / "product-info" / "actual.html")
    target.symlink_to(sources / "product-info" / "actual.html")

    with pytest.raises(SourceValidationError, match="SYMLINK_NOT_ALLOWED"):
        scan_sources(sources)


def test_source_set_enforces_role_size_limit(tmp_path: Path) -> None:
    sources = create_source_tree(tmp_path)

    with pytest.raises(SourceValidationError, match="SOURCE_TOO_LARGE"):
        scan_sources(sources, limits=SourceLimits(max_bytes_per_file=10))


def test_valid_source_set_records_roles_hashes_and_relative_paths(tmp_path: Path) -> None:
    sources = create_source_tree(tmp_path)

    manifest = scan_sources(sources)

    assert {item.role for item in manifest.files} == {
        SourceRole.PRODUCT_HTML,
        SourceRole.SEMANTIC_DOCUMENT,
        SourceRole.DICTIONARY_EXCEL,
    }
    assert all(len(item.sha256) == 64 for item in manifest.files)
    assert {item.relative_path for item in manifest.files} == {
        "product-info/product.html",
        "semantic/semantic.docx",
        "dictionary/dictionary.xlsx",
    }
