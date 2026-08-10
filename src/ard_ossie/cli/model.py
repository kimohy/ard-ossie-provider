from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.application.contracts import (
    WorkflowConfigurationError,
    WorkflowError,
    WorkflowResult,
    WorkflowStatus,
)
from ard_ossie.application.modeling import ModelingService
from ard_ossie.cli.execution import result_writer

app = typer.Typer(no_args_is_help=True)


@app.callback()
def model_group() -> None:
    """Build staged ARD model artifacts."""


@app.command("build")
def model_build(
    product_path: Path,
    registry: Annotated[Path, typer.Option("--registry")],
    staging_output: Annotated[Path, typer.Option("--staging-output")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
    no_llm: Annotated[bool, typer.Option("--no-llm/--llm")] = True,
) -> None:
    from ard_ossie.adapters.filesystem import RepositoryPaths

    command = "model.build"
    paths = RepositoryPaths(repository)
    writer = result_writer(paths.root, command)
    try:
        if not no_llm:
            raise WorkflowConfigurationError(
                "MODEL_BUILD_LLM_NOT_SUPPORTED",
                "granular model build is deterministic and does not load LLM credentials",
            )
        result = ModelingService(paths).build(product_path, registry, staging_output)
        output = paths.resolve_write(staging_output).relative_to(paths.root).as_posix()
        artifacts = [f"{output}/{name}" for name in result.generated_files]
        writer.write(
            WorkflowResult(
                command=command,
                status=WorkflowStatus.SUCCESS,
                outputs={"output": output},
                artifacts=artifacts,
            )
        )
        typer.echo(result.model_dump_json())
    except WorkflowError as error:
        writer.write(
            WorkflowResult(
                command=command,
                status=WorkflowStatus.FAILURE,
                findings=[{"code": error.code, "message": error.code}],
            )
        )
        typer.echo(error.code, err=True)
        raise typer.Exit(int(error.exit_code)) from error
