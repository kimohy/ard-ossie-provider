from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ard_ossie.application.contracts import WorkflowConflict, WorkflowTransientError


class GitConflict(WorkflowConflict):
    pass


class GitTransientError(WorkflowTransientError):
    pass


@dataclass(frozen=True)
class ChangedPaths:
    merge_base: str
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class CommitResult:
    sha: str
    created: bool


class GitPort(Protocol):
    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths: ...

    def current_sha(self) -> str: ...

    def is_worktree_clean(self) -> bool: ...

    def remote_branch_sha(self, branch: str) -> str | None: ...

    def branch_exists(self, branch: str) -> bool: ...

    def switch_or_create(self, branch: str, base_ref: str) -> None: ...

    def merge_revision(self, revision: str, message: str) -> CommitResult: ...

    def restore_paths(self, revision: str, paths: Sequence[Path]) -> None: ...

    def commit_allowed_paths(self, product_key: str, message: str) -> CommitResult: ...

    def commit_intake_paths(self, product_key: str, message: str) -> CommitResult: ...

    def commit_changeset_paths(
        self,
        changeset_id: str,
        product_key: str | None,
        message: str,
    ) -> CommitResult: ...

    def push(self, branch: str, *, lfs: bool = False) -> None: ...

    def tag_target(self, tag: str) -> str | None: ...

    def create_annotated_tag(self, tag: str, target: str, message: str) -> None: ...

    def push_tags(self, tags: Sequence[str]) -> None: ...

    def is_ancestor(self, ancestor: str, descendant: str) -> bool: ...

    def read_text_at(self, revision: str, path: str | Path) -> str: ...

    def read_bytes_at(self, revision: str, path: str | Path) -> bytes: ...
