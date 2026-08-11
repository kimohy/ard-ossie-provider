from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ard_ossie.application.contracts import WorkflowSecurityError


class PathPolicyError(WorkflowSecurityError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class FileSystemPort(Protocol):
    root: Path

    def resolve_read(self, path: str | Path) -> Path: ...

    def resolve_directory(
        self,
        path: str | Path,
        *,
        allow_missing: bool = False,
    ) -> Path: ...

    def resolve_write(self, path: str | Path) -> Path: ...

    def is_writeback_allowed(self, path: str | Path, product_key: str) -> bool: ...

    def is_intake_write_allowed(self, path: str | Path, product_key: str) -> bool: ...

    def is_changeset_write_allowed(
        self,
        path: str | Path,
        changeset_id: str,
        product_key: str | None,
    ) -> bool: ...
