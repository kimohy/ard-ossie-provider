from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.ports.git import (
    ChangedPaths,
    CommitResult,
    GitConflict,
    GitTransientError,
)
from ard_ossie.ports.process import CommandRequest, CommandResult, CommandRunner

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_BOT_NAME = "github-actions[bot]"
_BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class GitCli:
    def __init__(
        self,
        repository: str | Path,
        runner: CommandRunner,
        *,
        paths: RepositoryPaths | None = None,
    ) -> None:
        self.paths = paths or RepositoryPaths(repository)
        self.repository = self.paths.root
        self.runner = runner

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        _validate_revision(base_ref)
        _validate_revision(head_ref)
        merge = self._git("merge-base", head_ref, base_ref)
        if merge.returncode != 0:
            raise GitConflict("MERGE_BASE_NOT_FOUND", merge.stderr or "merge base unavailable")
        merge_base = _validated_sha(merge.stdout)
        diff = self._git("diff", "--name-only", "-z", f"{merge_base}...{head_ref}")
        self._require_success(diff, "CHANGED_PATHS_FAILED")
        paths = tuple(self._repository_relative(item) for item in _split_paths(diff.stdout))
        return ChangedPaths(merge_base=merge_base, paths=tuple(dict.fromkeys(paths)))

    def current_sha(self) -> str:
        result = self._git("rev-parse", "--verify", "HEAD")
        self._require_success(result, "HEAD_RESOLUTION_FAILED")
        return _validated_sha(result.stdout)

    def is_worktree_clean(self) -> bool:
        result = self._git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        self._require_success(result, "STATUS_FAILED")
        return not result.stdout

    def remote_branch_sha(self, branch: str) -> str | None:
        _validate_ref(branch)
        ref = f"refs/heads/{branch}"
        result = self._git("ls-remote", "--heads", "origin", ref)
        self._require_success(result, "REMOTE_BRANCH_LOOKUP_FAILED")
        records = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if not records:
            return None
        if len(records) != 1 or len(records[0]) != 2 or records[0][1] != ref:
            raise GitConflict("AMBIGUOUS_REMOTE_BRANCH", branch)
        return _validated_sha(records[0][0])

    def branch_exists(self, branch: str) -> bool:
        _validate_ref(branch)
        result = self._git("ls-remote", "--exit-code", "--heads", "origin", branch)
        if result.returncode == 0:
            return True
        if result.returncode == 2 and not result.stdout.strip():
            return False
        raise GitTransientError("REMOTE_BRANCH_LOOKUP_FAILED", result.stderr or branch)

    def switch_or_create(self, branch: str, base_ref: str) -> None:
        _validate_ref(branch)
        _validate_revision(base_ref)
        if self.branch_exists(branch):
            remote_ref = f"refs/remotes/origin/{branch}"
            self._run_required(
                "BRANCH_FETCH_FAILED",
                "fetch",
                "origin",
                f"refs/heads/{branch}:{remote_ref}",
            )
            self._run_required(
                "BRANCH_SWITCH_FAILED",
                "switch",
                "--force-create",
                branch,
                remote_ref,
            )
            return
        self._run_required("BRANCH_CREATE_FAILED", "switch", "--create", branch, base_ref)

    def merge_revision(self, revision: str, message: str) -> CommitResult:
        _validate_revision(revision)
        previous = self.current_sha()
        self._run_required("GIT_IDENTITY_FAILED", "config", "user.name", _BOT_NAME)
        self._run_required("GIT_IDENTITY_FAILED", "config", "user.email", _BOT_EMAIL)
        result = self._git(
            "merge",
            "--no-ff",
            "--no-edit",
            "--message",
            message,
            revision,
        )
        if result.returncode != 0:
            abort = self._git("merge", "--abort")
            if abort.returncode != 0:
                raise GitTransientError(
                    "BASE_SYNC_ABORT_FAILED",
                    abort.stderr or abort.stdout or "merge abort failed",
                )
            raise GitConflict(
                "BASE_SYNC_MERGE_CONFLICT",
                result.stderr or result.stdout or "base merge failed",
            )
        current = self.current_sha()
        return CommitResult(sha=current, created=current != previous)

    def restore_paths(self, revision: str, paths: Sequence[Path]) -> None:
        _validate_revision(revision)
        relative = sorted(
            {self._repository_relative(path) for path in paths},
            key=lambda path: path.as_posix(),
        )
        if not relative:
            return
        self._run_required(
            "BASE_SYNC_RESTORE_FAILED",
            "restore",
            "--source",
            revision,
            "--worktree",
            "--",
            *(path.as_posix() for path in relative),
        )

    def commit_allowed_paths(self, product_key: str, message: str) -> CommitResult:
        return self._commit_product_paths(product_key, message, intake=False)

    def commit_intake_paths(self, product_key: str, message: str) -> CommitResult:
        return self._commit_product_paths(product_key, message, intake=True)

    def commit_changeset_paths(
        self,
        changeset_id: str,
        product_key: str | None,
        message: str,
    ) -> CommitResult:
        return self._commit_paths(
            message,
            lambda path: self.paths.is_changeset_write_allowed(
                path,
                changeset_id,
                product_key,
            ),
            "CHANGESET_PATH_NOT_ALLOWED",
        )

    def _commit_product_paths(
        self,
        product_key: str,
        message: str,
        *,
        intake: bool,
    ) -> CommitResult:
        return self._commit_paths(
            message,
            lambda path: (
                self.paths.is_intake_write_allowed(path, product_key)
                if intake
                else self.paths.is_writeback_allowed(path, product_key)
            ),
            "INTAKE_PATH_NOT_ALLOWED" if intake else "WRITEBACK_PATH_NOT_ALLOWED",
        )

    def _commit_paths(
        self,
        message: str,
        allowed: Callable[[Path], bool],
        conflict_code: str,
    ) -> CommitResult:
        status = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        self._require_success(status, "STATUS_FAILED")
        changed = _parse_status_paths(status.stdout)
        if not changed:
            return CommitResult(sha=self.current_sha(), created=False)

        relative_paths: list[Path] = []
        for item in changed:
            relative = self._repository_relative(item)
            if not allowed(relative):
                raise GitConflict(conflict_code, relative.as_posix())
            relative_paths.append(relative)
        relative_paths = list(dict.fromkeys(relative_paths))

        self._run_required(
            "GIT_ADD_FAILED",
            "add",
            "--",
            *(item.as_posix() for item in relative_paths),
        )
        staged = self._git("diff", "--cached", "--quiet", "--exit-code")
        if staged.returncode == 0:
            return CommitResult(sha=self.current_sha(), created=False)
        if staged.returncode != 1:
            raise GitTransientError("STAGED_DIFF_FAILED", staged.stderr or "git diff failed")

        self._run_required("GIT_IDENTITY_FAILED", "config", "user.name", _BOT_NAME)
        self._run_required("GIT_IDENTITY_FAILED", "config", "user.email", _BOT_EMAIL)
        self._run_required("COMMIT_FAILED", "commit", "-m", message, "--")
        return CommitResult(sha=self.current_sha(), created=True)

    def push(self, branch: str, *, lfs: bool = False) -> None:
        _validate_ref(branch)
        if lfs:
            result = self._git("lfs", "push", "origin", "HEAD", timeout_seconds=600)
            if result.returncode != 0:
                raise GitTransientError("LFS_PUSH_FAILED", result.stderr or branch)
        result = self._git("push", "origin", f"HEAD:refs/heads/{branch}", timeout_seconds=300)
        if result.returncode != 0:
            diagnostic = f"{result.stdout}\n{result.stderr}".casefold()
            if "non-fast-forward" in diagnostic or "fetch first" in diagnostic:
                raise GitConflict("NON_FAST_FORWARD", result.stderr or branch)
            raise GitTransientError("GIT_PUSH_FAILED", result.stderr or branch)
        self.current_sha()

    def tag_target(self, tag: str) -> str | None:
        _validate_ref(tag)
        result = self._git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
        if result.returncode == 1:
            return None
        self._require_success(result, "TAG_LOOKUP_FAILED")
        return _validated_sha(result.stdout)

    def create_annotated_tag(self, tag: str, target: str, message: str) -> None:
        _validate_ref(tag)
        target = _validated_sha(target)
        existing = self.tag_target(tag)
        if existing is not None:
            if existing != target:
                raise GitConflict("TAG_TARGET_CONFLICT", f"{tag} already targets {existing}")
            return
        self._run_required(
            "TAG_CREATE_FAILED",
            "tag",
            "--annotate",
            tag,
            target,
            "--message",
            message,
        )
        if self.tag_target(tag) != target:
            raise GitTransientError("TAG_VERIFICATION_FAILED", tag)

    def push_tags(self, tags: Sequence[str]) -> None:
        if not tags:
            return
        for tag in tags:
            _validate_ref(tag)
        result = self._git(
            "push",
            "origin",
            *(f"refs/tags/{tag}:refs/tags/{tag}" for tag in tags),
            timeout_seconds=300,
        )
        if result.returncode != 0:
            diagnostic = f"{result.stdout}\n{result.stderr}".casefold()
            if "already exists" in diagnostic or "rejected" in diagnostic:
                raise GitConflict("TAG_PUSH_CONFLICT", result.stderr or "tag push rejected")
            raise GitTransientError("TAG_PUSH_FAILED", result.stderr or "tag push failed")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        ancestor = _validated_sha(ancestor)
        descendant = _validated_sha(descendant)
        result = self._git("merge-base", "--is-ancestor", ancestor, descendant)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitTransientError("ANCESTRY_CHECK_FAILED", result.stderr or ancestor)

    def read_text_at(self, revision: str, path: str | Path) -> str:
        revision = _validated_sha(revision)
        relative = self._repository_relative(path)
        result = self._git("show", f"{revision}:{relative.as_posix()}")
        if result.returncode != 0:
            raise GitConflict(
                "REVISION_FILE_NOT_FOUND",
                result.stderr or relative.as_posix(),
            )
        return result.stdout

    def _repository_relative(self, value: str | Path) -> Path:
        resolved = self.paths.resolve_write(value)
        return resolved.relative_to(self.repository)

    def _run_required(self, code: str, *arguments: str) -> CommandResult:
        result = self._git(*arguments)
        self._require_success(result, code)
        return result

    def _git(self, *arguments: str, timeout_seconds: int = 60) -> CommandResult:
        return self.runner.run(
            CommandRequest(
                argv=("git", *arguments),
                cwd=self.repository,
                timeout_seconds=timeout_seconds,
            )
        )

    @staticmethod
    def _require_success(result: CommandResult, code: str) -> None:
        if result.returncode != 0:
            raise GitTransientError(code, result.stderr or result.stdout or "git command failed")


def _split_paths(value: str) -> list[str]:
    records = value.split("\x00") if "\x00" in value else value.splitlines()
    return [record for record in records if record]


def _parse_status_paths(value: str) -> list[str]:
    records = value.split("\x00")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise GitConflict("MALFORMED_GIT_STATUS", record[:20])
        status = record[:2]
        paths.append(record[3:])
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise GitConflict("MALFORMED_GIT_STATUS", "rename source is missing")
            paths.append(records[index])
            index += 1
    return paths


def _validated_sha(value: str) -> str:
    normalized = value.strip().casefold()
    if not _SHA.fullmatch(normalized):
        raise GitConflict("INVALID_GIT_SHA", normalized[:80])
    return normalized


def _validate_ref(value: str) -> None:
    if (
        not _REF.fullmatch(value)
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise GitConflict("INVALID_GIT_REF", value[:80])


def _validate_revision(value: str) -> None:
    if value == "HEAD":
        return
    _validate_ref(value)
