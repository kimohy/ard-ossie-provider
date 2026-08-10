from __future__ import annotations

import os
import re
from pathlib import Path

from ard_ossie.ports.filesystem import PathPolicyError

_PRODUCT_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class RepositoryPaths:
    def __init__(self, root: str | Path) -> None:
        supplied_root = Path(root).expanduser()
        if supplied_root.is_symlink():
            raise PathPolicyError("SYMLINK_NOT_ALLOWED", "repository root is a symlink")
        self.root = supplied_root.resolve()
        if not self.root.is_dir():
            raise PathPolicyError("REPOSITORY_NOT_FOUND", "repository root does not exist")

    def resolve_read(self, path: str | Path) -> Path:
        candidate = self._lexical_candidate(path)
        self._reject_symlinks(candidate)
        resolved = candidate.resolve()
        self._require_below_root(resolved)
        if not resolved.exists():
            raise PathPolicyError("READ_PATH_NOT_FOUND", "read path does not exist")
        if not (resolved.is_file() or resolved.is_dir()):
            raise PathPolicyError("READ_PATH_TYPE_NOT_ALLOWED", "read path has an invalid type")
        return resolved

    def resolve_write(self, path: str | Path) -> Path:
        candidate = self._lexical_candidate(path)
        relative = candidate.relative_to(self.root)
        if ".git" in relative.parts:
            raise PathPolicyError(
                "GIT_METADATA_WRITE_NOT_ALLOWED",
                "writes below .git are forbidden",
            )
        self._reject_symlinks(candidate)
        resolved = candidate.resolve(strict=False)
        self._require_below_root(resolved)
        return resolved

    def is_writeback_allowed(self, path: str | Path, product_key: str) -> bool:
        if not _PRODUCT_KEY.fullmatch(product_key):
            raise PathPolicyError("INVALID_PRODUCT_KEY", "product key is not canonical")
        resolved = self.resolve_write(path)
        parts = resolved.relative_to(self.root).parts
        if len(parts) >= 2 and parts[0] == "registry":
            return True
        if len(parts) == 3 and parts == ("products", product_key, "product.yaml"):
            return True
        return (
            len(parts) >= 4
            and parts[0] == "products"
            and parts[1] == product_key
            and parts[2] in {"generated", "quality"}
        )

    def is_intake_write_allowed(self, path: str | Path, product_key: str) -> bool:
        if not _PRODUCT_KEY.fullmatch(product_key):
            raise PathPolicyError("INVALID_PRODUCT_KEY", "product key is not canonical")
        resolved = self.resolve_write(path)
        parts = resolved.relative_to(self.root).parts
        if len(parts) < 3 or parts[:2] != ("products", product_key):
            return False
        if len(parts) == 3:
            return parts[2] in {"product.yaml", "intake-manifest.json"}
        return parts[2] == "sources"

    def is_changeset_write_allowed(
        self,
        path: str | Path,
        changeset_id: str,
        product_key: str | None,
    ) -> bool:
        if product_key is not None and not _PRODUCT_KEY.fullmatch(product_key):
            raise PathPolicyError("INVALID_PRODUCT_KEY", "product key is not canonical")
        resolved = self.resolve_write(path)
        parts = resolved.relative_to(self.root).parts
        if product_key is None:
            return parts == ("registry", "changesets", f"{changeset_id}.json")
        return parts == (
            "products",
            product_key,
            "changesets",
            f"{changeset_id}.json",
        )

    def _lexical_candidate(self, path: str | Path) -> Path:
        supplied = Path(path)
        if "\x00" in os.fspath(supplied):
            raise PathPolicyError("INVALID_PATH", "path contains NUL")
        joined = supplied if supplied.is_absolute() else self.root / supplied
        candidate = Path(os.path.abspath(joined))
        self._require_below_root(candidate)
        return candidate

    def _reject_symlinks(self, candidate: Path) -> None:
        current = self.root
        for part in candidate.relative_to(self.root).parts:
            current /= part
            if current.is_symlink():
                raise PathPolicyError("SYMLINK_NOT_ALLOWED", "path contains a symlink")
            if not current.exists():
                break

    def _require_below_root(self, candidate: Path) -> None:
        if not candidate.is_relative_to(self.root):
            raise PathPolicyError(
                "PATH_OUTSIDE_REPOSITORY",
                "path resolves outside the repository",
            )
