from __future__ import annotations

from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
)
from ard_ossie.application.finalize import FinalizeRequest, FinalizeService

SHA = "a" * 40
CURRENT_SHA = "b" * 40


class FakeGitHub:
    def __init__(self) -> None:
        self.issue_labels = {"ard:approved", "ard:processing", "ard:pr-created"}
        self.comments: dict[str, str] = {}
        self.statuses: dict[tuple[str, str], str] = {}

    def set_issue_labels(self, number: int, *, add: set[str], remove: set[str]):
        before = set(self.issue_labels)
        self.issue_labels.difference_update(remove)
        self.issue_labels.update(add)
        if before == self.issue_labels:
            return []
        return [MutationRecord(resource="label", target=f"issue:{number}", action="set")]

    def upsert_pr_comment(self, number: int, marker: str, body: str) -> MutationRecord:
        previous = self.comments.get(marker)
        self.comments[marker] = body
        action = "create" if previous is None else ("noop" if previous == body else "update")
        return MutationRecord(resource="comment", target=f"pr:{number}:{marker}", action=action)

    def get_status(self, sha: str, context: str) -> str | None:
        return self.statuses.get((sha, context))

    def set_status(self, sha, context, state, description, target_url):
        self.statuses[(sha, context)] = state
        return MutationRecord(resource="status", target=f"{sha}:{context}", action="set")


def prior_result(tmp_path: Path, *, status: WorkflowStatus) -> Path:
    path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=status,
            outputs={"expected_head": SHA},
        ).model_dump_json(),
        encoding="utf-8",
    )
    return path


def request(tmp_path: Path, **updates) -> FinalizeRequest:
    values = {
        "repository": tmp_path,
        "upstream_result": "success",
        "result_path": prior_result(tmp_path, status=WorkflowStatus.SUCCESS),
        "issue_number": 42,
        "pr_number": 7,
        "expected_head": SHA,
    }
    values.update(updates)
    return FinalizeRequest(**values)


def test_issue_success_removes_processing_without_failed(tmp_path: Path) -> None:
    github = FakeGitHub()
    result = FinalizeService(RepositoryPaths(tmp_path), github).run(request(tmp_path))

    assert github.issue_labels == {"ard:approved", "ard:pr-created"}
    assert result.status is WorkflowStatus.SUCCESS


def test_successful_cross_job_finalizer_does_not_require_result_file(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    finalizer = FinalizeService(RepositoryPaths(tmp_path), github)

    result = finalizer.run(
        request(
            tmp_path,
            result_path=None,
            pr_number=None,
            expected_head=None,
        )
    )

    assert result.outputs["finalized_success"] is True
    assert github.issue_labels == {"ard:approved", "ard:pr-created"}


def test_trusted_finalizer_publishes_success_statuses_idempotently(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    github.statuses[(SHA, "ard/quality-gate")] = "failure"
    finalizer = FinalizeService(RepositoryPaths(tmp_path), github)
    successful = request(
        tmp_path,
        result_path=None,
        issue_number=None,
        pr_number=None,
        publish_success_statuses=True,
    )

    first = finalizer.run(successful)
    second = finalizer.run(successful)

    assert github.statuses == {
        (SHA, "ard/changeset"): "success",
        (SHA, "ard/quality-gate"): "success",
    }
    assert first.status is WorkflowStatus.SUCCESS
    assert second.status is WorkflowStatus.NOOP


def test_authoritative_failure_replaces_stale_success_statuses(
    tmp_path: Path,
) -> None:
    github = FakeGitHub()
    github.statuses = {
        (SHA, "ard/changeset"): "success",
        (SHA, "ard/quality-gate"): "success",
    }
    finalizer = FinalizeService(RepositoryPaths(tmp_path), github)
    failed = request(
        tmp_path,
        upstream_result="failure",
        result_path=None,
        issue_number=None,
        pr_number=None,
        authoritative_statuses=True,
    )

    first = finalizer.run(failed)
    second = finalizer.run(failed)

    assert set(github.statuses.values()) == {"failure"}
    assert first.status is WorkflowStatus.SUCCESS
    assert second.status is WorkflowStatus.NOOP


def test_partial_processing_posts_failure_status_and_comment_once(tmp_path: Path) -> None:
    github = FakeGitHub()
    finalizer = FinalizeService(RepositoryPaths(tmp_path), github)
    failed = request(
        tmp_path,
        upstream_result="failure",
        result_path=prior_result(tmp_path, status=WorkflowStatus.FAILURE),
    )

    first = finalizer.run(failed)
    second = finalizer.run(failed)

    assert first.outputs == second.outputs
    assert len(github.comments) == 1
    assert set(github.statuses.values()) == {"failure"}
    assert github.issue_labels == {"ard:approved", "ard:pr-created", "ard:failed"}
    assert second.status is WorkflowStatus.NOOP


def test_partial_result_finalizes_current_head_not_original_input(tmp_path: Path) -> None:
    github = FakeGitHub()
    result_path = prior_result(tmp_path, status=WorkflowStatus.FAILURE)
    result_path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.FAILURE,
            outputs={"expected_head": SHA, "current_head": CURRENT_SHA},
        ).model_dump_json(),
        encoding="utf-8",
    )

    FinalizeService(RepositoryPaths(tmp_path), github).run(
        FinalizeRequest(
            repository=tmp_path,
            upstream_result="failure",
            result_path=result_path,
            issue_number=42,
            pr_number=7,
            expected_head=CURRENT_SHA,
        )
    )

    assert set(github.statuses) == {
        (CURRENT_SHA, "ard/changeset"),
        (CURRENT_SHA, "ard/quality-gate"),
    }


def test_finalizer_rejects_result_outside_runtime_directory(tmp_path: Path) -> None:
    outside = tmp_path / "untrusted-result.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkflowSecurityError, match="FINALIZE_RESULT_PATH_NOT_TRUSTED"):
        FinalizeService(RepositoryPaths(tmp_path), FakeGitHub()).run(
            request(tmp_path, result_path=outside)
        )
