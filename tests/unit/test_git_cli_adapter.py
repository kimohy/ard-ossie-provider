from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

from ard_ossie.adapters.git_cli import GitCli
from ard_ossie.adapters.subprocess import SubprocessRunner
from ard_ossie.ports.filesystem import PathPolicyError
from ard_ossie.ports.git import CommitResult, GitConflict, GitTransientError
from ard_ossie.ports.process import BinaryCommandResult, CommandRequest, CommandResult

SHA = "a" * 40
NEW_SHA = "b" * 40


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr=stderr)


def failed(returncode: int, stderr: str) -> CommandResult:
    return CommandResult(returncode=returncode, stdout="", stderr=stderr)


class RecordingRunner:
    def __init__(self, results: Iterable[CommandResult]) -> None:
        self.results = iter(results)
        self.requests: list[CommandRequest] = []

    @property
    def argv(self) -> list[tuple[str, ...]]:
        return [request.argv for request in self.requests]

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        return next(self.results)


def test_changed_paths_uses_merge_base_and_nul_delimited_name_only(tmp_path: Path) -> None:
    """Changed path discovery must preserve spaces and use the PR merge base."""
    runner = RecordingRunner(
        [
            ok(f"{SHA}\n"),
            ok("products/sales-order/sources/product document.html\x00"),
        ]
    )

    changed = GitCli(tmp_path, runner).changed_paths("origin/main", "HEAD")

    assert changed.merge_base == SHA
    assert changed.paths == (Path("products/sales-order/sources/product document.html"),)
    assert runner.argv == [
        ("git", "merge-base", "HEAD", "origin/main"),
        ("git", "diff", "--name-only", "-z", f"{SHA}...HEAD"),
    ]


def test_changed_paths_allows_a_deleted_repository_path(tmp_path: Path) -> None:
    """Diff classification must include deleted files without requiring them to exist."""
    runner = RecordingRunner([ok(f"{SHA}\n"), ok("products/old/product.yaml\x00")])

    changed = GitCli(tmp_path, runner).changed_paths("origin/main")

    assert changed.paths == (Path("products/old/product.yaml"),)


def test_worktree_integrity_check_includes_untracked_paths(tmp_path: Path) -> None:
    clean_runner = RecordingRunner([ok("")])
    dirty_runner = RecordingRunner([ok("?? generated-secret.txt\x00")])

    assert GitCli(tmp_path, clean_runner).is_worktree_clean() is True
    assert GitCli(tmp_path, dirty_runner).is_worktree_clean() is False
    assert clean_runner.argv == [("git", "status", "--porcelain=v1", "-z", "--untracked-files=all")]


def test_commit_allowed_paths_rejects_unexpected_status(tmp_path: Path) -> None:
    """A processor write outside its allowlist must be rejected before git add."""
    runner = RecordingRunner([ok(" M README.md\x00")])

    with pytest.raises(GitConflict, match="WRITEBACK_PATH_NOT_ALLOWED"):
        GitCli(tmp_path, runner).commit_allowed_paths("sales-order", "message")

    assert runner.argv == [
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
    ]


def test_commit_allowed_paths_stages_explicit_paths_and_returns_new_sha(tmp_path: Path) -> None:
    """Writeback must never rely on an implicit repository-wide add."""
    generated = tmp_path / "products" / "sales-order" / "generated" / "data-product.md"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated", encoding="utf-8")
    runner = RecordingRunner(
        [
            ok("?? products/sales-order/generated/data-product.md\x00"),
            ok(),
            failed(1, ""),
            ok(),
            ok(),
            ok(),
            ok(f"{NEW_SHA}\n"),
        ]
    )

    result = GitCli(tmp_path, runner).commit_allowed_paths("sales-order", "message")

    assert result.sha == NEW_SHA
    assert result.created is True
    assert runner.argv == [
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("git", "add", "--", "products/sales-order/generated/data-product.md"),
        ("git", "diff", "--cached", "--quiet", "--exit-code"),
        ("git", "config", "user.name", "github-actions[bot]"),
        (
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ),
        ("git", "commit", "-m", "message", "--"),
        ("git", "rev-parse", "--verify", "HEAD"),
    ]


def test_commit_allowed_paths_returns_noop_without_configuring_identity(tmp_path: Path) -> None:
    """An idempotent retry must not create a commit or mutate local git config."""
    runner = RecordingRunner([ok(""), ok(f"{SHA}\n")])

    result = GitCli(tmp_path, runner).commit_allowed_paths("sales-order", "message")

    assert result.sha == SHA
    assert result.created is False
    assert runner.argv == [
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("git", "rev-parse", "--verify", "HEAD"),
    ]


def test_commit_intake_paths_stages_sources_without_processor_outputs(tmp_path: Path) -> None:
    """Intake authority is limited to source/config/manifest paths for one product."""
    source = tmp_path / "products" / "sales-order" / "sources" / "product.html"
    source.parent.mkdir(parents=True)
    source.write_text("<html></html>", encoding="utf-8")
    runner = RecordingRunner(
        [
            ok("?? products/sales-order/sources/product.html\x00"),
            ok(),
            failed(1, ""),
            ok(),
            ok(),
            ok(),
            ok(f"{NEW_SHA}\n"),
        ]
    )

    result = GitCli(tmp_path, runner).commit_intake_paths("sales-order", "intake")

    assert result.sha == NEW_SHA
    assert runner.argv[1] == (
        "git",
        "add",
        "--",
        "products/sales-order/sources/product.html",
    )


def test_commit_intake_paths_rejects_generated_output(tmp_path: Path) -> None:
    """Issue intake must not smuggle generated or Registry writeback into its commit."""
    runner = RecordingRunner([ok(" M products/sales-order/generated/model.json\x00")])

    with pytest.raises(GitConflict, match="INTAKE_PATH_NOT_ALLOWED"):
        GitCli(tmp_path, runner).commit_intake_paths("sales-order", "intake")


def test_commit_changeset_paths_stages_only_central_record(tmp_path: Path) -> None:
    """Coordinator commits must not absorb unrelated Registry mutations."""
    runner = RecordingRunner(
        [
            ok("?? registry/changesets/cst_example.json\x00"),
            ok(),
            failed(1, ""),
            ok(),
            ok(),
            ok(),
            ok(f"{NEW_SHA}\n"),
        ]
    )

    result = GitCli(tmp_path, runner).commit_changeset_paths(
        "cst_example",
        None,
        "changeset",
    )

    assert result.sha == NEW_SHA
    assert runner.argv[1] == (
        "git",
        "add",
        "--",
        "registry/changesets/cst_example.json",
    )


def test_commit_changeset_paths_rejects_unrelated_registry_file(tmp_path: Path) -> None:
    runner = RecordingRunner([ok(" M registry/products/prd_example.json\x00")])

    with pytest.raises(GitConflict, match="CHANGESET_PATH_NOT_ALLOWED"):
        GitCli(tmp_path, runner).commit_changeset_paths("cst_example", None, "changeset")


def test_push_orders_lfs_before_git_and_revalidates_head(tmp_path: Path) -> None:
    """Publishing a branch before its LFS objects would expose an incomplete commit."""
    runner = RecordingRunner([ok(), ok(), ok(f"{SHA}\n")])

    GitCli(tmp_path, runner).push("ard/example", lfs=True)

    assert runner.argv == [
        ("git", "lfs", "push", "origin", "HEAD"),
        ("git", "push", "origin", "HEAD:refs/heads/ard/example"),
        ("git", "rev-parse", "--verify", "HEAD"),
    ]


def test_remote_branch_sha_resolves_exact_head_without_checkout(tmp_path: Path) -> None:
    """PR creation must revalidate the remote ref rather than trust a local checkout."""
    runner = RecordingRunner([ok(f"{SHA}\trefs/heads/feature/sales\n")])

    assert GitCli(tmp_path, runner).remote_branch_sha("feature/sales") == SHA
    assert runner.argv == [("git", "ls-remote", "--heads", "origin", "refs/heads/feature/sales")]


def test_remote_branch_sha_returns_none_for_missing_branch(tmp_path: Path) -> None:
    """A deleted direct-change branch is a typed missing state, not an arbitrary SHA."""
    runner = RecordingRunner([ok("")])

    assert GitCli(tmp_path, runner).remote_branch_sha("feature/missing") is None


def test_push_classifies_lfs_failure_as_transient(tmp_path: Path) -> None:
    """A failed LFS transfer is retryable and must stop the ordinary git push."""
    runner = RecordingRunner([failed(1, "network unavailable")])

    with pytest.raises(GitTransientError, match="LFS_PUSH_FAILED"):
        GitCli(tmp_path, runner).push("ard/example", lfs=True)

    assert len(runner.requests) == 1


def test_push_classifies_non_fast_forward_as_conflict(tmp_path: Path) -> None:
    """A stale branch must be reconciled instead of blindly retried."""
    runner = RecordingRunner([failed(1, "! [rejected] HEAD -> ard/example (non-fast-forward)")])

    with pytest.raises(GitConflict, match="NON_FAST_FORWARD"):
        GitCli(tmp_path, runner).push("ard/example")


def test_read_text_at_uses_validated_revision_and_repository_path(tmp_path: Path) -> None:
    runner = RecordingRunner([ok('{"version":2}\n')])

    value = GitCli(tmp_path, runner).read_text_at(
        SHA,
        "registry/products/prd_example.json",
    )

    assert value == '{"version":2}\n'
    assert runner.argv == [("git", "show", f"{SHA}:registry/products/prd_example.json")]


def test_read_bytes_at_returns_real_git_blob_over_generic_runner_limit(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    payload = b'{"padding":"' + (b"x" * (1024 * 1024 + 17)) + b'"}\n'
    (tmp_path / "large.json").write_bytes(payload)
    subprocess.run(["git", "-C", str(tmp_path), "add", "large.json"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "fixture"], check=True)
    revision = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    actual = GitCli(tmp_path, SubprocessRunner()).read_bytes_at(revision, "large.json")

    assert actual == payload


def test_read_bytes_at_distinguishes_unknown_revision_from_missing_file(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "fixture"], check=True)
    revision = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    adapter = GitCli(tmp_path, SubprocessRunner())

    with pytest.raises(GitConflict) as missing_file:
        adapter.read_bytes_at(revision, "missing.txt")
    with pytest.raises(GitConflict) as missing_revision:
        adapter.read_bytes_at("f" * 40, "missing.txt")

    assert missing_file.value.code == "REVISION_FILE_NOT_FOUND"
    assert missing_revision.value.code == "REVISION_NOT_FOUND"


def test_read_bytes_at_does_not_hide_existing_blob_read_failure(tmp_path: Path) -> None:
    class FailingBlobRunner:
        def run_bytes(
            self,
            request: CommandRequest,
            *,
            max_output_bytes: int,
        ) -> BinaryCommandResult:
            del request, max_output_bytes
            return BinaryCommandResult(
                returncode=128,
                stdout=b"",
                stderr=b"fatal: unable to read blob",
            )

        def run(self, request: CommandRequest) -> CommandResult:
            assert request.argv == (
                "git",
                "ls-tree",
                "-z",
                SHA,
                "--",
                "registry/indexes/product-keys.json",
            )
            return ok("100644 blob deadbeef\tregistry/indexes/product-keys.json\x00")

    with pytest.raises(GitTransientError) as captured:
        GitCli(tmp_path, FailingBlobRunner()).read_bytes_at(
            SHA,
            "registry/indexes/product-keys.json",
        )

    assert captured.value.code == "REVISION_READ_FAILED"


def test_merge_revision_creates_an_explicit_non_fast_forward_merge(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(
        [
            ok(f"{SHA}\n"),
            ok(),
            ok(),
            ok(),
            ok(f"{NEW_SHA}\n"),
        ]
    )

    result = GitCli(tmp_path, runner).merge_revision(NEW_SHA, "sync")

    assert result == CommitResult(sha=NEW_SHA, created=True)
    assert runner.argv == [
        ("git", "rev-parse", "--verify", "HEAD"),
        ("git", "config", "user.name", "github-actions[bot]"),
        (
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ),
        (
            "git",
            "merge",
            "--no-ff",
            "--no-edit",
            "--message",
            "sync",
            NEW_SHA,
        ),
        ("git", "rev-parse", "--verify", "HEAD"),
    ]


def test_merge_revision_returns_noop_when_base_is_already_merged(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner([ok(f"{SHA}\n"), ok(), ok(), ok(), ok(f"{SHA}\n")])

    result = GitCli(tmp_path, runner).merge_revision(NEW_SHA, "sync")

    assert result == CommitResult(sha=SHA, created=False)


def test_merge_revision_aborts_a_conflict(tmp_path: Path) -> None:
    runner = RecordingRunner(
        [
            ok(f"{SHA}\n"),
            ok(),
            ok(),
            failed(1, "CONFLICT (content)"),
            ok(),
        ]
    )

    with pytest.raises(GitConflict, match="BASE_SYNC_MERGE_CONFLICT"):
        GitCli(tmp_path, runner).merge_revision(NEW_SHA, "sync")

    assert runner.argv[-1] == ("git", "merge", "--abort")


def test_merge_revision_requires_a_successful_conflict_abort(tmp_path: Path) -> None:
    runner = RecordingRunner(
        [
            ok(f"{SHA}\n"),
            ok(),
            ok(),
            failed(1, "CONFLICT (content)"),
            failed(128, "no merge to abort"),
        ]
    )

    with pytest.raises(GitTransientError, match="BASE_SYNC_ABORT_FAILED"):
        GitCli(tmp_path, runner).merge_revision(NEW_SHA, "sync")


def test_restore_paths_uses_source_worktree_and_sorted_explicit_paths(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner([ok()])

    GitCli(tmp_path, runner).restore_paths(
        SHA,
        [
            Path("products/sales-order/quality/quality-report.json"),
            Path("products/sales-order/generated/ossie-model.json"),
        ],
    )

    assert runner.argv == [
        (
            "git",
            "restore",
            "--source",
            SHA,
            "--worktree",
            "--",
            "products/sales-order/generated/ossie-model.json",
            "products/sales-order/quality/quality-report.json",
        )
    ]


def test_restore_paths_is_a_noop_for_an_empty_path_set(tmp_path: Path) -> None:
    runner = RecordingRunner([])

    GitCli(tmp_path, runner).restore_paths(SHA, [])

    assert runner.argv == []


def test_restore_paths_classifies_a_git_failure(tmp_path: Path) -> None:
    runner = RecordingRunner([failed(128, "pathspec did not match")])

    with pytest.raises(GitTransientError, match="BASE_SYNC_RESTORE_FAILED"):
        GitCli(tmp_path, runner).restore_paths(
            SHA,
            [Path("products/sales-order/generated/ossie-model.json")],
        )


def test_restore_paths_rejects_a_path_outside_the_repository(tmp_path: Path) -> None:
    runner = RecordingRunner([])

    with pytest.raises(PathPolicyError, match="PATH_OUTSIDE_REPOSITORY"):
        GitCli(tmp_path, runner).restore_paths(SHA, [Path("../secret")])

    assert runner.argv == []


def test_create_annotated_tag_reuses_exact_target(tmp_path: Path) -> None:
    """An idempotent release retry must reuse an immutable tag at the same commit."""
    runner = RecordingRunner([ok(f"{SHA}\n")])

    GitCli(tmp_path, runner).create_annotated_tag("product/prd_example/v1", SHA, "release")

    assert len(runner.requests) == 1


def test_create_annotated_tag_configures_bot_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh Actions checkouts must be able to create annotated release tags."""
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    git("add", "README.md")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.com",
        "commit",
        "-qm",
        "fixture",
    )
    revision = git("rev-parse", "HEAD")

    GitCli(repository, SubprocessRunner()).create_annotated_tag(
        "product/prd_example/v1",
        revision,
        "release",
    )

    assert git("cat-file", "-t", "product/prd_example/v1") == "tag"
    assert git("config", "--local", "user.name") == "github-actions[bot]"
    assert (
        git("config", "--local", "user.email")
        == "41898282+github-actions[bot]@users.noreply.github.com"
    )


def test_create_annotated_tag_rejects_other_target(tmp_path: Path) -> None:
    """An existing immutable tag must never move to another commit."""
    runner = RecordingRunner([ok(f"{NEW_SHA}\n")])

    with pytest.raises(GitConflict, match="TAG_TARGET_CONFLICT"):
        GitCli(tmp_path, runner).create_annotated_tag("product/prd_example/v1", SHA, "release")
