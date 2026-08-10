from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.application.changesets import ChangesetRequest, ChangesetService
from ard_ossie.application.contracts import (
    WorkflowContext,
    WorkflowError,
    WorkflowResult,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.application.finalize import FinalizeRequest, FinalizeService
from ard_ossie.application.intake import IssueAuthorizationService, IssueIntakeService
from ard_ossie.application.processing import (
    ProcessingReconcileRequest,
    ProcessingReconcileService,
    ProcessingRequest,
    ProcessingService,
    provider_from_environment,
)
from ard_ossie.application.release_detection import (
    ReleaseDetectionRequest,
    ReleaseDetectionService,
)
from ard_ossie.application.release_dispatch import (
    ReleaseDispatchRequest,
    ReleaseDispatchService,
)
from ard_ossie.application.release_publication import (
    ReleasePublicationRequest,
    ReleasePublicationService,
)
from ard_ossie.application.repository_checks import (
    RepositoryCheckRequest,
    RepositoryCheckService,
    RepositoryVerificationTools,
)
from ard_ossie.application.source_check import (
    DetectProductService,
    EnsureProductPrService,
    SourceCheckService,
)
from ard_ossie.cli.execution import result_writer

app = typer.Typer(no_args_is_help=True)


@app.callback()
def workflow_group() -> None:
    """Run one idempotent GitHub workflow lifecycle."""


@app.command("issue-authorize")
def issue_authorize(
    event: Annotated[Path, typer.Option("--event")],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    actor: Annotated[str, typer.Option("--actor")],
    label: Annotated[str, typer.Option("--label")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.issue-authorize"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        context = _context(paths.root, event, repository_name, actor)
        return _issue_authorization_service(repository_name, paths).run(
            context,
            label=label,
            actor=actor,
        )

    _publish(paths.root, command, run)


@app.command("process-reconcile")
def process_reconcile_workflow(
    result_path: Annotated[Path, typer.Option("--result-path")],
    branch: Annotated[str, typer.Option("--branch")],
    pr_number: Annotated[int, typer.Option("--pr-number", min=1)],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    target_url: Annotated[str, typer.Option("--target-url")] = "",
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.process-reconcile"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        request = ProcessingReconcileRequest(
            repository=paths.root,
            result_path=result_path,
            branch=branch,
            pr_number=pr_number,
            target_url=target_url,
        )
        return _processing_reconcile_service(repository_name, paths).run(request)

    _publish(paths.root, command, run)


@app.command("issue-intake")
def issue_intake(
    event: Annotated[Path, typer.Option("--event")],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    actor: Annotated[str, typer.Option("--actor")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.issue-intake"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        context = _context(paths.root, event, repository_name, actor)
        return _issue_intake_service(repository_name, paths).run(context)

    _publish(paths.root, command, run)


@app.command("detect-product")
def detect_product(
    base_ref: Annotated[str, typer.Option("--base-ref")],
    head_ref: Annotated[str, typer.Option("--head-ref")] = "HEAD",
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.detect-product"
    paths = _repository_paths(repository)
    _publish(
        paths.root,
        command,
        lambda: _detect_product_service(paths).run(base_ref, head_ref),
    )


@app.command("source-check")
def source_check(
    product_key: Annotated[str, typer.Option("--product-key")],
    expected_head: Annotated[str, typer.Option("--expected-head")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.source-check"
    paths = _repository_paths(repository)
    _publish(
        paths.root,
        command,
        lambda: _source_check_service(paths).run(product_key, expected_head),
    )


@app.command("ensure-product-pr")
def ensure_product_pr(
    branch: Annotated[str, typer.Option("--branch")],
    product_key: Annotated[str, typer.Option("--product-key")],
    expected_head: Annotated[str, typer.Option("--expected-head")],
    base_branch: Annotated[str, typer.Option("--base-branch")],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.ensure-product-pr"
    paths = _repository_paths(repository)
    _publish(
        paths.root,
        command,
        lambda: _ensure_product_pr_service(repository_name, paths).run(
            branch,
            product_key,
            expected_head,
            base_branch=base_branch,
        ),
    )


@app.command("process")
def process_workflow(
    product_key: Annotated[str, typer.Option("--product-key")],
    branch: Annotated[str, typer.Option("--branch")],
    pr_number: Annotated[int, typer.Option("--pr-number", min=1)],
    expected_head: Annotated[str, typer.Option("--expected-head")],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    allow_writeback: Annotated[
        bool,
        typer.Option("--allow-writeback/--no-allow-writeback"),
    ] = True,
    warnings_as_errors: Annotated[bool, typer.Option("--warnings-as-errors")] = False,
    target_url: Annotated[str, typer.Option("--target-url")] = "",
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.process"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        request = ProcessingRequest(
            repository=paths.root,
            product_key=product_key,
            branch=branch,
            pr_number=pr_number,
            expected_head=expected_head,
            allow_writeback=allow_writeback,
            warnings_as_errors=warnings_as_errors,
            target_url=target_url,
        )
        return _processing_service(repository_name, paths).run(request)

    _publish(paths.root, command, run)


@app.command("changeset")
def changeset_workflow(
    mode: Annotated[str, typer.Option("--mode")],
    changeset_id: Annotated[str, typer.Option("--changeset-id")],
    base_branch: Annotated[str, typer.Option("--base-branch")],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    table_ids: Annotated[str, typer.Option("--table-ids")] = "",
    product_ids: Annotated[str, typer.Option("--product-ids")] = "",
    product_id: Annotated[str | None, typer.Option("--product-id")] = None,
    version: Annotated[int | None, typer.Option("--version", min=1, max=999)] = None,
    pr_number: Annotated[int | None, typer.Option("--pr-number", min=1)] = None,
    head_sha: Annotated[str | None, typer.Option("--head-sha")] = None,
    initiating_pr: Annotated[int | None, typer.Option("--initiating-pr", min=1)] = None,
    target_url: Annotated[str, typer.Option("--target-url")] = "",
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.changeset"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        request = ChangesetRequest(
            repository=paths.root,
            mode=mode,
            changeset_id=changeset_id,
            table_ids=_csv(table_ids),
            product_ids=_csv(product_ids),
            product_id=product_id,
            version=version,
            pr_number=pr_number,
            head_sha=head_sha,
            initiating_pr=initiating_pr,
            base_branch=base_branch,
            target_url=target_url,
        )
        return _changeset_service(repository_name, paths).run(request)

    _publish(paths.root, command, run)


@app.command("finalize")
def finalize_workflow(
    upstream_result: Annotated[str, typer.Option("--upstream-result")],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    result_path: Annotated[Path | None, typer.Option("--result-path")] = None,
    issue_number: Annotated[int | None, typer.Option("--issue-number", min=1)] = None,
    pr_number: Annotated[int | None, typer.Option("--pr-number", min=1)] = None,
    expected_head: Annotated[str | None, typer.Option("--expected-head")] = None,
    target_url: Annotated[str, typer.Option("--target-url")] = "",
    publish_success_statuses: Annotated[
        bool,
        typer.Option("--publish-success-statuses/--no-publish-success-statuses"),
    ] = False,
    authoritative_statuses: Annotated[
        bool,
        typer.Option("--authoritative-statuses/--no-authoritative-statuses"),
    ] = False,
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.finalize"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        request = FinalizeRequest(
            repository=paths.root,
            upstream_result=upstream_result,
            result_path=result_path,
            issue_number=issue_number,
            pr_number=pr_number,
            expected_head=expected_head,
            target_url=target_url,
            publish_success_statuses=publish_success_statuses,
            authoritative_statuses=authoritative_statuses,
        )
        return _finalize_service(repository_name, paths).run(request)

    _publish(paths.root, command, run)


@app.command("release-detect")
def release_detect_workflow(
    before: Annotated[str, typer.Option("--before")],
    current: Annotated[str, typer.Option("--current")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.release-detect"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        request = ReleaseDetectionRequest(
            repository=paths.root,
            before=before,
            current=current,
        )
        return _release_detection_service(paths).run(request)

    _publish(paths.root, command, run)


@app.command("release-product")
def release_product_workflow(
    product_key: Annotated[str, typer.Option("--product-key")],
    current: Annotated[str, typer.Option("--current")],
    table_ids: Annotated[str, typer.Option("--table-ids")],
    output: Annotated[Path, typer.Option("--output")],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.release-product"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        request = ReleasePublicationRequest(
            repository=paths.root,
            product_key=product_key,
            current=current,
            table_ids=_json_string_list(table_ids),
            output=output,
        )
        return _release_publication_service(repository_name, paths).run(request)

    _publish(paths.root, command, run)


@app.command("release-dispatch")
def release_dispatch_workflow(
    result_path: Annotated[Path, typer.Option("--result-path")],
    current: Annotated[str, typer.Option("--current")],
    repository_name: Annotated[str, typer.Option("--repository-name")],
    target_url: Annotated[str, typer.Option("--target-url")] = "",
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.release-dispatch"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        request = ReleaseDispatchRequest(
            repository=paths.root,
            result_path=result_path,
            current=current,
            target_url=target_url,
        )
        return _release_dispatch_service(repository_name, paths).run(request)

    _publish(paths.root, command, run)


@app.command("repository-check")
def repository_check_workflow(
    base_ref: Annotated[str, typer.Option("--base-ref")],
    head_ref: Annotated[str, typer.Option("--head-ref")],
    head_sha: Annotated[str, typer.Option("--head-sha")],
    verification_group: Annotated[
        str,
        typer.Option("--verification-group"),
    ],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "workflow.repository-check"
    paths = _repository_paths(repository)

    def run() -> WorkflowResult:
        request = RepositoryCheckRequest(
            repository=paths.root,
            base_ref=base_ref,
            head_ref=head_ref,
            head_sha=head_sha,
            verification_group=verification_group,
        )
        return _repository_check_service(paths).run(request)

    _publish(paths.root, command, run)


def _context(
    repository: Path,
    event: Path,
    repository_name: str,
    actor: str,
) -> WorkflowContext:
    return WorkflowContext(
        repository=repository,
        event_path=event,
        event_name="issues",
        repository_name=repository_name,
        actor=actor,
    )


def _publish(repository: Path, command: str, run: Callable[[], WorkflowResult]) -> None:
    writer = result_writer(repository, command)
    try:
        result = run()
    except WorkflowError as error:
        result = WorkflowResult(
            command=command,
            status=WorkflowStatus.FAILURE,
            outputs=getattr(error, "outputs", {}),
            artifacts=getattr(error, "artifacts", []),
            findings=[{"code": error.code, "message": error.code}],
            mutations=getattr(error, "mutations", []),
            retryable=error.retryable,
        )
        writer.write(result)
        typer.echo(error.code, err=True)
        raise typer.Exit(int(error.exit_code)) from error
    except (ValueError, TypeError) as error:
        wrapped = WorkflowValidationError(
            str(error).partition(":")[0] or type(error).__name__,
            "workflow command validation failed",
        )
        writer.write(
            WorkflowResult(
                command=command,
                status=WorkflowStatus.FAILURE,
                findings=[{"code": wrapped.code, "message": wrapped.code}],
            )
        )
        typer.echo(wrapped.code, err=True)
        raise typer.Exit(int(wrapped.exit_code)) from error
    writer.write(result)
    typer.echo(result.model_dump_json())


def _repository_paths(repository: Path):
    from ard_ossie.adapters.filesystem import RepositoryPaths

    return RepositoryPaths(repository)


def _issue_authorization_service(repository_name: str, paths):
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    github = GitHubCli(repository_name, SubprocessRunner(), paths=paths)
    return IssueAuthorizationService(github)


def _issue_intake_service(repository_name: str, paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    runner = SubprocessRunner()
    git = GitCli(paths.root, runner, paths=paths)
    github = GitHubCli(repository_name, runner, paths=paths)
    return IssueIntakeService(paths, git, github)


def _detect_product_service(paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    return DetectProductService(GitCli(paths.root, SubprocessRunner(), paths=paths))


def _source_check_service(paths):
    return SourceCheckService(paths)


def _ensure_product_pr_service(repository_name: str, paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    runner = SubprocessRunner()
    git = GitCli(paths.root, runner, paths=paths)
    github = GitHubCli(repository_name, runner, paths=paths)
    return EnsureProductPrService(git, github)


def _processing_service(repository_name: str, paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    runner = SubprocessRunner()
    git = GitCli(paths.root, runner, paths=paths)
    github = GitHubCli(repository_name, runner, paths=paths)
    return ProcessingService(
        paths,
        git,
        github,
        provider_factory=provider_from_environment,
    )


def _changeset_service(repository_name: str, paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    runner = SubprocessRunner()
    git = GitCli(paths.root, runner, paths=paths)
    github = GitHubCli(repository_name, runner, paths=paths)
    return ChangesetService(paths, git, github)


def _processing_reconcile_service(repository_name: str, paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    runner = SubprocessRunner()
    git = GitCli(paths.root, runner, paths=paths)
    github = GitHubCli(repository_name, runner, paths=paths)
    return ProcessingReconcileService(paths, git, github)


def _finalize_service(repository_name: str, paths):
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    github = GitHubCli(repository_name, SubprocessRunner(), paths=paths)
    return FinalizeService(paths, github)


def _release_detection_service(paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    return ReleaseDetectionService(paths, GitCli(paths.root, SubprocessRunner(), paths=paths))


def _release_publication_service(repository_name: str, paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    runner = SubprocessRunner()
    return ReleasePublicationService(
        paths,
        GitCli(paths.root, runner, paths=paths),
        GitHubCli(repository_name, runner, paths=paths),
    )


def _release_dispatch_service(repository_name: str, paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    runner = SubprocessRunner()
    return ReleaseDispatchService(
        paths,
        GitCli(paths.root, runner, paths=paths),
        GitHubCli(repository_name, runner, paths=paths),
    )


def _repository_check_service(paths):
    from ard_ossie.adapters.git_cli import GitCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    runner = SubprocessRunner()
    return RepositoryCheckService(
        paths,
        GitCli(paths.root, runner, paths=paths),
        RepositoryVerificationTools(paths, runner),
    )


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_string_list(value: str) -> list[str]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ValueError("JSON_STRING_LIST_REQUIRED")
    return decoded
