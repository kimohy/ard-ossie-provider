from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.application.contracts import (
    ExitCode,
    WorkflowError,
    WorkflowResult,
    WorkflowStatus,
)
from ard_ossie.application.modeling import ModelingService
from ard_ossie.cli.execution import result_writer

app = typer.Typer(no_args_is_help=True)


@app.callback()
def validate_group() -> None:
    """Validate ARD products without mutating source state."""


@app.command("product")
def validate_product(
    product_path: Path,
    registry: Annotated[Path, typer.Option("--registry")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    from ard_ossie.adapters.filesystem import RepositoryPaths

    command = "validate.product"
    paths = RepositoryPaths(repository)
    writer = result_writer(paths.root, command)
    try:
        result = ModelingService(paths).validate(product_path, registry)
        writer.write(
            WorkflowResult(
                command=command,
                status=WorkflowStatus.SUCCESS if result.passed else WorkflowStatus.FAILURE,
                outputs={"valid": result.passed},
                findings=[item.model_dump(mode="json") for item in result.findings],
            )
        )
        typer.echo(result.model_dump_json())
        if not result.passed:
            raise typer.Exit(int(ExitCode.VALIDATION))
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
