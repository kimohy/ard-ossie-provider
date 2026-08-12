from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import TypeAdapter

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowContext,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
)
from ard_ossie.application.intake import (
    IssueRequest,
    load_issue_request,
    prepare_existing_intake,
    require_managed_pr,
)
from ard_ossie.github_event import IntakeManifest, prepare_issue_event
from ard_ossie.models import ProductRecord, ProductTableRef, TableRecord
from ard_ossie.ports.filesystem import FileSystemPort, PathPolicyError
from ard_ossie.ports.git import GitPort
from ard_ossie.ports.github import GitHubPort, PullRequestState


class IssueRouteService:
    def __init__(self, git: GitPort, github: GitHubPort) -> None:
        self.git = git
        self.github = github

    def run(self, context: WorkflowContext) -> WorkflowResult:
        request = load_issue_request(context)
        base_sha = self.git.current_sha()
        if self.git.remote_branch_sha(request.event.default_branch) != base_sha:
            raise WorkflowSecurityError(
                "ISSUE_ROUTE_BASE_MOVED",
                "trusted base moved after checkout",
            )

        outputs: dict[str, object] = {
            "mode": "intake",
            "base_sha": base_sha,
            "branch": request.branch,
            "product_key": str(request.intake.product_key),
        }
        pull_request = self.github.find_open_pr(request.branch)
        if pull_request is not None:
            require_managed_pr(
                pull_request,
                request.branch,
                request.event.default_branch,
            )
            outputs.update(
                mode="base_sync",
                pr_number=pull_request.number,
                expected_head=pull_request.head_sha,
            )

        return WorkflowResult(
            command="workflow.issue-route",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
        )


class IssueBaseSyncService:
    def __init__(
        self,
        paths: FileSystemPort,
        git: GitPort,
        github: GitHubPort,
        *,
        prepare: Callable[[Path, Path], IntakeManifest] = prepare_issue_event,
    ) -> None:
        self.paths = paths
        self.git = git
        self.github = github
        self.prepare = prepare

    def run(self, context: WorkflowContext, *, base_sha: str) -> WorkflowResult:
        request = load_issue_request(context)
        pull_request = self.github.find_open_pr(request.branch)
        if pull_request is None:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_PR_REQUIRED",
                "managed Draft PR is missing",
            )
        require_managed_pr(
            pull_request,
            request.branch,
            request.event.default_branch,
        )
        if not self.git.is_worktree_clean():
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_WORKTREE_DIRTY",
                "candidate worktree is not clean",
            )
        if self.git.current_sha() != pull_request.head_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_HEAD_MISMATCH",
                "candidate checkout does not match the live PR head",
            )
        if self.git.remote_branch_sha(request.branch) != pull_request.head_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_HEAD_MISMATCH",
                "remote branch does not match the live PR head",
            )
        if self.git.remote_branch_sha(request.event.default_branch) != base_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_BASE_MOVED",
                "default branch moved after routing",
            )

        changed = self.git.changed_paths(base_sha, pull_request.head_sha)
        manifest = prepare_existing_intake(
            self.paths,
            request,
            self.prepare,
            event_path=context.event_path,
            runner_temp=context.runner_temp,
        )
        product_key = str(request.intake.product_key)
        reset_paths = tuple(
            path
            for path in changed.paths
            if not self.paths.is_intake_write_allowed(path, product_key)
        )
        if any(
            not self.paths.is_writeback_allowed(path, product_key)
            for path in reset_paths
        ):
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_PATH_NOT_ALLOWED",
                "candidate contains a path outside the reprocessing boundary",
            )

        if reset_paths:
            table_ids = self._load_registry_ownership(
                product_key,
                str(manifest.product_id),
            )
            if any(
                not self.paths.is_base_sync_reset_allowed(
                    path,
                    product_key,
                    str(manifest.product_id),
                    table_ids,
                )
                for path in reset_paths
            ):
                raise WorkflowSecurityError(
                    "ISSUE_BASE_SYNC_PATH_NOT_ALLOWED",
                    "candidate contains unrelated derived output",
                )

        product_root = Path("products") / product_key
        intake_paths = (
            product_root / "product.yaml",
            product_root / "intake-manifest.json",
            *(product_root / item.relative_path for item in manifest.files),
        )
        if any(
            not self.paths.is_intake_write_allowed(path, product_key)
            for path in intake_paths
        ):
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_PATH_NOT_ALLOWED",
                "approved intake contains a path outside the preservation boundary",
            )
        intake_paths = tuple(
            sorted(set(intake_paths), key=lambda path: path.as_posix())
        )
        reset_paths = tuple(sorted(reset_paths, key=lambda path: path.as_posix()))
        self._require_same_managed_pr(request, pull_request)
        if self.git.remote_branch_sha(request.branch) != pull_request.head_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_HEAD_MISMATCH",
                "remote branch moved during candidate validation",
            )
        if self.git.remote_branch_sha(request.event.default_branch) != base_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_BASE_MOVED",
                "default branch moved during candidate validation",
            )
        merge = self.git.merge_revision(
            base_sha,
            f"chore({product_key}): merge main before reprocessing",
        )
        self.git.restore_paths(pull_request.head_sha, intake_paths)
        preserved = self.git.commit_intake_paths(
            product_key,
            f"data({product_key}): preserve approved intake after base sync",
        )
        prepare_existing_intake(
            self.paths,
            request,
            self.prepare,
            event_path=context.event_path,
            runner_temp=context.runner_temp,
        )
        self.git.restore_paths(base_sha, reset_paths)
        reset = self.git.commit_allowed_paths(
            product_key,
            f"data({product_key}): reset generated outputs after base sync",
        )
        final_sha = reset.sha
        if not self.git.is_ancestor(base_sha, final_sha):
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_ANCESTRY_MISMATCH",
                "trusted base is not an ancestor of synchronized head",
            )
        if not self.git.is_worktree_clean():
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_WORKTREE_DIRTY",
                "candidate worktree is dirty after synchronization",
            )
        self._require_same_managed_pr(request, pull_request)
        if self.git.remote_branch_sha(request.branch) != pull_request.head_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_HEAD_MISMATCH",
                "remote branch moved during synchronization",
            )
        if self.git.remote_branch_sha(request.event.default_branch) != base_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_BASE_MOVED",
                "default branch moved during synchronization",
            )
        self.git.push(request.branch, lfs=False)
        if self.git.remote_branch_sha(request.branch) != final_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_HEAD_MISMATCH",
                "published branch does not match synchronized head",
            )

        mutations = [
            MutationRecord(
                resource="commit",
                target=result.sha,
                action="create",
            )
            for result in (merge, preserved, reset)
            if result.created
        ]
        return WorkflowResult(
            command="workflow.issue-base-sync",
            status=(
                WorkflowStatus.SUCCESS
                if merge.created or reset.created
                else WorkflowStatus.NOOP
            ),
            outputs={
                "branch": request.branch,
                "product_key": product_key,
                "pr_number": pull_request.number,
                "expected_head": final_sha,
                "product_id": str(manifest.product_id),
            },
            mutations=mutations,
        )

    def _require_same_managed_pr(
        self,
        request: IssueRequest,
        expected: PullRequestState,
    ) -> None:
        live = self.github.find_open_pr(request.branch)
        if live is None:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_PR_REQUIRED",
                "managed Draft PR is missing",
            )
        require_managed_pr(live, request.branch, request.event.default_branch)
        if live.number != expected.number:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_PULL_REQUEST_MISMATCH",
                "managed Draft PR identity moved during synchronization",
            )
        if live.head_sha != expected.head_sha:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_HEAD_MISMATCH",
                "managed Draft PR head moved during synchronization",
            )

    def _load_registry_ownership(
        self,
        product_key: str,
        product_id: str,
    ) -> set[str]:
        try:
            product_path = self.paths.resolve_read(
                Path("registry") / "products" / f"{product_id}.json"
            )
            product = ProductRecord.model_validate_json(
                product_path.read_text(encoding="utf-8")
            )
            mapping_path = self.paths.resolve_read(
                Path("registry") / "mappings" / f"{product_id}.json"
            )
            mappings = TypeAdapter(list[ProductTableRef]).validate_json(
                mapping_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, PathPolicyError) as error:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_OUTPUT_REGISTRY_INVALID",
                "candidate output Registry ownership is missing or invalid",
            ) from error

        if str(product.product_id) != product_id or str(product.product_key) != product_key:
            raise WorkflowSecurityError(
                "ISSUE_BASE_SYNC_OUTPUT_REGISTRY_MISMATCH",
                "candidate Registry product identity does not match approved intake",
            )

        table_ids: set[str] = set()
        link_ids: set[str] = set()
        for mapping in mappings:
            table_id = str(mapping.table_id)
            if str(mapping.product_id) != product_id or str(mapping.link_id) in link_ids:
                raise WorkflowSecurityError(
                    "ISSUE_BASE_SYNC_OUTPUT_REGISTRY_MISMATCH",
                    "candidate Registry mapping ownership is inconsistent",
                )
            link_ids.add(str(mapping.link_id))
            try:
                table_path = self.paths.resolve_read(
                    Path("registry") / "tables" / f"{table_id}.json"
                )
                table = TableRecord.model_validate_json(
                    table_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, PathPolicyError) as error:
                raise WorkflowSecurityError(
                    "ISSUE_BASE_SYNC_OUTPUT_REGISTRY_INVALID",
                    "candidate Registry table ownership is missing or invalid",
                ) from error
            if str(table.table_id) != table_id or table.version != mapping.table_version:
                raise WorkflowSecurityError(
                    "ISSUE_BASE_SYNC_OUTPUT_REGISTRY_MISMATCH",
                    "candidate Registry table identity is inconsistent",
                )
            table_ids.add(table_id)
        return table_ids
