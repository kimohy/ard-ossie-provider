from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from pydantic import Field, ValidationError, field_validator

from ard_ossie.application.contracts import (
    ExitCode,
    MutationRecord,
    WorkflowConfigurationError,
    WorkflowError,
    WorkflowPartialError,
    WorkflowResult,
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowTransientError,
    WorkflowValidationError,
)
from ard_ossie.application.source_check import validate_changeset_binding
from ard_ossie.ingestion import SourceValidationError
from ard_ossie.models import StrictModel
from ard_ossie.pipeline import (
    PipelineSecurityError,
    PipelineValidationError,
    ProcessResult,
    ProviderExecutionError,
    ProviderFailureKind,
    process_product,
)
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.git import GitPort
from ard_ossie.ports.github import GitHubPort, PullRequestState

_PRODUCT_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_INVOCATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WORKFLOW_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_RetryResult = TypeVar("_RetryResult")


class ProcessingRequest(StrictModel):
    repository: Path
    product_key: str
    branch: str
    pr_number: int = Field(gt=0)
    expected_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    allow_writeback: bool
    warnings_as_errors: bool = False
    target_url: str = ""

    @field_validator("product_key")
    @classmethod
    def validate_product_key(cls, value: str) -> str:
        if not _PRODUCT_KEY.fullmatch(value):
            raise ValueError("INVALID_PRODUCT_KEY")
        return value

    @field_validator("branch")
    @classmethod
    def validate_processing_branch(cls, value: str) -> str:
        return _validated_branch(value)


class ProcessingReconcileRequest(StrictModel):
    repository: Path
    result_path: Path
    branch: str
    pr_number: int = Field(gt=0)
    invocation_id: str
    target_url: str = ""

    @field_validator("branch")
    @classmethod
    def validate_reconcile_branch(cls, value: str) -> str:
        return _validated_branch(value)

    @field_validator("invocation_id")
    @classmethod
    def validate_invocation_id(cls, value: str) -> str:
        return validate_processing_invocation_id(value)


class ProcessingService:
    def __init__(
        self,
        paths: FileSystemPort,
        git: GitPort,
        github: GitHubPort,
        *,
        processor: Callable[..., ProcessResult] = process_product,
        provider_factory: Callable[[], Any] = lambda: None,
    ) -> None:
        self.paths = paths
        self.git = git
        self.github = github
        self.processor = processor
        self.provider_factory = provider_factory

    def run(self, request: ProcessingRequest) -> WorkflowResult:
        if request.repository.expanduser().resolve() != self.paths.root:
            raise WorkflowSecurityError(
                "PROCESSING_REPOSITORY_MISMATCH",
                "processing request repository does not match the filesystem port",
            )
        pull_request = self.github.get_pr(request.pr_number)
        processing_head = self._require_expected_head(request, pull_request)

        product = self.paths.resolve_read(Path("products") / request.product_key)
        registry = self.paths.resolve_directory("registry", allow_missing=True)
        requested_changeset = validate_changeset_binding(
            self.paths,
            product,
            request.product_key,
        )
        if (
            requested_changeset is not None
            and request.branch != f"ard/{requested_changeset}-{request.product_key}"
        ):
            raise WorkflowSecurityError(
                "PROCESSING_CHANGESET_BRANCH_MISMATCH",
                "changeset processing requires the canonical tracking branch",
            )
        try:
            provider = self.provider_factory()
            processed = self.processor(
                product,
                registry_root=registry,
                provider=provider,
                pr_number=request.pr_number,
                warnings_as_errors=request.warnings_as_errors,
            )
        except PipelineSecurityError as error:
            raise WorkflowSecurityError(
                _error_code(error),
                "registry path security validation failed",
            ) from None
        except ProviderExecutionError as error:
            if error.kind is ProviderFailureKind.CONFIGURATION:
                raise WorkflowConfigurationError(
                    error.code,
                    "provider configuration or request was rejected",
                ) from None
            if error.kind is ProviderFailureKind.OUTPUT:
                raise WorkflowValidationError(
                    error.code,
                    "provider output failed validation",
                ) from None
            raise WorkflowTransientError(error.code, "provider execution failed") from None
        except (PipelineValidationError, SourceValidationError, ValueError) as error:
            raise WorkflowValidationError(
                _error_code(error),
                "product validation failed",
            ) from None
        except OSError as error:
            raise WorkflowTransientError("PROCESSING_IO_FAILURE", str(error)) from error

        changeset_id = validate_changeset_binding(
            self.paths,
            product,
            request.product_key,
        )
        if changeset_id != requested_changeset:
            raise WorkflowSecurityError(
                "PROCESSING_CHANGESET_BINDING_CHANGED",
                "processing changed the product changeset binding",
            )
        if (
            self._require_expected_head(request, self.github.get_pr(request.pr_number))
            != processing_head
        ):
            raise WorkflowSecurityError(
                "PROCESSING_HEAD_CHANGED",
                "processing branch changed while the provider was running",
            )
        mutations: list[MutationRecord] = []
        current_head = processing_head
        try:
            if request.allow_writeback:
                commit = self.git.commit_allowed_paths(
                    request.product_key,
                    f"data({request.product_key}): generate validated Ossie artifacts",
                )
                current_head = commit.sha
                mutations.append(
                    MutationRecord(
                        resource="commit",
                        target=commit.sha,
                        action="create" if commit.created else "noop",
                    )
                )
                if commit.created:
                    self.git.push(request.branch, lfs=True)
                remote_head = self.git.remote_branch_sha(request.branch)
                if remote_head != current_head:
                    raise WorkflowSecurityError(
                        "PROCESSING_REMOTE_HEAD_MISMATCH",
                        "remote branch does not match the processing commit",
                    )
            changeset_state = "pending" if changeset_id else "success"
            changeset_description = (
                f"Waiting for {changeset_id}"
                if changeset_id
                else "No shared-table changeset required"
            )
            mutations.append(
                self.github.set_status(
                    current_head,
                    "ard/changeset",
                    changeset_state,
                    changeset_description,
                    request.target_url,
                )
            )
            mutations.append(
                self.github.set_status(
                    current_head,
                    "ard/quality-gate",
                    "success",
                    "ARD validation passed",
                    request.target_url,
                )
            )
            if changeset_id:
                mutations.append(
                    self.github.dispatch_workflow(
                        "ard-changeset.yml",
                        pull_request.base_branch,
                        {
                            "changeset_id": changeset_id,
                            "head_sha": current_head,
                            "mode": "ready",
                            "pr_number": str(request.pr_number),
                            "product_id": processed.product_id,
                            "version": str(processed.product_version),
                        },
                    )
                )
        except WorkflowPartialError:
            raise
        except WorkflowError as error:
            if not any(item.resource == "commit" for item in mutations):
                raise
            raise WorkflowPartialError(
                "PROCESSING_POST_COMMIT_FAILED",
                "processing commit succeeded but publication did not converge",
                retryable=error.retryable,
                outputs={
                    "current_head": current_head,
                    "expected_head": request.expected_head,
                    "product_id": processed.product_id,
                    "product_key": request.product_key,
                    "version": processed.product_version,
                    **({"changeset_id": changeset_id} if changeset_id else {}),
                },
                artifacts=_artifact_paths(self.paths.root, product),
                mutations=mutations,
            ) from None

        outputs: dict[str, object] = {
            "current_head": current_head,
            "expected_head": request.expected_head,
            "product_id": processed.product_id,
            "product_key": request.product_key,
            "version": processed.product_version,
        }
        if changeset_id:
            outputs["changeset_id"] = changeset_id
        return WorkflowResult(
            command="workflow.process",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
            artifacts=_artifact_paths(self.paths.root, product),
            mutations=mutations,
        )

    def _require_expected_head(
        self,
        request: ProcessingRequest,
        pull_request: PullRequestState,
    ) -> str:
        current = self.git.current_sha()
        remote = self.git.remote_branch_sha(request.branch)
        if (
            current != request.expected_head
            or remote != request.expected_head
            or pull_request.head_sha != request.expected_head
            or pull_request.head_branch != request.branch
            or not pull_request.draft
        ):
            raise WorkflowSecurityError(
                "PROCESSING_HEAD_MISMATCH",
                "checkout, remote branch, and pull request head must match the validated SHA",
            )
        return current


class ProcessingReconcileService:
    def __init__(
        self,
        paths: FileSystemPort,
        git: GitPort,
        github: GitHubPort,
        *,
        retry_attempts: int = 4,
        retry_delay_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")
        self.paths = paths
        self.git = git
        self.github = github
        self.retry_attempts = retry_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.sleeper = sleeper

    def run(self, request: ProcessingReconcileRequest) -> WorkflowResult:
        if request.repository.expanduser().resolve() != self.paths.root:
            raise WorkflowSecurityError(
                "PROCESSING_RECONCILE_REPOSITORY_MISMATCH",
                "reconciliation repository does not match filesystem port",
            )
        prior = _load_process_result(self.paths, request.result_path)
        if prior.outputs.get("invocation_id") != request.invocation_id:
            raise WorkflowSecurityError(
                "PROCESSING_RECONCILE_INVOCATION_MISMATCH",
                "process result was not produced by this workflow invocation",
            )
        codes = [item.get("code") for item in prior.findings]
        if "PROCESSING_POST_COMMIT_FAILED" not in codes:
            raise _recorded_process_failure(prior)
        if codes != ["PROCESSING_POST_COMMIT_FAILED"] or prior.outputs.get(
            "failure_exit_code"
        ) != int(ExitCode.PARTIAL):
            raise WorkflowSecurityError(
                "PROCESSING_RECONCILE_RESULT_INVALID",
                "partial result has an invalid failure shape",
            )
        current_head = _required_output(prior, "current_head")
        expected_head = _required_output(prior, "expected_head")
        product_id = _required_output(prior, "product_id")
        product_key = _required_output(prior, "product_key")
        version = prior.outputs.get("version")
        if not isinstance(version, int) or version < 1:
            raise WorkflowSecurityError(
                "PROCESSING_RECONCILE_RESULT_INVALID",
                "partial result has an invalid product version",
            )
        pull_request = self._retry(lambda: self.github.get_pr(request.pr_number))
        if (
            self.git.current_sha() != current_head
            or self.git.remote_branch_sha(request.branch) != current_head
            or pull_request.head_sha != current_head
            or pull_request.head_branch != request.branch
            or not pull_request.draft
        ):
            raise WorkflowSecurityError(
                "PROCESSING_RECONCILE_HEAD_MISMATCH",
                "partial result is not tied to the current PR head",
            )
        commit_records = [
            item
            for item in prior.mutations
            if item.resource == "commit" and item.target == current_head
        ]
        if not commit_records or (
            current_head != expected_head
            and not any(item.action == "create" for item in commit_records)
        ):
            raise WorkflowSecurityError(
                "PROCESSING_RECONCILE_COMMIT_MISSING",
                "partial result does not journal the processing commit",
            )

        changeset_id = prior.outputs.get("changeset_id")
        if changeset_id is not None and not isinstance(changeset_id, str):
            raise WorkflowSecurityError(
                "PROCESSING_RECONCILE_RESULT_INVALID",
                "partial result has an invalid changeset",
            )
        mutations: list[MutationRecord] = []
        desired_changeset = "pending" if changeset_id else "success"
        changeset_mutation = self._ensure_status(
            current_head,
            "ard/changeset",
            desired_changeset,
            (
                f"Waiting for {changeset_id}"
                if changeset_id
                else "No shared-table changeset required"
            ),
            request.target_url,
        )
        if changeset_mutation is not None:
            mutations.append(changeset_mutation)
        quality_mutation = self._ensure_status(
            current_head,
            "ard/quality-gate",
            "success",
            "ARD validation passed",
            request.target_url,
        )
        if quality_mutation is not None:
            mutations.append(quality_mutation)
        if changeset_id:
            mutations.append(
                self._retry(
                    lambda: self.github.dispatch_workflow(
                        "ard-changeset.yml",
                        pull_request.base_branch,
                        {
                            "changeset_id": changeset_id,
                            "head_sha": current_head,
                            "mode": "ready",
                            "pr_number": str(request.pr_number),
                            "product_id": product_id,
                            "version": str(version),
                        },
                    )
                )
            )
        outputs = {
            "invocation_id": request.invocation_id,
            "current_head": current_head,
            "expected_head": expected_head,
            "product_id": product_id,
            "product_key": product_key,
            "version": version,
        }
        if changeset_id:
            outputs["changeset_id"] = changeset_id
        return WorkflowResult(
            command="workflow.process-reconcile",
            status=WorkflowStatus.SUCCESS,
            outputs=outputs,
            artifacts=prior.artifacts,
            mutations=mutations,
        )

    def _ensure_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ) -> MutationRecord | None:
        if self._retry(lambda: self.github.get_status(sha, context)) == state:
            return None
        return self._retry(
            lambda: self.github.set_status(
                sha,
                context,
                state,
                description,
                target_url,
            )
        )

    def _retry(self, operation: Callable[[], _RetryResult]) -> _RetryResult:
        for attempt in range(self.retry_attempts):
            try:
                return operation()
            except WorkflowTransientError:
                if attempt + 1 == self.retry_attempts:
                    raise
                self.sleeper(self.retry_delay_seconds * (2**attempt))
        raise AssertionError("retry loop exhausted without returning")


def provider_from_environment(
    *,
    registry=None,
    environment: Mapping[str, str] | None = None,
    factory=None,
):
    from ard_ossie.llm import (
        LLMProfileRegistry,
        LLMProviderFactory,
        LLMService,
    )

    active_environment = environment if environment is not None else os.environ
    profile_name = active_environment.get("ARD_LLM_PROFILE")
    if not profile_name:
        return None
    active_registry = registry or LLMProfileRegistry.load_packaged()
    active_factory = factory or LLMProviderFactory()
    profile = active_registry.resolve(profile_name)
    provider = active_factory.create(profile_name, profile, active_environment)
    return LLMService(provider)


def _validated_branch(value: str) -> str:
    if (
        not _BRANCH.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValueError("INVALID_BRANCH")
    return value


def validate_processing_invocation_id(value: str) -> str:
    if _INVOCATION_ID.fullmatch(value) is None:
        raise ValueError("INVALID_PROCESSING_INVOCATION_ID")
    return value


def _load_process_result(paths: FileSystemPort, value: Path) -> WorkflowResult:
    supplied = value if value.is_absolute() else paths.root / value
    expected = paths.root / ".ard" / "run" / "workflow.process-result.json"
    if supplied.absolute() != expected:
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_PATH_NOT_TRUSTED",
            "reconciliation requires the current process result envelope",
        )
    try:
        result = WorkflowResult.model_validate_json(
            paths.resolve_read(value).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, ValidationError):
        raise WorkflowValidationError(
            "PROCESSING_RECONCILE_RESULT_INVALID",
            "partial process result is malformed",
        ) from None
    if result.command != "workflow.process" or result.status is not WorkflowStatus.FAILURE:
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_NOT_PARTIAL",
            "result does not describe a failed process command",
        )
    return result


def _recorded_process_failure(result: WorkflowResult) -> WorkflowError:
    if len(result.findings) != 1:
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_INVALID",
            "recorded process failure must contain exactly one finding",
        )
    code = result.findings[0].get("code")
    if not isinstance(code, str) or _WORKFLOW_ERROR_CODE.fullmatch(code) is None:
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_INVALID",
            "recorded process failure has an invalid code",
        )
    raw_exit_code = result.outputs.get("failure_exit_code")
    if isinstance(raw_exit_code, bool) or not isinstance(raw_exit_code, int):
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_INVALID",
            "recorded process failure is missing its exit code",
        )
    try:
        exit_code = ExitCode(raw_exit_code)
    except ValueError:
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_INVALID",
            "recorded process failure has an unknown exit code",
        ) from None
    if exit_code in {ExitCode.SUCCESS, ExitCode.PARTIAL} or (
        result.retryable != (exit_code is ExitCode.TRANSIENT)
    ):
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_INVALID",
            "recorded process failure has inconsistent retry metadata",
        )
    return WorkflowError(
        code,
        "recorded process failure",
        exit_code,
        retryable=result.retryable,
    )


def _required_output(result: WorkflowResult, key: str) -> str:
    value = result.outputs.get(key)
    if not isinstance(value, str) or not value:
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_INVALID",
            f"partial result is missing {key}",
        )
    if key.endswith("head") and re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise WorkflowSecurityError(
            "PROCESSING_RECONCILE_RESULT_INVALID",
            f"partial result has an invalid {key}",
        )
    return value


def _artifact_paths(repository: Path, product: Path) -> list[str]:
    artifacts: list[str] = []
    for directory in (product / "generated", product / "quality"):
        if directory.is_dir():
            artifacts.extend(
                path.relative_to(repository).as_posix()
                for path in sorted(directory.rglob("*"))
                if path.is_file()
            )
    return artifacts


def _error_code(error: Exception) -> str:
    return str(error).partition(":")[0] or type(error).__name__
