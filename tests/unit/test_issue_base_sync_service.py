from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.base_sync import IssueBaseSyncService, IssueRouteService
from ard_ossie.application.contracts import WorkflowContext, WorkflowSecurityError
from ard_ossie.github_event import DownloadedAttachment, IntakeManifest
from ard_ossie.models import (
    ProductRecord,
    ProductTableRef,
    TableLocator,
    TableRecord,
)
from ard_ossie.ports.git import ChangedPaths, CommitResult
from ard_ossie.ports.github import PullRequestState

BASE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
BRANCH = "ard/issue-3-500138301"
RESET_SHA = "c" * 40
MERGE_SHA = "d" * 40
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
LINK_ID = "lnk_0198f6ca-2a11-78d1-8672-67d49e69f14d"


def issue_body() -> str:
    return """### Operation
create

### Product key
500138301

### Existing product ID
_No response_

### Requested version
1

### Display name
Marketing Insight

### Product HTML
[product.html](https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111)

### Semantic document
[semantic.pdf](https://github.com/user-attachments/assets/22222222-2222-2222-2222-222222222222)

### Data dictionary
[dictionary.xlsx](https://github.com/user-attachments/assets/33333333-3333-3333-3333-333333333333)

### Change reason
Reprocess with the current trusted processor
"""


def context(tmp_path: Path) -> WorkflowContext:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "labeled",
                "issue": {"number": 3, "body": issue_body()},
                "label": {"name": "ard:approved"},
                "repository": {
                    "default_branch": "main",
                    "full_name": "kimohy/ard-ossie-provider",
                },
                "sender": {"login": "kimohy"},
            }
        ),
        encoding="utf-8",
    )
    return WorkflowContext(
        repository=tmp_path,
        event_path=event,
        event_name="issues",
        repository_name="kimohy/ard-ossie-provider",
        actor="kimohy",
        runner_temp=tmp_path,
    )


class RouteGit:
    def __init__(self, *, current: str = BASE_SHA, remote: str | None = BASE_SHA) -> None:
        self.current = current
        self.remote = remote
        self.calls: list[str | tuple[str, str]] = []

    def current_sha(self) -> str:
        self.calls.append("current_sha")
        return self.current

    def remote_branch_sha(self, branch: str) -> str | None:
        self.calls.append(("remote_branch_sha", branch))
        return self.remote


class RouteGitHub:
    def __init__(self, pull_request: PullRequestState | None) -> None:
        self.pull_request = pull_request
        self.branches: list[str] = []
        self.mutations: list[object] = []

    def find_open_pr(self, branch: str) -> PullRequestState | None:
        self.branches.append(branch)
        return self.pull_request


class SequencedGitHub(RouteGitHub):
    def __init__(self, *pull_requests: PullRequestState | None) -> None:
        super().__init__(pull_requests[-1])
        self.pull_requests = list(pull_requests)

    def find_open_pr(self, branch: str) -> PullRequestState | None:
        self.branches.append(branch)
        if self.pull_requests:
            return self.pull_requests.pop(0)
        return self.pull_request


def managed_pr(**changes: object) -> PullRequestState:
    values: dict[str, object] = {
        "number": 5,
        "head_branch": BRANCH,
        "head_sha": CANDIDATE_SHA,
        "base_branch": "main",
        "draft": True,
        "merged_at": None,
        # GitHub populates merge_commit_sha with a synthetic test merge for open PRs.
        "merge_sha": "e" * 40,
        "url": "https://github.com/kimohy/ard-ossie-provider/pull/5",
    }
    values.update(changes)
    return PullRequestState(**values)  # type: ignore[arg-type]


def test_route_selects_unchanged_intake_when_managed_pr_is_absent(
    tmp_path: Path,
) -> None:
    git = RouteGit()
    github = RouteGitHub(None)

    result = IssueRouteService(git, github).run(context(tmp_path))

    assert result.outputs == {
        "mode": "intake",
        "base_sha": BASE_SHA,
        "branch": BRANCH,
        "product_key": "500138301",
    }
    assert git.calls == ["current_sha", ("remote_branch_sha", "main")]
    assert github.branches == [BRANCH]
    assert github.mutations == []


def test_route_selects_exact_existing_managed_draft(tmp_path: Path) -> None:
    result = IssueRouteService(RouteGit(), RouteGitHub(managed_pr())).run(
        context(tmp_path)
    )

    assert result.outputs == {
        "mode": "base_sync",
        "base_sha": BASE_SHA,
        "branch": BRANCH,
        "product_key": "500138301",
        "pr_number": 5,
        "expected_head": CANDIDATE_SHA,
    }


def test_route_rejects_a_default_branch_that_moved_after_checkout(
    tmp_path: Path,
) -> None:
    with pytest.raises(WorkflowSecurityError, match="ISSUE_ROUTE_BASE_MOVED"):
        IssueRouteService(
            RouteGit(remote="c" * 40),
            RouteGitHub(managed_pr()),
        ).run(context(tmp_path))


@pytest.mark.parametrize(
    "pull_request",
    [
        managed_pr(head_branch="ard/issue-99-other"),
        managed_pr(base_branch="release"),
        managed_pr(draft=False),
        managed_pr(merged_at="2026-08-12T00:00:00Z", merge_sha="d" * 40),
    ],
)
def test_route_rejects_a_pr_outside_the_managed_draft_contract(
    tmp_path: Path,
    pull_request: PullRequestState,
) -> None:
    with pytest.raises(
        WorkflowSecurityError,
        match="ISSUE_BASE_SYNC_PULL_REQUEST_MISMATCH",
    ):
        IssueRouteService(RouteGit(), RouteGitHub(pull_request)).run(context(tmp_path))


def registry_output(*, product_id: str = PRODUCT_ID) -> tuple[
    ProductRecord,
    TableRecord,
    ProductTableRef,
]:
    product = ProductRecord(
        product_id=product_id,
        product_key="500138301",
        version=1,
        display_name="Marketing Insight",
    )
    table = TableRecord(
        table_id=TABLE_ID,
        locator=TableLocator(
            source_system_id="marketing",
            catalog="warehouse",
            schema_name="campaign",
            table_name="marketing_campaign",
        ),
        version=1,
    )
    mapping = ProductTableRef(
        link_id=LINK_ID,
        product_id=product_id,
        table_id=TABLE_ID,
        table_version=1,
        usage="SOURCE",
    )
    return product, table, mapping


def populate_candidate(root: Path) -> IntakeManifest:
    product = root / "products" / "500138301"
    source_specs = {
        "product_html": (
            "product.html",
            "sources/product-info/product.html",
            "https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111",
        ),
        "semantic_document": (
            "semantic.pdf",
            "sources/semantic/semantic.pdf",
            "https://github.com/user-attachments/assets/22222222-2222-2222-2222-222222222222",
        ),
        "dictionary_excel": (
            "dictionary.xlsx",
            "sources/dictionary/dictionary.xlsx",
            "https://github.com/user-attachments/assets/33333333-3333-3333-3333-333333333333",
        ),
    }
    files: list[DownloadedAttachment] = []
    for role, (filename, relative, url) in source_specs.items():
        content = f"approved {role}\n".encode()
        target = product / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        files.append(
            DownloadedAttachment(
                role=role,
                filename=filename,
                relative_path=relative,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                source_url=url,
            )
        )
    manifest = IntakeManifest(
        issue_number=3,
        product_key="500138301",
        product_id=PRODUCT_ID,
        version=1,
        files=files,
    )
    product.mkdir(parents=True, exist_ok=True)
    (product / "product.yaml").write_text(
        "operation: create\n"
        f"product_id: {PRODUCT_ID}\n"
        "product_key: '500138301'\n"
        "version: 1\n"
        "display_name: Marketing Insight\n"
        "description: null\n"
        "changeset_id: null\n",
        encoding="utf-8",
    )
    (product / "intake-manifest.json").write_text(
        manifest.model_dump_json(),
        encoding="utf-8",
    )
    generated = product / "generated"
    generated.mkdir()
    (generated / "ossie-model.json").write_text(
        json.dumps({"dialects": ["ANSI_SQL"], "semantic_model": []}),
        encoding="utf-8",
    )
    quality = product / "quality"
    quality.mkdir()
    (quality / "quality-report.json").write_text("{}\n", encoding="utf-8")
    registry_product, registry_table, registry_mapping = registry_output()
    registry_payloads = {
        root / "registry" / "indexes" / "product-keys.json": {},
        root / "registry" / "indexes" / "table-locators.json": {},
        root / "registry" / "products" / f"{PRODUCT_ID}.json": (
            registry_product.model_dump(mode="json")
        ),
        root / "registry" / "mappings" / f"{PRODUCT_ID}.json": [
            registry_mapping.model_dump(mode="json")
        ],
        root / "registry" / "tables" / f"{TABLE_ID}.json": (
            registry_table.model_dump(mode="json")
        ),
    }
    for path, payload in registry_payloads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def canonical_prepare(_: Path, workspace: Path) -> IntakeManifest:
    return populate_candidate(workspace)


class BaseSyncGit:
    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = paths
        self.current = CANDIDATE_SHA
        self.remotes = {"main": BASE_SHA, BRANCH: CANDIDATE_SHA}
        self.clean = True
        self.clean_results: list[bool] = []
        self.remote_results: dict[str, list[str | None]] = {}
        self.ancestor = True
        self.push_updates_remote = True
        self.operations: list[object] = []

    def is_worktree_clean(self) -> bool:
        self.operations.append("is_worktree_clean")
        if self.clean_results:
            return self.clean_results.pop(0)
        return self.clean

    def current_sha(self) -> str:
        self.operations.append("current_sha")
        return self.current

    def remote_branch_sha(self, branch: str) -> str | None:
        self.operations.append(("remote_branch_sha", branch))
        sequence = self.remote_results.get(branch)
        if sequence:
            return sequence.pop(0)
        return self.remotes.get(branch)

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        self.operations.append(("changed_paths", base_ref, head_ref))
        return ChangedPaths(merge_base="e" * 40, paths=self.paths)

    def merge_revision(self, revision: str, message: str) -> CommitResult:
        self.operations.append(("merge_revision", revision, message))
        self.current = MERGE_SHA
        return CommitResult(sha=MERGE_SHA, created=True)

    def restore_paths(self, revision: str, paths: tuple[Path, ...]) -> None:
        self.operations.append(("restore_paths", revision, paths))

    def commit_allowed_paths(self, product_key: str, message: str) -> CommitResult:
        self.operations.append(("commit_allowed_paths", product_key, message))
        self.current = RESET_SHA
        return CommitResult(sha=RESET_SHA, created=True)

    def commit_intake_paths(self, product_key: str, message: str) -> CommitResult:
        self.operations.append(("commit_intake_paths", product_key, message))
        return CommitResult(sha=self.current, created=False)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        self.operations.append(("is_ancestor", ancestor, descendant))
        return self.ancestor and ancestor == BASE_SHA and descendant == RESET_SHA

    def push(self, branch: str, *, lfs: bool = False) -> None:
        self.operations.append(("push", branch, lfs))
        if self.push_updates_remote:
            self.remotes[branch] = self.current


def candidate_paths() -> tuple[Path, ...]:
    return (
        Path("products/500138301/product.yaml"),
        Path("products/500138301/intake-manifest.json"),
        Path("products/500138301/sources/product-info/product.html"),
        Path("products/500138301/sources/semantic/semantic.pdf"),
        Path("products/500138301/sources/dictionary/dictionary.xlsx"),
        Path("products/500138301/generated/ossie-model.json"),
        Path("products/500138301/quality/quality-report.json"),
        Path("registry/indexes/product-keys.json"),
        Path("registry/indexes/table-locators.json"),
        Path(f"registry/products/{PRODUCT_ID}.json"),
        Path(f"registry/mappings/{PRODUCT_ID}.json"),
        Path(f"registry/tables/{TABLE_ID}.json"),
    )


def test_base_sync_preserves_approved_input_and_resets_only_derived_paths(
    tmp_path: Path,
) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())

    result = IssueBaseSyncService(
        RepositoryPaths(tmp_path),
        git,
        RouteGitHub(managed_pr()),
        prepare=canonical_prepare,
    ).run(context(tmp_path), base_sha=BASE_SHA)

    expected_reset = tuple(sorted(candidate_paths()[5:], key=lambda path: path.as_posix()))
    expected_intake = tuple(sorted(candidate_paths()[:5], key=lambda path: path.as_posix()))
    assert ("restore_paths", CANDIDATE_SHA, expected_intake) in git.operations
    assert (
        "commit_intake_paths",
        "500138301",
        "data(500138301): preserve approved intake after base sync",
    ) in git.operations
    assert ("restore_paths", BASE_SHA, expected_reset) in git.operations
    assert all("/sources/" not in path.as_posix() for path in expected_reset)
    assert result.outputs == {
        "branch": BRANCH,
        "product_key": "500138301",
        "pr_number": 5,
        "expected_head": RESET_SHA,
        "product_id": PRODUCT_ID,
    }
    assert git.operations[-4:] == [
        ("remote_branch_sha", BRANCH),
        ("remote_branch_sha", "main"),
        ("push", BRANCH, False),
        ("remote_branch_sha", BRANCH),
    ]


@pytest.mark.parametrize(
    "unexpected",
    [
        Path("src/ard_ossie/cli/root.py"),
        Path(".github/workflows/ard-process.yml"),
        Path("products/other/generated/ossie-model.json"),
        Path("registry/products/prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632.json"),
        Path("registry/tables/tbl_0198f6ca-2a11-78d1-8672-67d49e69f14d.json"),
    ],
)
def test_base_sync_rejects_unrelated_changes_before_mutation(
    tmp_path: Path,
    unexpected: Path,
) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit((*candidate_paths(), unexpected))

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_PATH_NOT_ALLOWED"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] in {"merge_revision", "restore_paths", "push"}
        for item in git.operations
    )


def test_base_sync_rejects_a_moved_base_before_mutation(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    git.remotes["main"] = "f" * 40

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_BASE_MOVED"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "merge_revision"
        for item in git.operations
    )


@pytest.mark.parametrize(
    ("branch", "code"),
    [
        ("main", "ISSUE_BASE_SYNC_BASE_MOVED"),
        (BRANCH, "ISSUE_BASE_SYNC_HEAD_MISMATCH"),
    ],
)
def test_base_sync_rechecks_remote_heads_after_canonical_validation(
    tmp_path: Path,
    branch: str,
    code: str,
) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    initial = BASE_SHA if branch == "main" else CANDIDATE_SHA
    git.remote_results[branch] = [initial, "f" * 40]

    with pytest.raises(WorkflowSecurityError, match=code):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "merge_revision"
        for item in git.operations
    )


def test_base_sync_rechecks_managed_pr_state_after_canonical_validation(
    tmp_path: Path,
) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    github = SequencedGitHub(managed_pr(), managed_pr(draft=False))

    with pytest.raises(
        WorkflowSecurityError,
        match="ISSUE_BASE_SYNC_PULL_REQUEST_MISMATCH",
    ):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            github,
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "merge_revision"
        for item in git.operations
    )


def test_base_sync_requires_the_live_managed_pr(tmp_path: Path) -> None:
    populate_candidate(tmp_path)

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_PR_REQUIRED"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            BaseSyncGit(candidate_paths()),
            RouteGitHub(None),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)


def test_base_sync_rejects_registry_ownership_for_another_product(
    tmp_path: Path,
) -> None:
    populate_candidate(tmp_path)
    product = tmp_path / "registry" / "products" / f"{PRODUCT_ID}.json"
    unrelated, _, _ = registry_output(
        product_id="prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
    )
    product.write_text(
        unrelated.model_dump_json(),
        encoding="utf-8",
    )
    git = BaseSyncGit(candidate_paths())

    with pytest.raises(
        WorkflowSecurityError,
        match="ISSUE_BASE_SYNC_OUTPUT_REGISTRY_MISMATCH",
    ):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "merge_revision"
        for item in git.operations
    )


def test_base_sync_rejects_a_cross_product_registry_mapping(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    mapping_path = tmp_path / "registry" / "mappings" / f"{PRODUCT_ID}.json"
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    payload[0]["product_id"] = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
    mapping_path.write_text(json.dumps(payload), encoding="utf-8")
    git = BaseSyncGit(candidate_paths())

    with pytest.raises(
        WorkflowSecurityError,
        match="ISSUE_BASE_SYNC_OUTPUT_REGISTRY_MISMATCH",
    ):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "merge_revision"
        for item in git.operations
    )


def test_base_sync_rejects_a_registry_table_version_mismatch(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    mapping_path = tmp_path / "registry" / "mappings" / f"{PRODUCT_ID}.json"
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    payload[0]["table_version"] = 2
    mapping_path.write_text(json.dumps(payload), encoding="utf-8")
    git = BaseSyncGit(candidate_paths())

    with pytest.raises(
        WorkflowSecurityError,
        match="ISSUE_BASE_SYNC_OUTPUT_REGISTRY_MISMATCH",
    ):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "merge_revision"
        for item in git.operations
    )


def test_base_sync_rejects_a_dirty_candidate_before_mutation(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    git.clean = False

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_WORKTREE_DIRTY"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "merge_revision"
        for item in git.operations
    )


@pytest.mark.parametrize(
    "local,remote",
    [("f" * 40, CANDIDATE_SHA), (CANDIDATE_SHA, "f" * 40)],
)
def test_base_sync_rejects_a_candidate_head_mismatch(
    tmp_path: Path,
    local: str,
    remote: str,
) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    git.current = local
    git.remotes[BRANCH] = remote

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_HEAD_MISMATCH"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)


def test_base_sync_classifies_missing_registry_ownership(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    (tmp_path / "registry" / "mappings" / f"{PRODUCT_ID}.json").unlink()

    with pytest.raises(
        WorkflowSecurityError,
        match="ISSUE_BASE_SYNC_OUTPUT_REGISTRY_INVALID",
    ):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            BaseSyncGit(candidate_paths()),
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)


def test_base_sync_rejects_failed_final_ancestry(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    git.ancestor = False

    with pytest.raises(
        WorkflowSecurityError,
        match="ISSUE_BASE_SYNC_ANCESTRY_MISMATCH",
    ):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "push" for item in git.operations
    )


def test_base_sync_rejects_a_dirty_post_sync_worktree(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    git.clean_results = [True, False]

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_WORKTREE_DIRTY"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)


def test_base_sync_rejects_a_remote_head_move_before_push(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    git.remote_results[BRANCH] = [CANDIDATE_SHA, CANDIDATE_SHA, "f" * 40]

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_HEAD_MISMATCH"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "push" for item in git.operations
    )


def test_base_sync_rejects_a_base_move_before_push(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    git.remote_results["main"] = [BASE_SHA, BASE_SHA, "f" * 40]

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_BASE_MOVED"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "push" for item in git.operations
    )


def test_base_sync_rechecks_managed_pr_state_before_push(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    github = SequencedGitHub(
        managed_pr(),
        managed_pr(),
        managed_pr(draft=False),
    )

    with pytest.raises(
        WorkflowSecurityError,
        match="ISSUE_BASE_SYNC_PULL_REQUEST_MISMATCH",
    ):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            github,
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)

    assert not any(
        isinstance(item, tuple) and item[0] == "push" for item in git.operations
    )


def test_base_sync_verifies_the_published_remote_head(tmp_path: Path) -> None:
    populate_candidate(tmp_path)
    git = BaseSyncGit(candidate_paths())
    git.push_updates_remote = False

    with pytest.raises(WorkflowSecurityError, match="ISSUE_BASE_SYNC_HEAD_MISMATCH"):
        IssueBaseSyncService(
            RepositoryPaths(tmp_path),
            git,
            RouteGitHub(managed_pr()),
            prepare=canonical_prepare,
        ).run(context(tmp_path), base_sha=BASE_SHA)
