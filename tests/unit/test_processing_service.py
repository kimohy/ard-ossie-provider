from __future__ import annotations

import json
from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowTransientError,
)
from ard_ossie.application.processing import (
    ProcessingReconcileRequest,
    ProcessingReconcileService,
    ProcessingRequest,
    ProcessingService,
)
from ard_ossie.pipeline import (
    ProcessResult,
    ProviderExecutionError,
    QualityReport,
    QualityStatus,
)
from ard_ossie.ports.git import ChangedPaths, CommitResult, GitTransientError
from ard_ossie.ports.github import GitHubTransientError, PullRequestState

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
CHANGESET_ID = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"


class FakeGit:
    def __init__(self) -> None:
        self.sha = OLD_SHA
        self.remote_sha = OLD_SHA
        self.pushes: list[tuple[str, bool]] = []

    def current_sha(self) -> str:
        return self.sha

    def commit_allowed_paths(self, product_key: str, message: str) -> CommitResult:
        if self.sha == NEW_SHA:
            return CommitResult(sha=NEW_SHA, created=False)
        self.sha = NEW_SHA
        return CommitResult(sha=NEW_SHA, created=True)

    def push(self, branch: str, *, lfs: bool = False) -> None:
        self.pushes.append((branch, lfs))
        self.remote_sha = self.sha

    def remote_branch_sha(self, branch: str) -> str | None:
        return self.remote_sha

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return ancestor == OLD_SHA and descendant == NEW_SHA

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        assert base_ref == OLD_SHA
        assert head_ref == NEW_SHA
        return ChangedPaths(
            merge_base=OLD_SHA,
            paths=(Path("products/sales-order/generated/ossie-model.json"),),
        )


class FailingPushGit(FakeGit):
    def push(self, branch: str, *, lfs: bool = False) -> None:
        raise GitTransientError("GIT_PUSH_FAILED", "network")


class FakeGitHub:
    def __init__(self, git: FakeGit) -> None:
        self.git = git
        self.head_branch = "ard/example"
        self.statuses: list[tuple[str, str, str]] = []
        self.dispatched = 0
        self.fail_status = False

    def get_pr(self, number: int) -> PullRequestState:
        return PullRequestState(
            number=number,
            head_branch=self.head_branch,
            head_sha=self.git.remote_sha,
            base_branch="main",
            draft=True,
            merged_at=None,
            merge_sha=None,
            url="https://example.invalid/pull/7",
        )

    def set_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ):
        if self.fail_status:
            raise GitHubTransientError("STATUS_FAILED", "network")
        self.statuses.append((sha, context, state))
        from ard_ossie.application.contracts import MutationRecord

        return MutationRecord(resource="status", target=f"{sha}:{context}", action="set")

    def get_status(self, sha: str, context: str) -> str | None:
        return next(
            (
                state
                for status_sha, status_context, state in reversed(self.statuses)
                if status_sha == sha and status_context == context
            ),
            None,
        )

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]):
        self.dispatched += 1
        from ard_ossie.application.contracts import MutationRecord

        return MutationRecord(resource="workflow", target=f"{workflow}:{ref}", action="dispatch")


class FlakyReconcileGitHub(FakeGitHub):
    def __init__(self, git: FakeGit) -> None:
        super().__init__(git)
        self.get_status_failures = 2
        self.set_status_failures = 2
        self.dispatch_failures = 2
        self.dispatch_attempts = 0

    def get_status(self, sha: str, context: str) -> str | None:
        if self.get_status_failures:
            self.get_status_failures -= 1
            raise GitHubTransientError("STATUS_READ_FAILED", "network")
        return super().get_status(sha, context)

    def set_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ):
        if self.set_status_failures:
            self.set_status_failures -= 1
            raise GitHubTransientError("STATUS_FAILED", "network")
        return super().set_status(sha, context, state, description, target_url)

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]):
        self.dispatch_attempts += 1
        if self.dispatch_failures:
            self.dispatch_failures -= 1
            raise GitHubTransientError("DISPATCH_FAILED", "network")
        return super().dispatch_workflow(workflow, ref, inputs)


def request(tmp_path: Path) -> ProcessingRequest:
    return ProcessingRequest(
        repository=tmp_path,
        product_key="sales-order",
        branch="ard/example",
        pr_number=7,
        expected_head=OLD_SHA,
        allow_writeback=True,
    )


def successful_processor(product_path: Path, **kwargs) -> ProcessResult:
    return ProcessResult(
        product_id=PRODUCT_ID,
        product_version=1,
        generated_dir=product_path / "generated",
        quality_report=QualityReport(
            status=QualityStatus.PASS,
            product_id=PRODUCT_ID,
            product_version=1,
            completeness=1,
            hard_errors=[],
            warnings=[],
            artifact_hashes={},
        ),
    )


def repository(tmp_path: Path) -> None:
    product = tmp_path / "products" / "sales-order"
    product.mkdir(parents=True)
    (product / "product.yaml").write_text(
        f"product_id: {PRODUCT_ID}\nversion: 1\nchangeset_id:\n",
        encoding="utf-8",
    )
    (tmp_path / "registry").mkdir()


def bind_changeset(tmp_path: Path) -> None:
    product = tmp_path / "products" / "sales-order"
    (product / "product.yaml").write_text(
        "\n".join(
            (
                f"product_id: {PRODUCT_ID}",
                "version: 1",
                f"changeset_id: {CHANGESET_ID}",
                "",
            )
        ),
        encoding="utf-8",
    )
    marker = product / "changesets" / f"{CHANGESET_ID}.json"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "changeset_id": CHANGESET_ID,
                "product_id": PRODUCT_ID,
                "status": "required",
            }
        ),
        encoding="utf-8",
    )


def test_processing_promotes_commits_and_sets_exact_head_status(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    service = ProcessingService(
        RepositoryPaths(tmp_path),
        git,
        github,
        processor=successful_processor,
        provider_factory=lambda: None,
    )

    result = service.run(request(tmp_path))

    assert result.status == "success"
    assert result.outputs["product_id"] == PRODUCT_ID
    assert result.outputs["current_head"] == NEW_SHA
    assert [mutation.resource for mutation in result.mutations][:3] == [
        "commit",
        "status",
        "status",
    ]
    assert all(status[0] == NEW_SHA for status in github.statuses)
    assert git.pushes == [("ard/example", True)]


def test_processing_rejects_stale_head_before_loading_provider(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    git.sha = "c" * 40
    provider_loaded = False

    def provider_factory():
        nonlocal provider_loaded
        provider_loaded = True

    with pytest.raises(WorkflowSecurityError, match="PROCESSING_HEAD_MISMATCH"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=successful_processor,
            provider_factory=provider_factory,
        ).run(request(tmp_path))

    assert provider_loaded is False


def test_processing_rejects_unbound_changeset_marker_before_provider(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    marker = tmp_path / "products" / "sales-order" / "changesets" / (
        "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2.json"
    )
    marker.parent.mkdir()
    marker.write_text("{}", encoding="utf-8")
    provider_loaded = False

    def provider_factory():
        nonlocal provider_loaded
        provider_loaded = True

    git = FakeGit()
    with pytest.raises(WorkflowSecurityError, match="CHANGESET_BINDING_REQUIRED"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=successful_processor,
            provider_factory=provider_factory,
        ).run(request(tmp_path))

    assert provider_loaded is False


def test_processing_classifies_provider_failure_as_transient(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()

    def fail_provider(*args, **kwargs):
        raise ProviderExecutionError("LLM_PROVIDER_FAILURE: timeout")

    with pytest.raises(WorkflowTransientError, match="LLM_PROVIDER_FAILURE"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=fail_provider,
            provider_factory=lambda: object(),
        ).run(request(tmp_path))


def test_processing_status_failure_after_push_is_partial(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    github.fail_status = True

    with pytest.raises(
        WorkflowPartialError,
        match="PROCESSING_POST_COMMIT_FAILED",
    ) as captured:
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            github,
            processor=successful_processor,
            provider_factory=lambda: None,
        ).run(request(tmp_path))

    assert git.remote_sha == NEW_SHA
    assert captured.value.outputs["current_head"] == NEW_SHA
    assert captured.value.outputs["expected_head"] == OLD_SHA
    assert captured.value.mutations[0].resource == "commit"


def test_processing_push_failure_after_commit_is_partial(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FailingPushGit()

    with pytest.raises(
        WorkflowPartialError,
        match="PROCESSING_POST_COMMIT_FAILED",
    ) as captured:
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=successful_processor,
            provider_factory=lambda: None,
        ).run(request(tmp_path))

    assert captured.value.outputs["current_head"] == NEW_SHA
    assert captured.value.mutations == [
        MutationRecord(resource="commit", target=NEW_SHA, action="create")
    ]


def test_processing_validates_changeset_binding_before_commit(tmp_path: Path) -> None:
    repository(tmp_path)
    bind_changeset(tmp_path)
    git = FakeGit()

    def corrupt_binding(product_path: Path, **kwargs) -> ProcessResult:
        (product_path / "product.yaml").write_text(
            f"product_id: {PRODUCT_ID}\nversion: 1\nchangeset_id:\n",
            encoding="utf-8",
        )
        return successful_processor(product_path, **kwargs)

    changeset_request = request(tmp_path).model_copy(
        update={"branch": f"ard/{CHANGESET_ID}-sales-order"}
    )
    github = FakeGitHub(git)
    github.head_branch = changeset_request.branch
    with pytest.raises(WorkflowSecurityError):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            github,
            processor=corrupt_binding,
            provider_factory=lambda: None,
        ).run(changeset_request)

    assert git.sha == OLD_SHA
    assert git.pushes == []


def test_processing_rejects_descendant_writeback_head_as_untrusted(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    git = FakeGit()
    git.sha = git.remote_sha = NEW_SHA

    with pytest.raises(WorkflowSecurityError, match="PROCESSING_HEAD_MISMATCH"):
        ProcessingService(
            RepositoryPaths(tmp_path),
            git,
            FakeGitHub(git),
            processor=successful_processor,
            provider_factory=lambda: None,
        ).run(request(tmp_path))


def test_processing_reconcile_uses_same_job_partial_envelope(tmp_path: Path) -> None:
    repository(tmp_path)
    git = FakeGit()
    git.sha = git.remote_sha = NEW_SHA
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.FAILURE,
            outputs={
                "current_head": NEW_SHA,
                "expected_head": OLD_SHA,
                "product_id": PRODUCT_ID,
                "product_key": "sales-order",
                "version": 1,
            },
            findings=[
                {
                    "code": "PROCESSING_POST_COMMIT_FAILED",
                    "message": "PROCESSING_POST_COMMIT_FAILED",
                }
            ],
            mutations=[
                MutationRecord(resource="commit", target=NEW_SHA, action="create")
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    github = FakeGitHub(git)

    result = ProcessingReconcileService(
        RepositoryPaths(tmp_path),
        git,
        github,
    ).run(
        ProcessingReconcileRequest(
            repository=tmp_path,
            result_path=result_path,
            branch="ard/example",
            pr_number=7,
        )
    )

    assert result.outputs["current_head"] == NEW_SHA
    assert github.statuses == [
        (NEW_SHA, "ard/changeset", "success"),
        (NEW_SHA, "ard/quality-gate", "success"),
    ]


def test_processing_reconcile_retries_consecutive_transient_failures(
    tmp_path: Path,
) -> None:
    repository(tmp_path)
    git = FakeGit()
    git.sha = git.remote_sha = NEW_SHA
    result_path = tmp_path / ".ard" / "run" / "workflow.process-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.FAILURE,
            outputs={
                "current_head": NEW_SHA,
                "expected_head": OLD_SHA,
                "product_id": PRODUCT_ID,
                "product_key": "sales-order",
                "version": 1,
                "changeset_id": CHANGESET_ID,
            },
            findings=[
                {
                    "code": "PROCESSING_POST_COMMIT_FAILED",
                    "message": "PROCESSING_POST_COMMIT_FAILED",
                }
            ],
            mutations=[
                MutationRecord(resource="commit", target=NEW_SHA, action="create")
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    github = FlakyReconcileGitHub(git)

    result = ProcessingReconcileService(
        RepositoryPaths(tmp_path),
        git,
        github,
        retry_attempts=3,
        retry_delay_seconds=0,
        sleeper=lambda _: None,
    ).run(
        ProcessingReconcileRequest(
            repository=tmp_path,
            result_path=result_path,
            branch="ard/example",
            pr_number=7,
        )
    )

    assert result.status is WorkflowStatus.SUCCESS
    assert github.get_status_failures == 0
    assert github.set_status_failures == 0
    assert github.dispatch_attempts == 3
    assert github.dispatched == 1
