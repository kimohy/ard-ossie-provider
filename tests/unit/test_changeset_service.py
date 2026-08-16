from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.changesets import ChangesetRequest, ChangesetService
from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowSecurityError,
    WorkflowStatus,
)
from ard_ossie.models import ProductRecord, TableLocator, TableRecord
from ard_ossie.ports.git import ChangedPaths, CommitResult
from ard_ossie.ports.github import PullRequestState
from ard_ossie.registry import Registry

CHANGESET_ID = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
PRODUCT_A = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
PRODUCT_B = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"


class FakeGit:
    def __init__(self) -> None:
        self.branch = "main"
        self.sha_counter = 0
        self.sha = "a" * 40
        self.remote: dict[str, str] = {"main": "a" * 40}
        self.tracking_version = 1
        self.changed_override: tuple[Path, ...] | None = None
        self.ancestor_override = False
        self.ancestor_args: tuple[str, str] | None = None

    def switch_or_create(self, branch: str, base_ref: str) -> None:
        self.branch = branch
        self.sha = self.remote.get(branch, "a" * 40)

    def commit_changeset_paths(
        self,
        changeset_id: str,
        product_key: str | None,
        message: str,
    ) -> CommitResult:
        self.sha_counter += 1
        self.sha = f"{self.sha_counter:040x}"
        return CommitResult(sha=self.sha, created=True)

    def push(self, branch: str, *, lfs: bool = False) -> None:
        self.remote[branch] = self.sha

    def current_sha(self) -> str:
        return self.sha

    def remote_branch_sha(self, branch: str) -> str | None:
        return self.remote.get(branch)

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        if self.changed_override is not None:
            return ChangedPaths(merge_base="a" * 40, paths=self.changed_override)
        if self.branch == f"ard/changeset-{CHANGESET_ID}":
            paths = (Path(f"registry/changesets/{CHANGESET_ID}.json"),)
        else:
            product_key = self.branch.rsplit("-", 2)[-2:]
            key = "-".join(product_key)
            paths = (Path(f"products/{key}/changesets/{CHANGESET_ID}.json"),)
        return ChangedPaths(merge_base="a" * 40, paths=paths)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        self.ancestor_args = (ancestor, descendant)
        assert len(ancestor) == 40
        assert descendant == self.remote["main"]
        return self.ancestor_override

    def read_text_at(self, revision: str, path: str | Path) -> str:
        relative = Path(path)
        if relative.parts[:2] == ("registry", "products"):
            return ProductRecord(
                product_id=PRODUCT_A,
                product_key="sales-order",
                version=self.tracking_version,
            ).model_dump_json()
        if relative.name == "product.yaml":
            return (
                f"product_id: {PRODUCT_A}\n"
                "product_key: sales-order\n"
                f"version: {self.tracking_version}\n"
                f"changeset_id: {CHANGESET_ID}\n"
            )
        if relative.name == f"{CHANGESET_ID}.json":
            return json.dumps(
                {
                    "changeset_id": CHANGESET_ID,
                    "product_id": PRODUCT_A,
                    "status": "required",
                }
            )
        raise AssertionError(relative)


class FakeGitHub:
    def __init__(self, git: FakeGit) -> None:
        self.git = git
        self.prs: dict[str, PullRequestState] = {}
        self.created_pr_count = 0
        self.comments: dict[str, str] = {}

    def find_open_pr(self, branch: str) -> PullRequestState | None:
        existing = self.prs.get(branch)
        if existing is None:
            return None
        return replace(
            existing,
            head_sha=self.git.remote.get(branch, existing.head_sha),
        )

    def create_draft_pr(self, branch: str, base: str, title: str, body: str) -> PullRequestState:
        self.created_pr_count += 1
        pr = PullRequestState(
            number=self.created_pr_count,
            head_branch=branch,
            head_sha=self.git.remote[branch],
            base_branch=base,
            draft=True,
            merged_at=None,
            merge_sha=None,
            url=f"https://example.invalid/pull/{self.created_pr_count}",
        )
        self.prs[branch] = pr
        return pr

    def get_pr(self, number: int) -> PullRequestState:
        pr = next(pr for pr in self.prs.values() if pr.number == number)
        return replace(
            pr,
            head_sha=self.git.remote.get(pr.head_branch, pr.head_sha),
        )

    def set_status(self, sha, context, state, description, target_url):
        return MutationRecord(resource="status", target=f"{sha}:{context}", action="set")

    def upsert_pr_comment(self, number: int, marker: str, body: str) -> MutationRecord:
        action = "noop" if self.comments.get(marker) == body else "update"
        if marker not in self.comments:
            action = "create"
        self.comments[marker] = body
        return MutationRecord(resource="comment", target=f"pr:{number}:{marker}", action=action)


def seed_registry(root: Path) -> None:
    registry = Registry.load(root / "registry")
    registry.write_product(
        ProductRecord(product_id=PRODUCT_A, product_key="sales-order", version=1)
    )
    registry.write_product(
        ProductRecord(product_id=PRODUCT_B, product_key="finance-order", version=1)
    )
    registry.write_table(
        TableRecord(
            table_id=TABLE_ID,
            locator=TableLocator(
                source_system_id="erp",
                catalog="analytics",
                schema_name="sales",
                table_name="orders",
            ),
            version=1,
        )
    )


def create_request(root: Path) -> ChangesetRequest:
    return ChangesetRequest(
        repository=root,
        mode="create",
        changeset_id=CHANGESET_ID,
        table_ids=[TABLE_ID],
        product_ids=[PRODUCT_A, PRODUCT_B],
        initiating_pr=99,
        base_branch="main",
    )


def test_changeset_create_builds_coordination_and_tracking_prs(tmp_path: Path) -> None:
    seed_registry(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)

    result = ChangesetService(RepositoryPaths(tmp_path), git, github).run(
        create_request(tmp_path)
    )

    assert result.outputs["required_count"] == 2
    assert len(
        [mutation for mutation in result.mutations if mutation.resource == "pull_request"]
    ) == 3
    assert github.created_pr_count == 3


def test_changeset_rejects_preclaimed_coordination_branch_with_code(
    tmp_path: Path,
) -> None:
    seed_registry(tmp_path)
    git = FakeGit()
    branch = f"ard/changeset-{CHANGESET_ID}"
    git.remote[branch] = "c" * 40
    git.changed_override = (
        Path(f"registry/changesets/{CHANGESET_ID}.json"),
        Path("src/ard_ossie/cli/root.py"),
    )

    with pytest.raises(WorkflowSecurityError, match="CHANGESET_COORDINATION_PATH_MISMATCH"):
        ChangesetService(RepositoryPaths(tmp_path), git, FakeGitHub(git)).run(
            create_request(tmp_path)
        )


def test_changeset_rejects_empty_divergent_coordination_branch(
    tmp_path: Path,
) -> None:
    seed_registry(tmp_path)
    git = FakeGit()
    branch = f"ard/changeset-{CHANGESET_ID}"
    git.remote[branch] = "c" * 40
    git.changed_override = ()

    with pytest.raises(WorkflowSecurityError, match="CHANGESET_COORDINATION_PATH_MISMATCH"):
        ChangesetService(RepositoryPaths(tmp_path), git, FakeGitHub(git)).run(
            create_request(tmp_path)
        )


def test_changeset_ready_reuses_merged_coordination_branch_with_empty_diff(
    tmp_path: Path,
) -> None:
    seed_registry(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    service = ChangesetService(RepositoryPaths(tmp_path), git, github)
    service.run(create_request(tmp_path))
    coordination_branch = f"ard/changeset-{CHANGESET_ID}"
    retained_coordination_head = git.remote[coordination_branch]
    tracking = github.prs[f"ard/{CHANGESET_ID}-sales-order"]
    github.prs.pop(coordination_branch)
    git.changed_override = ()
    git.ancestor_override = True

    result = service.run(
        ChangesetRequest(
            repository=tmp_path,
            mode="ready",
            changeset_id=CHANGESET_ID,
            product_id=PRODUCT_A,
            version=1,
            pr_number=tracking.number,
            head_sha=tracking.head_sha,
            base_branch="main",
        )
    )

    assert result.status is WorkflowStatus.SUCCESS
    assert result.outputs["ready_count"] == 1
    assert github.created_pr_count == 4
    assert github.prs[coordination_branch].number == 4
    assert git.ancestor_args == (retained_coordination_head, git.remote["main"])


def test_changeset_ready_is_idempotent_for_same_head(tmp_path: Path) -> None:
    seed_registry(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    service = ChangesetService(RepositoryPaths(tmp_path), git, github)
    service.run(create_request(tmp_path))
    tracking = github.prs[f"ard/{CHANGESET_ID}-sales-order"]
    ready = ChangesetRequest(
        repository=tmp_path,
        mode="ready",
        changeset_id=CHANGESET_ID,
        product_id=PRODUCT_A,
        version=1,
        pr_number=tracking.number,
        head_sha=tracking.head_sha,
        base_branch="main",
    )

    first = service.run(ready)
    second = service.run(ready)

    assert first.outputs == second.outputs
    assert second.status is WorkflowStatus.NOOP


def test_changeset_ready_accepts_version_from_exact_tracking_head(
    tmp_path: Path,
) -> None:
    seed_registry(tmp_path)
    git = FakeGit()
    github = FakeGitHub(git)
    service = ChangesetService(RepositoryPaths(tmp_path), git, github)
    service.run(create_request(tmp_path))
    tracking = github.prs[f"ard/{CHANGESET_ID}-sales-order"]
    git.tracking_version = 2

    result = service.run(
        ChangesetRequest(
            repository=tmp_path,
            mode="ready",
            changeset_id=CHANGESET_ID,
            product_id=PRODUCT_A,
            version=2,
            pr_number=tracking.number,
            head_sha=tracking.head_sha,
            base_branch="main",
        )
    )

    assert result.outputs["ready_count"] == 1
