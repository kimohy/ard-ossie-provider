from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowConflict,
    WorkflowTransientError,
)


class GitHubConflict(WorkflowConflict):
    pass


class GitHubTransientError(WorkflowTransientError):
    pass


@dataclass(frozen=True)
class RepositoryState:
    full_name: str
    public: bool
    archived: bool
    default_branch: str
    permission: str


@dataclass(frozen=True)
class CollaboratorState:
    login: str
    permission: str


@dataclass(frozen=True)
class PullRequestState:
    number: int
    head_branch: str
    head_sha: str
    base_branch: str
    draft: bool
    merged_at: str | None
    merge_sha: str | None
    url: str


@dataclass(frozen=True)
class ReleaseAssetState:
    name: str
    digest: str | None
    url: str


@dataclass(frozen=True)
class ReleaseAssetPayload:
    name: str
    payload: bytes

    def __post_init__(self) -> None:
        if (
            not self.name
            or self.name in {".", ".."}
            or "/" in self.name
            or "\\" in self.name
            or "\x00" in self.name
        ):
            raise ValueError("release asset name must be a plain file name")
        if not isinstance(self.payload, bytes):
            raise TypeError("release asset payload must be immutable bytes")


@dataclass(frozen=True)
class ReleaseState:
    id: int
    tag: str
    title: str
    draft: bool
    prerelease: bool
    assets: tuple[ReleaseAssetState, ...]


@dataclass(frozen=True)
class LabelState:
    name: str
    color: str
    description: str


@dataclass(frozen=True)
class ActionsPermissionState:
    default_workflow_permissions: str
    can_approve_pull_request_reviews: bool


@dataclass(frozen=True)
class EnvironmentReviewer:
    kind: str
    id: int
    login: str


@dataclass(frozen=True)
class EnvironmentState:
    name: str
    reviewers: tuple[EnvironmentReviewer, ...]
    prevent_self_review: bool
    wait_timer: int
    branch_patterns: tuple[str, ...]


@dataclass(frozen=True)
class BranchProtectionState:
    required_statuses: tuple[str, ...]
    strict: bool
    enforce_admins: bool
    required_approving_review_count: int
    require_conversation_resolution: bool
    allow_force_pushes: bool
    allow_deletions: bool
    require_pull_request: bool = True


class GitHubPort(Protocol):
    def repository(self) -> RepositoryState: ...

    def branch_sha(self, branch: str) -> str | None: ...

    def collaborator_permission(self, login: str) -> str: ...

    def list_collaborators(self) -> tuple[CollaboratorState, ...]: ...

    def user_reviewer(self, login: str) -> EnvironmentReviewer: ...

    def find_open_pr(self, branch: str) -> PullRequestState | None: ...

    def get_pr(self, number: int) -> PullRequestState: ...

    def create_draft_pr(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestState: ...

    def set_issue_labels(
        self,
        number: int,
        *,
        add: set[str],
        remove: set[str],
    ) -> list[MutationRecord]: ...

    def upsert_pr_comment(
        self,
        number: int,
        marker: str,
        body: str,
    ) -> MutationRecord: ...

    def set_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ) -> MutationRecord: ...

    def get_status(self, sha: str, context: str) -> str | None: ...

    def dispatch_workflow(
        self,
        workflow: str,
        ref: str,
        inputs: Mapping[str, str],
    ) -> MutationRecord: ...

    def get_release(self, tag: str) -> ReleaseState | None: ...

    def upsert_release(
        self,
        tag: str,
        title: str,
        asset: ReleaseAssetPayload | Path,
        sha256: str,
    ) -> MutationRecord: ...

    def repository_dispatch(
        self,
        event_type: str,
        payload: Mapping[str, object],
    ) -> MutationRecord: ...

    def list_labels(self) -> Mapping[str, LabelState]: ...

    def upsert_label(self, name: str, color: str, description: str) -> MutationRecord: ...

    def get_actions_permissions(self) -> ActionsPermissionState: ...

    def set_actions_permissions(self, state: ActionsPermissionState) -> MutationRecord: ...

    def get_environment(self, name: str) -> EnvironmentState | None: ...

    def upsert_environment(self, state: EnvironmentState) -> MutationRecord: ...

    def list_environment_secret_names(self, environment: str) -> frozenset[str]: ...

    def set_environment_secret(
        self,
        environment: str,
        name: str,
        value: str,
    ) -> MutationRecord: ...

    def set_variable(
        self,
        name: str,
        value: str,
        environment: str | None = None,
    ) -> MutationRecord: ...

    def list_variables(self, environment: str | None = None) -> Mapping[str, str]: ...

    def get_branch_protection(self, branch: str) -> BranchProtectionState | None: ...

    def set_branch_protection(
        self,
        branch: str,
        state: BranchProtectionState,
    ) -> MutationRecord: ...
