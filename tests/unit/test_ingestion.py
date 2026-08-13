from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

import ard_ossie.ingestion as ingestion
from ard_ossie.ingestion import (
    SourceFile,
    SourceLimits,
    SourceRole,
    SourceValidationError,
    scan_sources,
    snapshot_source_file,
    source_bytes,
)


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


def test_missing_source_root_uses_the_product_html_contract(tmp_path: Path) -> None:
    with pytest.raises(SourceValidationError, match="MISSING_PRODUCT_HTML"):
        scan_sources(tmp_path / "missing-sources")


def test_disappearing_enumerated_source_is_a_stable_path_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = create_source_tree(tmp_path)
    real_stat = ingestion.os.stat

    def disappearing_stat(path: os.PathLike[str] | str, *args, **kwargs):
        if path == "product.html" and kwargs.get("dir_fd") is not None:
            raise FileNotFoundError(errno.ENOENT, "entry disappeared", path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(ingestion.os, "stat", disappearing_stat)

    with pytest.raises(SourceValidationError, match="SOURCE_PATH_CHANGED"):
        scan_sources(sources)


def test_snapshot_source_file_maps_a_disappearing_ancestor_to_path_change(
    tmp_path: Path,
) -> None:
    with pytest.raises(SourceValidationError, match="SOURCE_PATH_CHANGED"):
        snapshot_source_file(
            tmp_path / "disappeared" / "semantic.pdf",
            role=SourceRole.SEMANTIC_DOCUMENT,
            relative_path="semantic/semantic.pdf",
        )


@pytest.mark.parametrize("error_number", [errno.EIO, errno.EACCES])
def test_source_root_open_preserves_operational_os_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    sources = create_source_tree(tmp_path)
    real_open = ingestion.os.open

    def failing_root_open(path: os.PathLike[str] | str, flags: int, *args, **kwargs) -> int:
        if path == os.path.sep:
            raise OSError(error_number, "root descriptor unavailable")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ingestion.os, "open", failing_root_open)

    with pytest.raises(OSError) as exc_info:
        scan_sources(sources)

    assert exc_info.value.errno == error_number


@pytest.mark.parametrize("target_name", ["product-info", "product.html"])
def test_source_descriptor_closes_when_immediate_fstat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    sources = create_source_tree(tmp_path)
    real_open = ingestion.os.open
    real_fstat = ingestion.os.fstat
    real_close = ingestion.os.close
    target_descriptor: int | None = None
    closed: list[int] = []

    def tracked_open(path: os.PathLike[str] | str, flags: int, *args, **kwargs) -> int:
        nonlocal target_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if path == target_name and kwargs.get("dir_fd") is not None:
            target_descriptor = descriptor
        return descriptor

    def failing_fstat(descriptor: int):
        if descriptor == target_descriptor:
            raise OSError(errno.EIO, "directory identity unavailable")
        return real_fstat(descriptor)

    def tracked_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(ingestion.os, "open", tracked_open)
    monkeypatch.setattr(ingestion.os, "fstat", failing_fstat)
    monkeypatch.setattr(ingestion.os, "close", tracked_close)

    with pytest.raises(OSError, match="directory identity unavailable"):
        scan_sources(sources)

    assert target_descriptor is not None
    assert target_descriptor in closed


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


def test_source_manifest_retains_one_immutable_byte_snapshot(tmp_path: Path) -> None:
    sources = create_source_tree(tmp_path)
    original = (sources / "semantic" / "semantic.docx").read_bytes()

    source = scan_sources(sources).by_role(SourceRole.SEMANTIC_DOCUMENT)
    (sources / "semantic" / "semantic.docx").write_bytes(b"PK\x03\x04replacement")

    assert source.snapshot == original
    assert source.sha256 == hashlib.sha256(original).hexdigest()


def test_source_file_legacy_constructor_acquires_verified_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "semantic.pdf"
    payload = b"%PDF-compatible"
    path.write_bytes(payload)

    source = SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path="semantic/semantic.pdf",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    path.write_bytes(b"%PDF-replaced")

    assert source_bytes(source) == payload
    assert "snapshot" not in source.model_dump()
    assert "snapshot" not in SourceFile.model_json_schema()["properties"]


def test_source_file_legacy_constructor_rejects_path_hash_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "semantic.pdf"
    path.write_bytes(b"%PDF-current")

    with pytest.raises(ValueError, match="SOURCE_SNAPSHOT_HASH_MISMATCH"):
        SourceFile(
            role=SourceRole.SEMANTIC_DOCUMENT,
            path=path,
            relative_path="semantic/semantic.pdf",
            sha256=hashlib.sha256(b"%PDF-previous").hexdigest(),
            size_bytes=len(b"%PDF-current"),
        )


def test_scan_sources_rejects_symlinked_role_directory(tmp_path: Path) -> None:
    sources = create_source_tree(tmp_path)
    trusted = sources / "semantic"
    trusted.rename(tmp_path / "trusted-semantic")
    outside = tmp_path / "outside-semantic"
    outside.mkdir()
    (outside / "semantic.docx").write_bytes(b"PK\x03\x04outside")
    trusted.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SourceValidationError, match="SYMLINK_NOT_ALLOWED|SOURCE_PATH_CHANGED"):
        scan_sources(sources)


def test_scan_sources_anchors_role_directory_during_leaf_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = create_source_tree(tmp_path)
    trusted = sources / "semantic"
    moved = tmp_path / "trusted-semantic"
    outside = tmp_path / "outside-semantic"
    outside.mkdir()
    (outside / "semantic.docx").write_bytes(b"PK\x03\x04outside")
    real_open = ingestion.os.open

    def swap_parent_then_open(path: os.PathLike[str] | str, flags: int, *args, **kwargs):
        if Path(path).name == "semantic.docx" and not moved.exists():
            trusted.rename(moved)
            trusted.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ingestion.os, "open", swap_parent_then_open)

    with pytest.raises(SourceValidationError, match="SYMLINK_NOT_ALLOWED|SOURCE_PATH_CHANGED"):
        scan_sources(sources)


def test_snapshot_source_file_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "semantic.pdf").write_bytes(b"%PDF-outside")
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SourceValidationError, match="SYMLINK_NOT_ALLOWED|SOURCE_PATH_CHANGED"):
        snapshot_source_file(
            linked / "semantic.pdf",
            role=SourceRole.SEMANTIC_DOCUMENT,
            relative_path="semantic/semantic.pdf",
        )


def test_snapshot_source_file_revalidates_racing_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    source_path = trusted / "semantic.pdf"
    source_path.write_bytes(b"%PDF-trusted")
    moved = tmp_path / "moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "semantic.pdf").write_bytes(b"%PDF-outside")
    real_open = ingestion.os.open

    def swap_parent_then_open(path: os.PathLike[str] | str, flags: int, *args, **kwargs):
        if Path(path).name == "semantic.pdf" and not moved.exists():
            trusted.rename(moved)
            trusted.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ingestion.os, "open", swap_parent_then_open)

    with pytest.raises(SourceValidationError, match="SYMLINK_NOT_ALLOWED|SOURCE_PATH_CHANGED"):
        snapshot_source_file(
            source_path,
            role=SourceRole.SEMANTIC_DOCUMENT,
            relative_path="semantic/semantic.pdf",
        )


def test_snapshot_source_file_fails_closed_without_secure_directory_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "semantic.pdf"
    path.write_bytes(b"%PDF-source")
    monkeypatch.setattr(
        ingestion,
        "_source_directory_fd_supported",
        lambda: False,
        raising=False,
    )

    with pytest.raises(SourceValidationError, match="SECURE_SOURCE_READ_UNAVAILABLE"):
        snapshot_source_file(
            path,
            role=SourceRole.SEMANTIC_DOCUMENT,
            relative_path="semantic/semantic.pdf",
        )
