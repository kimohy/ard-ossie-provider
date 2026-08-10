from __future__ import annotations

from ard_ossie.application.contracts import MutationRecord, WorkflowConfigurationError
from ard_ossie.application.github_bootstrap import (
    BootstrapConfig,
    GitHubBootstrapService,
)
from ard_ossie.ports.github import (
    ActionsPermissionState,
    BranchProtectionState,
    CollaboratorState,
    EnvironmentReviewer,
    EnvironmentState,
    LabelState,
    RepositoryState,
)

REPOSITORY = "kimohy/ard-ossie-provider"


class FakeGitHub:
    def __init__(self) -> None:
        self.repository_state = RepositoryState(
            full_name=REPOSITORY,
            public=True,
            archived=False,
            default_branch="main",
            permission="admin",
        )
        self.labels: dict[str, LabelState] = {}
        self.actions = ActionsPermissionState(
            default_workflow_permissions="write",
            can_approve_pull_request_reviews=False,
        )
        self.environments: dict[str, EnvironmentState] = {}
        self.variables: dict[str, dict[str, str]] = {}
        self.secrets: dict[str, set[str]] = {}
        self.protection: BranchProtectionState | None = None
        self.collaborators: tuple[CollaboratorState, ...] = ()
        self.secret_inputs: list[str] = []

    def repository(self) -> RepositoryState:
        return self.repository_state

    def user_reviewer(self, login: str) -> EnvironmentReviewer:
        return EnvironmentReviewer(kind="User", id=11, login=login)

    def list_labels(self):
        return dict(self.labels)

    def upsert_label(self, name: str, color: str, description: str):
        action = "create" if name not in self.labels else "update"
        desired = LabelState(name=name, color=color.casefold(), description=description)
        if self.labels.get(name) == desired:
            action = "noop"
        self.labels[name] = desired
        return MutationRecord(resource="label", target=name, action=action)

    def get_actions_permissions(self):
        return self.actions

    def set_actions_permissions(self, state):
        self.actions = state
        return MutationRecord(resource="actions_permissions", target=REPOSITORY, action="set")

    def get_environment(self, name: str):
        return self.environments.get(name)

    def upsert_environment(self, state):
        action = "noop" if self.environments.get(state.name) == state else "upsert"
        self.environments[state.name] = state
        return MutationRecord(resource="environment", target=state.name, action=action)

    def list_variables(self, environment: str | None = None):
        return dict(self.variables.get(environment or "repository", {}))

    def set_variable(self, name: str, value: str, environment: str | None = None):
        self.variables.setdefault(environment or "repository", {})[name] = value
        return MutationRecord(resource="variable", target=f"{environment}:{name}", action="set")

    def list_environment_secret_names(self, environment: str):
        return frozenset(self.secrets.get(environment, set()))

    def set_environment_secret(self, environment: str, name: str, value: str):
        self.secret_inputs.append(value)
        self.secrets.setdefault(environment, set()).add(name)
        return MutationRecord(
            resource="environment_secret",
            target=f"{environment}:{name}",
            action="set",
        )

    def get_branch_protection(self, branch: str):
        assert branch == "main"
        return self.protection

    def set_branch_protection(self, branch: str, state):
        self.protection = state
        return MutationRecord(resource="branch_protection", target=f"branch:{branch}", action="set")

    def list_collaborators(self):
        return self.collaborators


def provider_config() -> BootstrapConfig:
    return BootstrapConfig(
        base_url="https://api.openai.com/v1",
        model="gpt-example",
        api_style="chat_completions",
        max_attachment_bytes=52_428_800,
    )


def test_bootstrap_plan_contains_exact_project_resources() -> None:
    service = GitHubBootstrapService(REPOSITORY, FakeGitHub())

    plan = service.plan(provider_config())

    assert [item.target for item in plan.items] == [
        "label:ard:submission",
        "label:ard:approved",
        "label:ard:processing",
        "label:ard:failed",
        "label:ard:pr-created",
        "actions:workflow-permissions",
        "environment:ard-llm",
        "environment:production-linkage",
        "branch:main",
    ]


def test_second_bootstrap_is_noop_and_secret_never_enters_result() -> None:
    github = FakeGitHub()
    service = GitHubBootstrapService(REPOSITORY, github)
    plan = service.plan(provider_config())

    result = service.apply(plan, api_key="sentinel-key")
    second = service.plan(provider_config())

    assert all(item.action == "noop" for item in second.items)
    assert github.secret_inputs == ["sentinel-key"]
    assert "sentinel-key" not in result.model_dump_json()
    assert github.environments["ard-llm"].branch_patterns == ("main",)


def test_apply_replans_noop_resources_after_confirmation_drift() -> None:
    github = FakeGitHub()
    service = GitHubBootstrapService(REPOSITORY, github)
    service.apply(service.plan(provider_config()), api_key="sentinel-key")
    confirmed_plan = service.plan(provider_config())
    assert all(item.action == "noop" for item in confirmed_plan.items)

    github.protection = None
    result = service.apply(confirmed_plan)

    assert github.protection is not None
    assert any(
        mutation.resource == "branch_protection" for mutation in result.mutations
    )


def test_enable_review_protection_changes_only_approval_count() -> None:
    github = FakeGitHub()
    github.protection = BranchProtectionState(
        required_statuses=("ard/changeset", "ard/quality-gate"),
        strict=True,
        enforce_admins=True,
        required_approving_review_count=0,
        require_conversation_resolution=True,
        allow_force_pushes=False,
        allow_deletions=False,
        require_pull_request=True,
    )
    github.collaborators = (
        CollaboratorState(login="kimohy", permission="admin"),
        CollaboratorState(login="reviewer", permission="write"),
    )
    service = GitHubBootstrapService(REPOSITORY, github)

    service.enable_review_protection()

    assert github.protection == BranchProtectionState(
        required_statuses=("ard/changeset", "ard/quality-gate"),
        strict=True,
        enforce_admins=True,
        required_approving_review_count=1,
        require_conversation_resolution=True,
        allow_force_pushes=False,
        allow_deletions=False,
        require_pull_request=True,
    )


def test_enable_review_protection_requires_non_owner_writer() -> None:
    github = FakeGitHub()
    github.collaborators = (CollaboratorState(login="kimohy", permission="admin"),)

    try:
        GitHubBootstrapService(REPOSITORY, github).enable_review_protection()
    except WorkflowConfigurationError as error:
        assert error.code == "ELIGIBLE_REVIEWER_NOT_FOUND"
    else:
        raise AssertionError("missing eligible reviewer must block protection")

    assert github.protection is None
