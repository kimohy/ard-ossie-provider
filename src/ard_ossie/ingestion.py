from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from ard_ossie.models import Sha256, StrictModel


class SourceRole(StrEnum):
    PRODUCT_HTML = "product_html"
    SEMANTIC_DOCUMENT = "semantic_document"
    DICTIONARY_EXCEL = "dictionary_excel"


class SourceFile(StrictModel):
    role: SourceRole
    path: Path
    relative_path: str
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class SourceManifest(StrictModel):
    files: list[SourceFile]

    def by_role(self, role: SourceRole) -> SourceFile:
        return next(item for item in self.files if item.role is role)


@dataclass(frozen=True)
class SourceLimits:
    max_bytes_per_file: int = 50 * 1024 * 1024


class SourceValidationError(ValueError):
    pass


_ROLE_SPECS = {
    SourceRole.PRODUCT_HTML: ("product-info", frozenset({".html", ".htm"})),
    SourceRole.SEMANTIC_DOCUMENT: ("semantic", frozenset({".docx", ".pdf"})),
    SourceRole.DICTIONARY_EXCEL: ("dictionary", frozenset({".xlsx"})),
}


def scan_sources(
    sources_root: str | Path,
    *,
    limits: SourceLimits | None = None,
) -> SourceManifest:
    root = Path(sources_root)
    active_limits = limits or SourceLimits()
    files: list[SourceFile] = []

    for role, (directory_name, extensions) in _ROLE_SPECS.items():
        directory = root / directory_name
        entries = sorted(directory.glob("*"))
        symlink = next((path for path in entries if path.is_symlink()), None)
        if symlink is not None:
            raise SourceValidationError(f"SYMLINK_NOT_ALLOWED: {symlink}")
        candidates = sorted(
            path for path in entries if path.suffix.lower() in extensions and path.is_file()
        )
        if not candidates:
            code = {
                SourceRole.PRODUCT_HTML: "MISSING_PRODUCT_HTML",
                SourceRole.SEMANTIC_DOCUMENT: "MISSING_SEMANTIC_DOCUMENT",
                SourceRole.DICTIONARY_EXCEL: "MISSING_DICTIONARY",
            }[role]
            raise SourceValidationError(code)
        if len(candidates) > 1:
            code = {
                SourceRole.PRODUCT_HTML: "MULTIPLE_PRODUCT_HTML_DOCUMENTS",
                SourceRole.SEMANTIC_DOCUMENT: "MULTIPLE_SEMANTIC_DOCUMENTS",
                SourceRole.DICTIONARY_EXCEL: "MULTIPLE_DICTIONARY_DOCUMENTS",
            }[role]
            raise SourceValidationError(code)

        path = candidates[0]
        size = path.stat().st_size
        if size > active_limits.max_bytes_per_file:
            raise SourceValidationError(f"SOURCE_TOO_LARGE: {path} ({size} bytes)")
        _validate_signature(path)
        files.append(
            SourceFile(
                role=role,
                path=path,
                relative_path=path.relative_to(root).as_posix(),
                sha256=_sha256(path),
                size_bytes=size,
            )
        )

    return SourceManifest(files=sorted(files, key=lambda item: item.role.value))


def _validate_signature(path: Path) -> None:
    head = path.read_bytes()[:8]
    suffix = path.suffix.lower()
    if suffix in {".docx", ".xlsx"} and not head.startswith(b"PK\x03\x04"):
        raise SourceValidationError(f"SOURCE_SIGNATURE_MISMATCH: {path}")
    if suffix == ".pdf" and not head.startswith(b"%PDF"):
        raise SourceValidationError(f"SOURCE_SIGNATURE_MISMATCH: {path}")
    if suffix in {".html", ".htm"} and b"<" not in head:
        raise SourceValidationError(f"SOURCE_SIGNATURE_MISMATCH: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
