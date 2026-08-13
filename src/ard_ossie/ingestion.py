from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema

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
    snapshot: SkipJsonSchema[bytes] = Field(exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def acquire_legacy_snapshot(cls, value: object) -> object:
        if not isinstance(value, dict) or "snapshot" in value or "path" not in value:
            return value
        snapshot = _read_snapshot(
            Path(value["path"]),
            maximum=SourceLimits().max_bytes_per_file,
        )
        declared_size = value.get("size_bytes")
        if isinstance(declared_size, int) and len(snapshot) != declared_size:
            raise ValueError("SOURCE_SNAPSHOT_SIZE_MISMATCH")
        declared_hash = value.get("sha256")
        if (
            isinstance(declared_hash, str)
            and hashlib.sha256(snapshot).hexdigest() != declared_hash
        ):
            raise ValueError("SOURCE_SNAPSHOT_HASH_MISMATCH")
        return {**value, "snapshot": snapshot}

    @model_validator(mode="after")
    def validate_snapshot(self) -> SourceFile:
        if len(self.snapshot) != self.size_bytes:
            raise ValueError("SOURCE_SNAPSHOT_SIZE_MISMATCH")
        if hashlib.sha256(self.snapshot).hexdigest() != self.sha256:
            raise ValueError("SOURCE_SNAPSHOT_HASH_MISMATCH")
        return self


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
_SOURCE_OPEN_DIR_FD_SUPPORTED = os.open in os.supports_dir_fd
_SOURCE_STAT_DIR_FD_SUPPORTED = os.stat in os.supports_dir_fd
_SOURCE_STAT_NOFOLLOW_SUPPORTED = os.stat in os.supports_follow_symlinks
_SOURCE_LISTDIR_FD_SUPPORTED = os.listdir in os.supports_fd


def scan_sources(
    sources_root: str | Path,
    *,
    limits: SourceLimits | None = None,
) -> SourceManifest:
    root = Path(os.path.abspath(os.fspath(sources_root)))
    active_limits = limits or SourceLimits()
    files: list[SourceFile] = []
    try:
        root_anchors = _open_source_directory_chain(root)
    except FileNotFoundError as error:
        raise SourceValidationError("MISSING_PRODUCT_HTML") from error
    role_anchors: list[_SourceAnchor] = []
    try:
        for role, (directory_name, extensions) in _ROLE_SPECS.items():
            missing_code = {
                SourceRole.PRODUCT_HTML: "MISSING_PRODUCT_HTML",
                SourceRole.SEMANTIC_DOCUMENT: "MISSING_SEMANTIC_DOCUMENT",
                SourceRole.DICTIONARY_EXCEL: "MISSING_DICTIONARY",
            }[role]
            try:
                role_anchor = _open_source_directory_at(
                    root_anchors[-1][2],
                    directory_name,
                )
            except FileNotFoundError as error:
                raise SourceValidationError(missing_code) from error
            role_anchors.append(role_anchor)
            directory_descriptor = role_anchor[2]
            chain = [*root_anchors, role_anchor]
            candidates: list[str] = []
            for name in sorted(os.listdir(directory_descriptor)):
                try:
                    entry = _source_entry_stat(directory_descriptor, name)
                except FileNotFoundError as error:
                    raise SourceValidationError("SOURCE_PATH_CHANGED") from error
                if stat.S_ISLNK(entry.st_mode):
                    raise SourceValidationError(
                        f"SYMLINK_NOT_ALLOWED: {root / directory_name / name}"
                    )
                if Path(name).suffix.casefold() in extensions and stat.S_ISREG(entry.st_mode):
                    candidates.append(name)
            if not candidates:
                raise SourceValidationError(missing_code)
            if len(candidates) > 1:
                code = {
                    SourceRole.PRODUCT_HTML: "MULTIPLE_PRODUCT_HTML_DOCUMENTS",
                    SourceRole.SEMANTIC_DOCUMENT: "MULTIPLE_SEMANTIC_DOCUMENTS",
                    SourceRole.DICTIONARY_EXCEL: "MULTIPLE_DICTIONARY_DOCUMENTS",
                }[role]
                raise SourceValidationError(code)

            name = candidates[0]
            path = root / directory_name / name
            snapshot = _read_snapshot_at(
                directory_descriptor,
                name,
                path=path,
                maximum=active_limits.max_bytes_per_file,
                anchors=chain,
            )
            _validate_signature(path, snapshot)
            files.append(
                SourceFile(
                    role=role,
                    path=path,
                    relative_path=f"{directory_name}/{name}",
                    sha256=hashlib.sha256(snapshot).hexdigest(),
                    size_bytes=len(snapshot),
                    snapshot=snapshot,
                )
            )

        for role_anchor in role_anchors:
            _require_source_chain_identity([*root_anchors, role_anchor])
        return SourceManifest(files=sorted(files, key=lambda item: item.role.value))
    finally:
        for _parent, _name, descriptor, _expected in reversed(role_anchors):
            os.close(descriptor)
        _close_source_anchors(root_anchors)


def _validate_signature(path: Path, snapshot: bytes) -> None:
    head = snapshot[:8]
    suffix = path.suffix.lower()
    if suffix in {".docx", ".xlsx"} and not head.startswith(b"PK\x03\x04"):
        raise SourceValidationError(f"SOURCE_SIGNATURE_MISMATCH: {path}")
    if suffix == ".pdf" and not head.startswith(b"%PDF"):
        raise SourceValidationError(f"SOURCE_SIGNATURE_MISMATCH: {path}")
    if suffix in {".html", ".htm"} and b"<" not in head:
        raise SourceValidationError(f"SOURCE_SIGNATURE_MISMATCH: {path}")


def source_bytes(source: SourceFile) -> bytes:
    """Return the immutable source snapshot acquired during ingestion."""
    return source.snapshot


@contextmanager
def materialized_source_path(source: SourceFile) -> Iterator[Path]:
    """Expose a private, short-lived path backed only by the source snapshot."""
    with tempfile.TemporaryDirectory(prefix="ard-source-") as directory:
        path = Path(directory) / f"source{source.path.suffix.casefold()}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            remaining = memoryview(source.snapshot)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise OSError("source snapshot write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        yield path


def snapshot_source_file(
    path: str | Path,
    *,
    role: SourceRole,
    relative_path: str,
    maximum: int = SourceLimits().max_bytes_per_file,
) -> SourceFile:
    source_path = Path(path)
    snapshot = _read_snapshot(source_path, maximum=maximum)
    _validate_signature(source_path, snapshot)
    return SourceFile(
        role=role,
        path=source_path,
        relative_path=relative_path,
        sha256=hashlib.sha256(snapshot).hexdigest(),
        size_bytes=len(snapshot),
        snapshot=snapshot,
    )


def _read_snapshot(path: Path, *, maximum: int) -> bytes:
    if not _source_directory_fd_supported():
        raise SourceValidationError("SECURE_SOURCE_READ_UNAVAILABLE")
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        anchors = _open_source_directory_chain(absolute.parent)
    except FileNotFoundError as error:
        raise SourceValidationError("SOURCE_PATH_CHANGED") from error
    try:
        return _read_snapshot_at(
            anchors[-1][2],
            absolute.name,
            path=absolute,
            maximum=maximum,
            anchors=anchors,
        )
    finally:
        _close_source_anchors(anchors)


_SourceAnchor = tuple[int | None, str | None, int, os.stat_result]


def _source_directory_fd_supported() -> bool:
    return bool(
        getattr(os, "O_DIRECTORY", 0)
        and getattr(os, "O_NOFOLLOW", 0)
        and _SOURCE_OPEN_DIR_FD_SUPPORTED
        and _SOURCE_STAT_DIR_FD_SUPPORTED
        and _SOURCE_STAT_NOFOLLOW_SUPPORTED
        and _SOURCE_LISTDIR_FD_SUPPORTED
    )


def _open_source_directory_chain(directory: Path) -> list[_SourceAnchor]:
    if not _source_directory_fd_supported():
        raise SourceValidationError("SECURE_SOURCE_READ_UNAVAILABLE")
    if not directory.is_absolute():
        raise SourceValidationError("SOURCE_PATH_CHANGED")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_descriptor = os.open(os.path.sep, flags)
    try:
        root_status = os.fstat(root_descriptor)
    except Exception:
        os.close(root_descriptor)
        raise
    anchors: list[_SourceAnchor] = [(None, None, root_descriptor, root_status)]
    try:
        for component in directory.parts[1:]:
            anchors.append(_open_source_directory_at(anchors[-1][2], component))
        return anchors
    except Exception:
        _close_source_anchors(anchors)
        raise


def _open_source_directory_at(parent_descriptor: int, name: str) -> _SourceAnchor:
    expected = _source_entry_stat(parent_descriptor, name)
    if stat.S_ISLNK(expected.st_mode):
        raise SourceValidationError(f"SYMLINK_NOT_ALLOWED: {name}")
    if not stat.S_ISDIR(expected.st_mode):
        raise SourceValidationError("SOURCE_PATH_CHANGED")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SourceValidationError(f"SYMLINK_NOT_ALLOWED: {name}") from error
        if _is_source_path_race_error(error):
            raise SourceValidationError("SOURCE_PATH_CHANGED") from error
        raise
    try:
        actual = os.fstat(descriptor)
        _require_same_source_identity(actual, expected)
    except Exception:
        os.close(descriptor)
        raise
    return (parent_descriptor, name, descriptor, actual)


def _read_snapshot_at(
    directory_descriptor: int,
    name: str,
    *,
    path: Path,
    maximum: int,
    anchors: list[_SourceAnchor],
) -> bytes:
    try:
        before = _source_entry_stat(directory_descriptor, name)
    except FileNotFoundError as error:
        raise SourceValidationError("SOURCE_PATH_CHANGED") from error
    if stat.S_ISLNK(before.st_mode):
        raise SourceValidationError(f"SYMLINK_NOT_ALLOWED: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise SourceValidationError(f"SOURCE_IS_NOT_A_FILE: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SourceValidationError(f"SYMLINK_NOT_ALLOWED: {path}") from error
        if _is_source_path_race_error(error):
            raise SourceValidationError("SOURCE_PATH_CHANGED") from error
        raise
    try:
        opened = os.fstat(descriptor)
        _require_same_source_file(opened, before)
        chunks: list[bytes] = []
        retained = 0
        while retained <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - retained))
            if not chunk:
                break
            chunks.append(chunk)
            retained += len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise SourceValidationError(f"SOURCE_TOO_LARGE: {path} ({len(payload)} bytes)")
        after = os.fstat(descriptor)
        try:
            _require_same_source_file(after, opened)
        except SourceValidationError as error:
            raise SourceValidationError("SOURCE_CHANGED_DURING_READ") from error
        if len(payload) != after.st_size:
            raise SourceValidationError("SOURCE_CHANGED_DURING_READ")
        try:
            current = _source_entry_stat(directory_descriptor, name)
            _require_same_source_file(current, after)
        except FileNotFoundError as error:
            raise SourceValidationError("SOURCE_PATH_CHANGED") from error
        except SourceValidationError as error:
            raise SourceValidationError("SOURCE_PATH_CHANGED") from error
        _require_source_chain_identity(anchors)
        return payload
    finally:
        os.close(descriptor)


def _source_entry_stat(parent_descriptor: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as error:
        if _is_source_path_race_error(error) and error.errno != errno.ENOENT:
            raise SourceValidationError("SOURCE_PATH_CHANGED") from error
        raise


def _require_source_chain_identity(anchors: list[_SourceAnchor]) -> None:
    for parent_descriptor, name, descriptor, expected in anchors:
        _require_same_source_identity(os.fstat(descriptor), expected)
        if parent_descriptor is None or name is None:
            continue
        try:
            current = _source_entry_stat(parent_descriptor, name)
        except FileNotFoundError as error:
            raise SourceValidationError("SOURCE_PATH_CHANGED") from error
        if stat.S_ISLNK(current.st_mode):
            raise SourceValidationError(f"SYMLINK_NOT_ALLOWED: {name}")
        _require_same_source_identity(current, expected)


def _require_same_source_identity(
    current: os.stat_result,
    expected: os.stat_result,
) -> None:
    if (
        current.st_dev,
        current.st_ino,
        stat.S_IFMT(current.st_mode),
    ) != (
        expected.st_dev,
        expected.st_ino,
        stat.S_IFMT(expected.st_mode),
    ):
        raise SourceValidationError("SOURCE_PATH_CHANGED")


def _require_same_source_file(
    current: os.stat_result,
    expected: os.stat_result,
) -> None:
    _require_same_source_identity(current, expected)
    if not stat.S_ISREG(current.st_mode) or (
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    ) != (
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    ):
        raise SourceValidationError("SOURCE_PATH_CHANGED")


def _close_source_anchors(anchors: list[_SourceAnchor]) -> None:
    for _parent, _name, descriptor, _expected in reversed(anchors):
        os.close(descriptor)


def _is_source_path_race_error(error: OSError) -> bool:
    return error.errno in {
        errno.ENOENT,
        errno.ENOTDIR,
        errno.ELOOP,
        getattr(errno, "ESTALE", -1),
    }
