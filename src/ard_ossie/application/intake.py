from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ard_ossie.application.contracts import (
    MutationRecord,
    WorkflowContext,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.github_event import (
    AttachmentSecurityError,
    IntakeManifest,
    IssueIntake,
    parse_issue_body,
    prepare_issue_event,
)
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.git import GitPort
from ard_ossie.ports.github import GitHubPort, PullRequestState


@dataclass(frozen=True)
class _IssueEvent:
    number: int
    body: str
    label: str
    actor: str
    default_branch: str
    repository_name: str


IssueEvent = _IssueEvent


@dataclass(frozen=True)
class IssueRequest:
    event: IssueEvent
    intake: IssueIntake
    branch: str


class IssueAuthorizationService:
    def __init__(self, github: GitHubPort) -> None:
        self.github = github

    def run(
        self,
        context: WorkflowContext,
        *,
        label: str | None = None,
        actor: str | None = None,
    ) -> WorkflowResult:
        event = _load_issue_event(context)
        _require_matching_context(context, event)
        if label is not None and label != event.label:
            raise WorkflowSecurityError(
                "ISSUE_LABEL_MISMATCH",
                "requested label does not match the trusted issue event",
            )
        if actor is not None and actor != event.actor:
            raise WorkflowSecurityError(
                "ISSUE_ACTOR_MISMATCH",
                "requested actor does not match the trusted issue event",
            )
        selected_label = event.label
        selected_actor = event.actor
        if selected_label != "ard:approved":
            raise WorkflowSecurityError(
                "ISSUE_APPROVAL_LABEL_REQUIRED",
                "issue authorization requires the ard:approved label",
            )
        if not selected_actor:
            raise WorkflowValidationError("ISSUE_ACTOR_REQUIRED", "issue actor is missing")
        permission = self.github.collaborator_permission(selected_actor)
        if permission.casefold() not in {"admin", "maintain", "write"}:
            raise WorkflowSecurityError(
                "ISSUE_APPROVER_PERMISSION_DENIED",
                "issue approver requires write permission",
            )
        return WorkflowResult(
            command="workflow.issue-authorize",
            status=WorkflowStatus.SUCCESS,
            outputs={"allowed": True},
        )


class IssueIntakeService:
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

    def run(self, context: WorkflowContext) -> WorkflowResult:
        request = load_issue_request(context)
        event = request.event
        intake = request.intake
        branch = request.branch
        existing = self.github.find_open_pr(branch)
        branch_checked_out = False
        if existing is not None:
            self.git.switch_or_create(branch, event.default_branch)
            branch_checked_out = True
            remote_head = self.git.remote_branch_sha(branch)
            if remote_head is None or self.git.current_sha() != remote_head:
                raise WorkflowSecurityError(
                    "ISSUE_EXISTING_HEAD_MISMATCH",
                    "existing intake branch is not checked out at its remote head",
                )
            _require_equivalent_pr(
                existing,
                event.default_branch,
                expected_head=remote_head,
            )
            changed = self.git.changed_paths(event.default_branch, remote_head)
            marker = (
                Path("products")
                / str(intake.product_key)
                / "changesets"
                / f"{intake.changeset_id}.json"
                if intake.changeset_id
                else None
            )
            if marker is not None and set(changed.paths) == {marker}:
                pass
            elif not changed.paths or any(
                not self.paths.is_intake_write_allowed(path, str(intake.product_key))
                for path in changed.paths
            ):
                raise WorkflowSecurityError(
                    "ISSUE_EXISTING_PATH_NOT_ALLOWED",
                    "existing intake branch contains changes outside intake paths",
                )
            if marker is None or set(changed.paths) != {marker}:
                manifest = prepare_existing_intake(
                    self.paths,
                    request,
                    self.prepare,
                    event_path=context.event_path,
                    runner_temp=context.runner_temp,
                )
                mutations = self.github.set_issue_labels(
                    event.number,
                    add={"ard:processing", "ard:pr-created"},
                    remove=set(),
                )
                return _intake_result(
                    status=WorkflowStatus.NOOP,
                    branch=branch,
                    product_key=str(intake.product_key),
                    pull_request=existing,
                    mutations=mutations,
                    product_id=manifest.product_id,
                )

        mutations = self.github.set_issue_labels(
            event.number,
            add={"ard:processing"},
            remove={"ard:failed"},
        )
        if not branch_checked_out:
            self.git.switch_or_create(branch, event.default_branch)
        try:
            manifest = self.prepare(context.event_path, self.paths.root)  # type: ignore[arg-type]
        except AttachmentSecurityError as error:
            raise WorkflowSecurityError(_error_code(error), "unsafe issue attachment") from error
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise WorkflowValidationError(_error_code(error), "invalid issue intake") from error
        if manifest.issue_number != event.number or manifest.product_key != intake.product_key:
            raise WorkflowSecurityError(
                "ISSUE_MANIFEST_MISMATCH",
                "prepared intake does not match the trusted issue event",
            )

        commit = self.git.commit_intake_paths(
            intake.product_key,
            f"data: ingest ARD issue #{event.number}",
        )
        mutations.append(
            MutationRecord(
                resource="commit",
                target=commit.sha,
                action="create" if commit.created else "noop",
            )
        )
        self.git.push(branch, lfs=True)

        pull_request = self.github.find_open_pr(branch)
        if pull_request is None:
            pull_request = self.github.create_draft_pr(
                branch,
                event.default_branch,
                f"data({intake.product_key}): ARD issue #{event.number}",
                f"Closes #{event.number}",
            )
            mutations.append(
                MutationRecord(
                    resource="pull_request",
                    target=f"pr:{pull_request.number}",
                    action="create",
                    result_id=str(pull_request.number),
                )
            )
        _require_equivalent_pr(pull_request, event.default_branch, expected_head=commit.sha)
        mutations.extend(
            self.github.set_issue_labels(
                event.number,
                add={"ard:pr-created"},
                remove=set(),
            )
        )
        return _intake_result(
            status=WorkflowStatus.SUCCESS,
            branch=branch,
            product_key=str(intake.product_key),
            pull_request=pull_request,
            mutations=mutations,
            product_id=manifest.product_id,
        )


def _load_issue_event(context: WorkflowContext) -> _IssueEvent:
    if context.event_path is None:
        raise WorkflowValidationError("ISSUE_EVENT_REQUIRED", "issue event path is missing")
    if context.event_name not in (None, "issues"):
        raise WorkflowSecurityError("ISSUE_EVENT_TYPE_MISMATCH", "expected an issues event")
    try:
        payload = json.loads(context.event_path.read_text(encoding="utf-8"))
        issue = _mapping(payload["issue"])
        label = _mapping(payload["label"])
        repository = _mapping(payload["repository"])
        sender = _mapping(payload.get("sender"))
        number = int(issue["number"])
        body = str(issue["body"])
        label_name = str(label["name"])
        actor = str(sender["login"])
        default_branch = str(repository["default_branch"])
        repository_name = str(repository["full_name"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise WorkflowValidationError("INVALID_ISSUE_EVENT", "issue event is malformed") from error
    if number <= 0 or not all((body, label_name, actor, default_branch, repository_name)):
        raise WorkflowValidationError("INVALID_ISSUE_EVENT", "issue event is incomplete")
    return _IssueEvent(
        number=number,
        body=body,
        label=label_name,
        actor=actor,
        default_branch=default_branch,
        repository_name=repository_name,
    )


def _require_matching_context(context: WorkflowContext, event: _IssueEvent) -> None:
    if context.repository_name and context.repository_name != event.repository_name:
        raise WorkflowSecurityError(
            "ISSUE_REPOSITORY_MISMATCH",
            "issue event repository does not match workflow context",
        )
    if context.actor and context.actor != event.actor:
        raise WorkflowSecurityError(
            "ISSUE_ACTOR_MISMATCH",
            "issue event actor does not match workflow context",
        )


def _require_equivalent_pr(
    pull_request: PullRequestState,
    base_branch: str,
    *,
    expected_head: str | None = None,
) -> None:
    if pull_request.base_branch != base_branch or not pull_request.draft:
        raise WorkflowSecurityError(
            "ISSUE_PULL_REQUEST_MISMATCH",
            "existing pull request is not the managed Draft PR",
        )
    if expected_head is not None and pull_request.head_sha != expected_head:
        raise WorkflowSecurityError(
            "ISSUE_PULL_REQUEST_HEAD_MISMATCH",
            "pull request head does not match the committed intake",
        )


def require_managed_pr(
    pull_request: PullRequestState,
    branch: str,
    base_branch: str,
) -> None:
    if (
        pull_request.head_branch != branch
        or pull_request.base_branch != base_branch
        or not pull_request.draft
        or pull_request.merged_at is not None
    ):
        raise WorkflowSecurityError(
            "ISSUE_BASE_SYNC_PULL_REQUEST_MISMATCH",
            "existing pull request is not the managed Draft PR",
        )


def load_issue_request(context: WorkflowContext) -> IssueRequest:
    event = _load_issue_event(context)
    _require_matching_context(context, event)
    try:
        intake = parse_issue_body(event.body)
    except AttachmentSecurityError as error:
        raise WorkflowSecurityError(_error_code(error), "unsafe issue attachment") from error
    except (ValueError, TypeError) as error:
        raise WorkflowValidationError(_error_code(error), "invalid issue intake") from error
    branch = (
        f"ard/{intake.changeset_id}-{intake.product_key}"
        if intake.changeset_id
        else f"ard/issue-{event.number}-{intake.product_key}"
    )
    return IssueRequest(event=event, intake=intake, branch=branch)


def prepare_existing_intake(
    paths: FileSystemPort,
    request: IssueRequest,
    prepare: Callable[[Path, Path], IntakeManifest],
    *,
    event_path: Path | None,
    runner_temp: Path | None,
) -> IntakeManifest:
    if event_path is None:
        raise WorkflowValidationError("ISSUE_EVENT_REQUIRED", "issue event path is missing")
    with tempfile.TemporaryDirectory(
        prefix="ard-intake-verify-",
        dir=runner_temp,
    ) as staging_directory:
        staging = Path(staging_directory)
        if request.intake.operation.value == "update":
            staged_config = (
                staging
                / "products"
                / str(request.intake.product_key)
                / "product.yaml"
            )
            staged_config.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                paths.resolve_read(
                    Path("products")
                    / str(request.intake.product_key)
                    / "product.yaml"
                ),
                staged_config,
            )
        canonical = prepare(event_path, staging)
        return _validate_existing_intake(
            paths,
            request.event,
            request.intake,
            canonical,
            staging,
        )


def _validate_existing_intake(
    paths: FileSystemPort,
    event: _IssueEvent,
    intake: IssueIntake,
    canonical: IntakeManifest,
    canonical_workspace: Path,
) -> IntakeManifest:
    product_root = Path("products") / str(intake.product_key)
    try:
        manifest_path = paths.resolve_read(product_root / "intake-manifest.json")
        manifest = IntakeManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        config_path = paths.resolve_read(product_root / "product.yaml")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        raise WorkflowSecurityError(
            "ISSUE_EXISTING_INTAKE_INVALID",
            "existing intake metadata is malformed",
        ) from error
    if not isinstance(config, dict):
        raise WorkflowSecurityError(
            "ISSUE_EXISTING_INTAKE_INVALID",
            "existing product config is not a mapping",
        )
    expected_config = {
        "operation": intake.operation.value,
        "product_id": str(intake.product_id or manifest.product_id),
        "product_key": str(intake.product_key),
        "version": int(intake.version),
        "display_name": intake.display_name,
        "description": intake.description,
        "changeset_id": intake.changeset_id,
    }
    if any(config.get(key) != value for key, value in expected_config.items()):
        raise WorkflowSecurityError(
            "ISSUE_EXISTING_CONFIG_MISMATCH",
            "existing product config does not match approved issue input",
        )
    if (
        manifest.issue_number != event.number
        or str(manifest.product_key) != str(intake.product_key)
        or manifest.version != intake.version
        or (intake.product_id is not None and manifest.product_id != intake.product_id)
    ):
        raise WorkflowSecurityError(
            "ISSUE_EXISTING_MANIFEST_MISMATCH",
            "existing intake manifest does not match approved issue input",
        )

    attachments = intake.attachments
    canonical_files = {item.role: item for item in canonical.files}
    if (
        {item.role for item in manifest.files} != set(attachments)
        or set(canonical_files) != set(attachments)
    ):
        raise WorkflowSecurityError(
            "ISSUE_EXISTING_MANIFEST_MISMATCH",
            "existing intake manifest has an unexpected attachment set",
        )
    recorded_paths: set[Path] = set()
    for item in manifest.files:
        relative = Path(item.relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise WorkflowSecurityError(
                "ISSUE_EXISTING_SOURCE_PATH_UNSAFE",
                "existing intake manifest contains an unsafe source path",
            )
        source = paths.resolve_read(product_root / relative)
        expected_attachment = attachments[item.role]
        content = source.read_bytes()
        canonical_item = canonical_files[item.role]
        canonical_source = (
            canonical_workspace
            / "products"
            / str(intake.product_key)
            / canonical_item.relative_path
        )
        canonical_content = canonical_source.read_bytes()
        if (
            relative.parts[0] != "sources"
            or relative != Path(canonical_item.relative_path)
            or item.source_url != expected_attachment.url
            or canonical_item.source_url != expected_attachment.url
            or item.filename != expected_attachment.filename
            or canonical_item.filename != expected_attachment.filename
            or item.size_bytes != len(content)
            or item.sha256 != hashlib.sha256(content).hexdigest()
            or canonical_item.sha256 != hashlib.sha256(canonical_content).hexdigest()
            or item.sha256 != canonical_item.sha256
            or content != canonical_content
        ):
            raise WorkflowSecurityError(
                "ISSUE_EXISTING_SOURCE_MISMATCH",
                "existing source does not match its approved attachment",
            )
        recorded_paths.add(source)
    source_root = paths.resolve_read(product_root / "sources")
    actual_paths = {
        paths.resolve_read(path)
        for path in source_root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_paths != recorded_paths:
        raise WorkflowSecurityError(
            "ISSUE_EXISTING_SOURCE_SET_MISMATCH",
            "existing source tree differs from its intake manifest",
        )
    return manifest


def _intake_result(
    *,
    status: WorkflowStatus,
    branch: str,
    product_key: str,
    pull_request: PullRequestState,
    mutations: list[MutationRecord],
    product_id: str | None = None,
) -> WorkflowResult:
    outputs: dict[str, Any] = {
        "branch": branch,
        "expected_head": pull_request.head_sha,
        "pr_number": pull_request.number,
        "product_key": product_key,
    }
    if product_id is not None:
        outputs["product_id"] = product_id
    return WorkflowResult(
        command="workflow.issue-intake",
        status=status,
        outputs=outputs,
        mutations=mutations,
    )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _error_code(error: Exception) -> str:
    return str(error).partition(":")[0] or type(error).__name__
