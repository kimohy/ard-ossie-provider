from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from pydantic import field_validator

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowConfigurationError,
    WorkflowError,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.models import StrictModel
from ard_ossie.ports.github import (
    ActionsPermissionState,
    BranchProtectionState,
    EnvironmentReviewer,
    EnvironmentState,
    GitHubPort,
    LabelState,
    RepositoryState,
)

_LABELS = (
    LabelState(
        name="ard:submission",
        color="1d76db",
        description="Public AI Ready Data submission",
    ),
    LabelState(
        name="ard:approved",
        color="0e8a16",
        description="Maintainer approved public ingestion",
    ),
    LabelState(
        name="ard:processing",
        color="fbca04",
        description="ARD ingestion and conversion in progress",
    ),
    LabelState(
        name="ard:failed",
        color="d93f0b",
        description="ARD ingestion or conversion failed",
    ),
    LabelState(
        name="ard:pr-created",
        color="5319e7",
        description="Draft product PR created",
    ),
)
_ACTIONS = ActionsPermissionState(
    default_workflow_permissions="read",
    can_approve_pull_request_reviews=True,
)
_PROTECTION = BranchProtectionState(
    required_statuses=("ard/changeset", "ard/quality-gate"),
    strict=True,
    enforce_admins=True,
    required_approving_review_count=0,
    require_conversation_resolution=True,
    allow_force_pushes=False,
    allow_deletions=False,
    require_pull_request=True,
)
_SECRET_NAME = "ARD_LLM_API_KEY"
_PROFILE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_VALUE = re.compile(r"^[^\x00\r\n]{1,200}$")


class BootstrapConfig(StrictModel):
    profile: str = "openai-compatible-default"
    base_url: str = "https://api.openai.com/v1"
    azure_endpoint: str | None = None
    gcp_project_id: str | None = None
    max_attachment_bytes: int = 52_428_800

    @field_validator("base_url", "azure_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("https://", "http://")) or any(
            character in normalized for character in ("\x00", "\n", "\r")
        ):
            raise ValueError("INVALID_PROVIDER_CONFIG")
        return normalized

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        normalized = value.strip()
        if _PROFILE_NAME.fullmatch(normalized) is None:
            raise ValueError("INVALID_PROVIDER_CONFIG")
        return normalized

    @field_validator("gcp_project_id")
    @classmethod
    def validate_gcp_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if _SAFE_VALUE.fullmatch(normalized) is None:
            raise ValueError("INVALID_PROVIDER_CONFIG")
        return normalized

    @field_validator("max_attachment_bytes")
    @classmethod
    def validate_attachment_limit(cls, value: int) -> int:
        if not 1 <= value <= 1_073_741_824:
            raise ValueError("INVALID_PROVIDER_CONFIG")
        return value

    def variables(self) -> dict[str, str]:
        variables = {
            "ARD_LLM_PROFILE": self.profile,
            "ARD_LLM_BASE_URL": self.base_url,
            "ARD_MAX_ATTACHMENT_BYTES": str(self.max_attachment_bytes),
        }
        if self.azure_endpoint is not None:
            variables["ARD_AZURE_OPENAI_ENDPOINT"] = self.azure_endpoint
        if self.gcp_project_id is not None:
            variables["ARD_GCP_PROJECT_ID"] = self.gcp_project_id
        return variables


class BootstrapItem(StrictModel):
    target: str
    action: Literal["create", "update", "noop", "blocked"]


class BootstrapPlan(StrictModel):
    repository: str
    owner_login: str
    owner_id: int
    config: BootstrapConfig
    secret_present: bool
    items: list[BootstrapItem]


class GitHubBootstrapService:
    def __init__(self, repository: str, github: GitHubPort) -> None:
        if repository.count("/") != 1 or any(
            not part or part in {".", ".."} for part in repository.split("/")
        ):
            raise WorkflowValidationError(
                "REPOSITORY_NAME_INVALID",
                "bootstrap repository must be OWNER/REPO",
            )
        self.repository_name = repository
        self.github = github

    def plan(self, config: BootstrapConfig) -> BootstrapPlan:
        repository = self._require_repository()
        owner_login = repository.full_name.split("/", 1)[0]
        owner = self.github.user_reviewer(owner_login)
        labels = self.github.list_labels()
        items = [
            BootstrapItem(
                target=f"label:{desired.name}",
                action=_desired_action(labels.get(desired.name), desired),
            )
            for desired in _LABELS
        ]
        items.append(
            BootstrapItem(
                target="actions:workflow-permissions",
                action=("noop" if self.github.get_actions_permissions() == _ACTIONS else "update"),
            )
        )
        llm = _llm_environment(owner)
        current_llm = self.github.get_environment(llm.name)
        llm_variables = self.github.list_variables(llm.name) if current_llm is not None else {}
        secret_names = (
            self.github.list_environment_secret_names(llm.name)
            if current_llm is not None
            else frozenset()
        )
        llm_drift = (
            current_llm != llm
            or any(llm_variables.get(name) != value for name, value in config.variables().items())
            or _SECRET_NAME not in secret_names
        )
        items.append(
            BootstrapItem(
                target="environment:ard-llm",
                action=("create" if current_llm is None else ("update" if llm_drift else "noop")),
            )
        )
        production = _production_environment(owner)
        current_production = self.github.get_environment(production.name)
        items.append(
            BootstrapItem(
                target="environment:production-linkage",
                action=_desired_action(current_production, production),
            )
        )
        current_protection = self.github.get_branch_protection("main")
        items.append(
            BootstrapItem(
                target="branch:main",
                action=(
                    "create"
                    if current_protection is None
                    else (
                        "noop"
                        if current_protection == _bootstrap_protection(current_protection)
                        else "update"
                    )
                ),
            )
        )
        return BootstrapPlan(
            repository=repository.full_name,
            owner_login=owner.login,
            owner_id=owner.id,
            config=config,
            secret_present=_SECRET_NAME in secret_names,
            items=items,
        )

    def apply(
        self,
        plan: BootstrapPlan,
        *,
        api_key: str | None = None,
        api_key_provider: Callable[[], str] | None = None,
        replace_secret: bool = False,
    ) -> WorkflowResult:
        if plan.repository != self.repository_name:
            raise WorkflowSecurityError(
                "BOOTSTRAP_PLAN_REPOSITORY_MISMATCH",
                "bootstrap plan targets another repository",
            )
        current_plan = self.plan(plan.config)
        needs_secret = not current_plan.secret_present or replace_secret
        if needs_secret and api_key is None and api_key_provider is None:
            raise WorkflowConfigurationError(
                "LLM_API_KEY_REQUIRED",
                "bootstrap requires a hidden LLM API key input",
            )
        owner = EnvironmentReviewer(
            kind="User",
            id=current_plan.owner_id,
            login=current_plan.owner_login,
        )
        mutations: list[MutationRecord] = []
        outputs: dict[str, object] = {
            "repository": current_plan.repository,
            "items": [item.model_dump(mode="json") for item in current_plan.items],
            "secret": (
                "replace"
                if replace_secret
                else ("present" if current_plan.secret_present else "create")
            ),
        }
        try:
            current_labels = self.github.list_labels()
            for desired in _LABELS:
                if current_labels.get(desired.name) != desired:
                    mutations.append(
                        self.github.upsert_label(
                            desired.name,
                            desired.color,
                            desired.description,
                        )
                    )
            if self.github.get_actions_permissions() != _ACTIONS:
                mutations.append(self.github.set_actions_permissions(_ACTIONS))

            llm = _llm_environment(owner)
            if self.github.get_environment(llm.name) != llm:
                mutations.append(self.github.upsert_environment(llm))
            current_variables = self.github.list_variables(llm.name)
            for name, value in sorted(plan.config.variables().items()):
                if current_variables.get(name) != value:
                    mutations.append(self.github.set_variable(name, value, llm.name))
            production = _production_environment(owner)
            if self.github.get_environment(production.name) != production:
                mutations.append(self.github.upsert_environment(production))

            if needs_secret:
                secret_value = api_key if api_key is not None else api_key_provider()
                if not isinstance(secret_value, str) or not secret_value:
                    raise WorkflowConfigurationError(
                        "LLM_API_KEY_REQUIRED",
                        "hidden LLM API key input was empty",
                    )
                try:
                    mutations.append(
                        self.github.set_environment_secret(
                            llm.name,
                            _SECRET_NAME,
                            secret_value,
                        )
                    )
                finally:
                    secret_value = ""

            current_protection = self.github.get_branch_protection("main")
            desired_protection = _bootstrap_protection(current_protection)
            if current_protection != desired_protection:
                mutations.append(
                    self.github.set_branch_protection(
                        "main",
                        desired_protection,
                    )
                )
            postcondition = self.plan(plan.config)
            remaining = [
                item.model_dump(mode="json")
                for item in postcondition.items
                if item.action != "noop"
            ]
            if remaining:
                outputs["remaining"] = remaining
                raise WorkflowPartialError(
                    "BOOTSTRAP_POSTCONDITION_FAILED",
                    "repository bootstrap did not reach the desired state",
                    retryable=True,
                    outputs=outputs,
                    mutations=mutations,
                )
        except WorkflowPartialError:
            raise
        except WorkflowError as error:
            if not mutations:
                raise
            raise WorkflowPartialError(
                "BOOTSTRAP_PARTIAL",
                "repository bootstrap did not converge",
                retryable=error.retryable,
                outputs=outputs,
                mutations=mutations,
            ) from error
        return WorkflowResult(
            command="github.bootstrap",
            status=(
                WorkflowStatus.SUCCESS
                if any(item.action != "noop" for item in mutations)
                else WorkflowStatus.NOOP
            ),
            outputs=outputs,
            mutations=mutations,
        )

    def enable_review_protection(self) -> WorkflowResult:
        repository = self._require_repository()
        owner = repository.full_name.split("/", 1)[0].casefold()
        eligible = sorted(
            collaborator.login
            for collaborator in self.github.list_collaborators()
            if collaborator.login.casefold() != owner
            and collaborator.permission.casefold() in {"write", "maintain", "admin"}
        )
        if not eligible:
            raise WorkflowConfigurationError(
                "ELIGIBLE_REVIEWER_NOT_FOUND",
                "a non-owner writer is required before enabling reviews",
            )
        current = self.github.get_branch_protection("main")
        if current is None:
            raise WorkflowConfigurationError(
                "BRANCH_PROTECTION_REQUIRED",
                "bootstrap branch protection must exist first",
            )
        if (
            current.required_approving_review_count not in {0, 1}
            or replace(current, required_approving_review_count=0) != _PROTECTION
        ):
            raise WorkflowConfigurationError(
                "BRANCH_PROTECTION_DRIFT",
                "review transition requires the bootstrap protection contract",
            )
        if current.required_approving_review_count == 1:
            return WorkflowResult(
                command="github.enable-review-protection",
                status=WorkflowStatus.NOOP,
                outputs={"eligible_reviewers": eligible, "required_reviews": 1},
            )
        desired = replace(current, required_approving_review_count=1)
        mutation = self.github.set_branch_protection("main", desired)
        return WorkflowResult(
            command="github.enable-review-protection",
            status=WorkflowStatus.SUCCESS,
            outputs={"eligible_reviewers": eligible, "required_reviews": 1},
            mutations=[mutation],
        )

    def _require_repository(self) -> RepositoryState:
        repository = self.github.repository()
        if (
            repository.full_name != self.repository_name
            or not repository.public
            or repository.archived
            or repository.default_branch != "main"
        ):
            raise WorkflowConfigurationError(
                "REPOSITORY_MISMATCH",
                "bootstrap requires the exact public main repository",
            )
        if repository.permission != "admin":
            raise WorkflowConfigurationError(
                "ADMIN_PERMISSION_REQUIRED",
                "bootstrap requires repository admin permission",
            )
        return repository


def _llm_environment(owner: EnvironmentReviewer) -> EnvironmentState:
    return EnvironmentState(
        name="ard-llm",
        reviewers=(owner,),
        prevent_self_review=False,
        wait_timer=0,
        branch_patterns=("main",),
    )


def _production_environment(owner: EnvironmentReviewer) -> EnvironmentState:
    return EnvironmentState(
        name="production-linkage",
        reviewers=(owner,),
        prevent_self_review=False,
        wait_timer=0,
        branch_patterns=("main",),
    )


def _desired_action(current: object | None, desired: object) -> Literal["create", "update", "noop"]:
    if current is None:
        return "create"
    return "noop" if current == desired else "update"


def _bootstrap_protection(
    current: BranchProtectionState | None,
) -> BranchProtectionState:
    review_count = (
        current.required_approving_review_count
        if current is not None and current.required_approving_review_count in {0, 1}
        else 0
    )
    return replace(_PROTECTION, required_approving_review_count=review_count)
