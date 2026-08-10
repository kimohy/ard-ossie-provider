from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowContext,
    WorkflowSecurityError,
    WorkflowStatus,
)
from ard_ossie.application.intake import IssueAuthorizationService, IssueIntakeService
from ard_ossie.github_event import DownloadedAttachment, IntakeManifest
from ard_ossie.ports.git import ChangedPaths, CommitResult
from ard_ossie.ports.github import PullRequestState

SHA = "a" * 40
BASE_SHA = "b" * 40
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"


def issue_body() -> str:
    return """### Operation
create

### Product key
sales-order

### Existing product ID
_No response_

### Requested version
1

### Display name
Sales Order

### Product HTML
[product.html](https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111)

### Semantic document
[semantic.docx](https://github.com/user-attachments/assets/22222222-2222-2222-2222-222222222222)

### Data dictionary
[dictionary.xlsx](https://github.com/user-attachments/assets/33333333-3333-3333-3333-333333333333)

### Change reason
Initial publication
"""


def context(
    tmp_path: Path,
    *,
    actor: str = "kimohy",
    body: str | None = None,
) -> WorkflowContext:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "labeled",
                "issue": {"number": 42, "body": body or issue_body()},
                "label": {"name": "ard:approved"},
                "repository": {"default_branch": "main", "full_name": "owner/repository"},
                "sender": {"login": actor},
            }
        ),
        encoding="utf-8",
    )
    return WorkflowContext(
        repository=tmp_path,
        event_path=event,
        event_name="issues",
        actor=actor,
        repository_name="owner/repository",
    )


class FakeGit:
    def __init__(self) -> None:
        self.sha = SHA
        self.remote_sha = SHA
        self.paths = (
            Path("products/sales-order/product.yaml"),
            Path("products/sales-order/intake-manifest.json"),
            Path("products/sales-order/sources/product/product.html"),
            Path("products/sales-order/sources/semantic/semantic.docx"),
            Path("products/sales-order/sources/dictionary/dictionary.xlsx"),
        )
        self.switched: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, bool]] = []

    def switch_or_create(self, branch: str, base_ref: str) -> None:
        self.switched.append((branch, base_ref))

    def commit_intake_paths(self, product_key: str, message: str) -> CommitResult:
        return CommitResult(sha=self.sha, created=True)

    def push(self, branch: str, *, lfs: bool = False) -> None:
        self.pushes.append((branch, lfs))

    def remote_branch_sha(self, branch: str) -> str | None:
        return self.remote_sha

    def current_sha(self) -> str:
        return self.sha

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        return ChangedPaths(merge_base=BASE_SHA, paths=self.paths)


class FakeGitHub:
    def __init__(self) -> None:
        self.permissions = {"kimohy": "admin", "reader": "read"}
        self.pull_request: PullRequestState | None = None
        self.created_pr_count = 0
        self.labels: set[str] = {"ard:approved"}

    def collaborator_permission(self, login: str) -> str:
        return self.permissions[login]

    def find_open_pr(self, branch: str) -> PullRequestState | None:
        if self.pull_request is not None and self.pull_request.head_branch == branch:
            return self.pull_request
        return None

    def create_draft_pr(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestState:
        self.created_pr_count += 1
        self.pull_request = PullRequestState(
            number=7,
            head_branch=branch,
            head_sha=SHA,
            base_branch=base,
            draft=True,
            merged_at=None,
            merge_sha=None,
            url="https://example.invalid/pull/7",
        )
        return self.pull_request

    def set_issue_labels(
        self,
        number: int,
        *,
        add: set[str],
        remove: set[str],
    ) -> list[MutationRecord]:
        before = set(self.labels)
        self.labels.difference_update(remove)
        self.labels.update(add)
        return [
            MutationRecord(resource="label", target=f"issue:{number}", action="set")
        ] if before != self.labels else []


def test_issue_authorize_requires_approved_label_and_writer(tmp_path: Path) -> None:
    github = FakeGitHub()

    result = IssueAuthorizationService(github).run(
        context(tmp_path),
        label="ard:approved",
        actor="kimohy",
    )

    assert result.status is WorkflowStatus.SUCCESS
    assert result.outputs["allowed"] is True


def test_issue_authorize_rejects_reader(tmp_path: Path) -> None:
    github = FakeGitHub()

    with pytest.raises(WorkflowSecurityError, match="ISSUE_APPROVER_PERMISSION_DENIED"):
        IssueAuthorizationService(github).run(
            context(tmp_path, actor="reader"),
            label="ard:approved",
            actor="reader",
        )


def test_issue_authorize_rejects_actor_that_does_not_match_event(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.permissions["other-admin"] = "admin"

    with pytest.raises(WorkflowSecurityError, match="ISSUE_ACTOR_MISMATCH"):
        IssueAuthorizationService(github).run(
            context(tmp_path),
            label="ard:approved",
            actor="other-admin",
        )


def test_issue_intake_reuses_equivalent_branch_and_pr(tmp_path: Path) -> None:
    workflow_context = context(tmp_path)
    git = FakeGit()
    github = FakeGitHub()
    prepare_count = 0

    def prepare(event_path: Path, workspace: Path) -> IntakeManifest:
        nonlocal prepare_count
        prepare_count += 1
        product = workspace / "products" / "sales-order"
        sources = {
            "product_html": product / "sources" / "product" / "product.html",
            "semantic_document": product / "sources" / "semantic" / "semantic.docx",
            "dictionary_excel": product / "sources" / "dictionary" / "dictionary.xlsx",
        }
        urls = {
            "product_html": "https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111",
            "semantic_document": "https://github.com/user-attachments/assets/22222222-2222-2222-2222-222222222222",
            "dictionary_excel": "https://github.com/user-attachments/assets/33333333-3333-3333-3333-333333333333",
        }
        files = []
        for role, path in sources.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            content = role.encode()
            path.write_bytes(content)
            files.append(
                DownloadedAttachment(
                    role=role,
                    filename=path.name,
                    relative_path=path.relative_to(product).as_posix(),
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    source_url=urls[role],
                )
            )
        (product / "product.yaml").write_text(
            "operation: create\n"
            f"product_id: {PRODUCT_ID}\n"
            "product_key: sales-order\n"
            "version: 1\n"
            "display_name: Sales Order\n"
            "description: null\n"
            "changeset_id: null\n"
            "tables: []\n",
            encoding="utf-8",
        )
        manifest = IntakeManifest(
            issue_number=42,
            product_key="sales-order",
            product_id=PRODUCT_ID,
            version=1,
            files=files,
        )
        (product / "intake-manifest.json").write_text(
            manifest.model_dump_json(),
            encoding="utf-8",
        )
        return manifest

    service = IssueIntakeService(
        RepositoryPaths(tmp_path),
        git,
        github,
        prepare=prepare,
    )

    first = service.run(workflow_context)
    second = service.run(workflow_context)

    assert first.outputs["pr_number"] == second.outputs["pr_number"] == 7
    assert first.outputs["expected_head"] == second.outputs["expected_head"] == SHA
    assert second.status is WorkflowStatus.NOOP
    assert github.created_pr_count == 1
    assert prepare_count == 2
    assert git.pushes == [("ard/issue-42-sales-order", True)]
    assert git.switched == [
        ("ard/issue-42-sales-order", "main"),
        ("ard/issue-42-sales-order", "main"),
    ]
    assert github.labels == {"ard:approved", "ard:processing", "ard:pr-created"}

    tampered = b"unapproved attachment bytes"
    source = tmp_path / "products" / "sales-order" / "sources" / "product" / "product.html"
    source.write_bytes(tampered)
    manifest_path = tmp_path / "products" / "sales-order" / "intake-manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    product_file = next(
        item for item in manifest_payload["files"] if item["role"] == "product_html"
    )
    product_file["sha256"] = hashlib.sha256(tampered).hexdigest()
    product_file["size_bytes"] = len(tampered)
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    with pytest.raises(WorkflowSecurityError, match="ISSUE_EXISTING_SOURCE_MISMATCH"):
        service.run(workflow_context)


def test_issue_intake_rejects_preclaimed_branch_with_code_changes(tmp_path: Path) -> None:
    workflow_context = context(tmp_path)
    git = FakeGit()
    github = FakeGitHub()
    github.pull_request = PullRequestState(
        number=7,
        head_branch="ard/issue-42-sales-order",
        head_sha=SHA,
        base_branch="main",
        draft=True,
        merged_at=None,
        merge_sha=None,
        url="https://example.invalid/pull/7",
    )
    git.paths = (Path("src/ard_ossie/cli/root.py"),)

    with pytest.raises(WorkflowSecurityError, match="ISSUE_EXISTING_PATH_NOT_ALLOWED"):
        IssueIntakeService(RepositoryPaths(tmp_path), git, github).run(workflow_context)


def test_issue_intake_uses_canonical_changeset_tracking_branch(tmp_path: Path) -> None:
    changeset_id = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
    body = issue_body().replace(
        "### Display name",
        f"### Changeset ID\n{changeset_id}\n\n### Display name",
    )
    service = IssueIntakeService(
        RepositoryPaths(tmp_path),
        FakeGit(),
        FakeGitHub(),
        prepare=lambda event, workspace: IntakeManifest(
            issue_number=42,
            product_key="sales-order",
            product_id=PRODUCT_ID,
            version=1,
            files=[],
        ),
    )

    result = service.run(context(tmp_path, body=body))

    assert result.outputs["branch"] == f"ard/{changeset_id}-sales-order"


def test_issue_intake_populates_pristine_changeset_tracking_pr(tmp_path: Path) -> None:
    changeset_id = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
    body = issue_body().replace(
        "### Display name",
        f"### Changeset ID\n{changeset_id}\n\n### Display name",
    )
    branch = f"ard/{changeset_id}-sales-order"
    git = FakeGit()
    git.paths = (
        Path(f"products/sales-order/changesets/{changeset_id}.json"),
    )
    github = FakeGitHub()
    github.pull_request = PullRequestState(
        number=7,
        head_branch=branch,
        head_sha=SHA,
        base_branch="main",
        draft=True,
        merged_at=None,
        merge_sha=None,
        url="https://example.invalid/pull/7",
    )

    result = IssueIntakeService(
        RepositoryPaths(tmp_path),
        git,
        github,
        prepare=lambda event, workspace: IntakeManifest(
            issue_number=42,
            product_key="sales-order",
            product_id=PRODUCT_ID,
            version=1,
            files=[],
        ),
    ).run(context(tmp_path, body=body))

    assert result.outputs["pr_number"] == 7
    assert github.created_pr_count == 0
    assert git.pushes == [(branch, True)]
