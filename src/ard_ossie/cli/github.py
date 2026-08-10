from __future__ import annotations

import getpass
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.application.contracts import (
    WorkflowError,
    WorkflowResult,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.application.github_bootstrap import (
    BootstrapConfig,
    BootstrapPlan,
    GitHubBootstrapService,
)
from ard_ossie.cli.execution import result_writer

app = typer.Typer(no_args_is_help=True)
_prompt: Callable[..., str] = typer.prompt
_confirm: Callable[..., bool] = typer.confirm
_getpass: Callable[[str], str] = getpass.getpass


@app.callback()
def github_group() -> None:
    """Reconcile GitHub repository resources."""


@app.command("bootstrap")
def bootstrap(
    repo: Annotated[str, typer.Option("--repo")],
    base_url: Annotated[str, typer.Option("--base-url")] = "https://api.openai.com/v1",
    model: Annotated[str | None, typer.Option("--model")] = None,
    api_style: Annotated[str, typer.Option("--api-style")] = "chat_completions",
    max_attachment_bytes: Annotated[
        int,
        typer.Option("--max-attachment-bytes", min=1, max=1_073_741_824),
    ] = 52_428_800,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    command = "github.bootstrap"

    def run() -> WorkflowResult:
        config = BootstrapConfig(
            base_url=base_url,
            model=model or _prompt("ARD_LLM_MODEL"),
            api_style=api_style,
            max_attachment_bytes=max_attachment_bytes,
        )
        service = _bootstrap_service(repo)
        plan = service.plan(config)
        typer.echo(plan.model_dump_json(indent=2))
        if dry_run:
            return _plan_result(plan)
        if not _confirm(f"Apply this plan to {plan.repository}?", default=False):
            return WorkflowResult(
                command=command,
                status=WorkflowStatus.NOOP,
                outputs={"repository": plan.repository, "confirmed": False},
            )
        replace_secret = False
        if plan.secret_present:
            replace_secret = _confirm(
                "Replace existing ard-llm ARD_LLM_API_KEY?",
                default=False,
            )
        needs_secret = not plan.secret_present or replace_secret
        provider = (lambda: _getpass("ARD_LLM_API_KEY: ")) if needs_secret else None
        return service.apply(
            plan,
            api_key_provider=provider,
            replace_secret=replace_secret,
        )

    _publish(command, run)


@app.command("enable-review-protection")
def enable_review_protection(
    repo: Annotated[str, typer.Option("--repo")],
) -> None:
    command = "github.enable-review-protection"
    _publish(command, lambda: _bootstrap_service(repo).enable_review_protection())


def _bootstrap_service(repository: str) -> GitHubBootstrapService:
    from ard_ossie.adapters.github_cli import GitHubCli
    from ard_ossie.adapters.subprocess import SubprocessRunner

    return GitHubBootstrapService(repository, GitHubCli(repository, SubprocessRunner()))


def _plan_result(plan: BootstrapPlan) -> WorkflowResult:
    return WorkflowResult(
        command="github.bootstrap",
        status=WorkflowStatus.NOOP,
        outputs={
            "repository": plan.repository,
            "dry_run": True,
            "items": [item.model_dump(mode="json") for item in plan.items],
            "secret": "present" if plan.secret_present else "missing",
        },
    )


def _publish(command: str, run: Callable[[], WorkflowResult]) -> None:
    writer = result_writer(Path.cwd(), command)
    try:
        result = run()
    except WorkflowError as error:
        result = WorkflowResult(
            command=command,
            status=WorkflowStatus.FAILURE,
            outputs=getattr(error, "outputs", {}),
            findings=[{"code": error.code, "message": error.code}],
            mutations=getattr(error, "mutations", []),
            retryable=error.retryable,
        )
        writer.write(result)
        typer.echo(error.code, err=True)
        raise typer.Exit(int(error.exit_code)) from error
    except (TypeError, ValueError) as error:
        wrapped = WorkflowValidationError(
            str(error).partition(":")[0] or type(error).__name__,
            "GitHub bootstrap validation failed",
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
