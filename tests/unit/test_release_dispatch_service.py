from __future__ import annotations

from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
)
from ard_ossie.application.release_dispatch import (
    ReleaseDispatchRequest,
    ReleaseDispatchService,
)
from ard_ossie.ports.github import GitHubTransientError

COMMIT = "a" * 40
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
TAG = f"product/{PRODUCT_ID}/v12"


class FakeGit:
    def __init__(self) -> None:
        self.sha = COMMIT

    def current_sha(self) -> str:
        return self.sha


class FakeGitHub:
    def __init__(self) -> None:
        self.last_dispatch: dict[str, object] | None = None
        self.status: str | None = None
        self.fail_status = False

    def get_status(self, sha: str, context: str) -> str | None:
        assert sha == COMMIT
        assert context == f"ard/dispatched:{PRODUCT_ID}:v12"
        return self.status

    def repository_dispatch(self, event_type: str, payload: dict[str, object]):
        self.last_dispatch = {
            "event_type": event_type,
            "client_payload": payload,
        }
        return MutationRecord(
            resource="repository_dispatch",
            target=event_type,
            action="dispatch",
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
        self.status = state
        return MutationRecord(resource="status", target=f"{sha}:{context}", action="set")


def release_result(tmp_path: Path, **updates: object) -> Path:
    outputs = {
        "product_id": PRODUCT_ID,
        "product_key": "sales-order",
        "version": 12,
        "product_tag": TAG,
        "table_tags": [],
        "commit": COMMIT,
        "artifact_sha256": "b" * 64,
        "artifact_hashes": {
            "generated/ossie-model.json": "c" * 64,
            "quality/quality-report.json": "d" * 64,
        },
        "changeset_id": "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2",
        **updates,
    }
    path = tmp_path / ".ard" / "run" / "workflow.release-product-result.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        WorkflowResult(
            command="workflow.release-product",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
        ).model_dump_json(),
        encoding="utf-8",
    )
    return path


def request(tmp_path: Path, path: Path) -> ReleaseDispatchRequest:
    return ReleaseDispatchRequest(
        repository=tmp_path,
        result_path=path,
        current=COMMIT,
    )


def test_dispatch_contains_only_approved_release_fields(tmp_path: Path) -> None:
    path = release_result(tmp_path, untrusted_extra="must-not-dispatch")
    github = FakeGitHub()

    ReleaseDispatchService(
        RepositoryPaths(tmp_path),
        FakeGit(),
        github,
    ).run(request(tmp_path, path))

    assert github.last_dispatch == {
        "event_type": "ard_product_released",
        "client_payload": {
            "product_id": PRODUCT_ID,
            "version": 12,
            "tag": TAG,
            "commit": COMMIT,
            "artifact_hashes": {
                "generated/ossie-model.json": "c" * 64,
                "quality/quality-report.json": "d" * 64,
            },
        },
    }


def test_dispatch_is_noop_after_exact_success_status(tmp_path: Path) -> None:
    path = release_result(tmp_path)
    github = FakeGitHub()
    github.status = "success"

    result = ReleaseDispatchService(
        RepositoryPaths(tmp_path),
        FakeGit(),
        github,
    ).run(request(tmp_path, path))

    assert result.status is WorkflowStatus.NOOP
    assert github.last_dispatch is None


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"product_tag": f"product/{PRODUCT_ID}/v13"}, "RELEASE_TAG_MISMATCH"),
        ({"commit": "b" * 40}, "RELEASE_COMMIT_MISMATCH"),
        ({"artifact_hashes": {"../secret": "c" * 64}}, "ARTIFACT_HASHES_INVALID"),
    ],
)
def test_dispatch_rejects_untrusted_release_envelope(
    tmp_path: Path,
    updates: dict[str, object],
    code: str,
) -> None:
    path = release_result(tmp_path, **updates)

    with pytest.raises(WorkflowSecurityError, match=code):
        ReleaseDispatchService(
            RepositoryPaths(tmp_path),
            FakeGit(),
            FakeGitHub(),
        ).run(request(tmp_path, path))


def test_dispatch_status_failure_preserves_deduplication_journal(tmp_path: Path) -> None:
    path = release_result(tmp_path)
    github = FakeGitHub()
    github.fail_status = True

    with pytest.raises(WorkflowPartialError, match="RELEASE_DISPATCH_PARTIAL") as captured:
        ReleaseDispatchService(
            RepositoryPaths(tmp_path),
            FakeGit(),
            github,
        ).run(request(tmp_path, path))

    assert captured.value.outputs["deduplication_key"] == [PRODUCT_ID, 12, TAG, COMMIT]
    assert captured.value.mutations[0].resource == "repository_dispatch"
