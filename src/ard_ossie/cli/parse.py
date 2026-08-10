from __future__ import annotations

import json
import tempfile
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
from ard_ossie.application.parsing import ParsedDocumentResult, ParsingService
from ard_ossie.cli.execution import result_writer
from ard_ossie.ingestion import SourceValidationError

app = typer.Typer(no_args_is_help=True)


@app.callback()
def parse_group() -> None:
    """Parse ARD source documents into deterministic intermediate forms."""


@app.command("product-html")
def parse_product_html(
    source: Path,
    output: Annotated[Path, typer.Option("--output")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
    evidence: Annotated[Path | None, typer.Option("--evidence")] = None,
) -> None:
    _document_command(
        "parse.product-html",
        source,
        output,
        repository,
        evidence,
        lambda service, path: service.parse_product_html(path),
    )


@app.command("product")
def parse_product(
    source: Path,
    output: Annotated[Path, typer.Option("--output")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
    evidence: Annotated[Path | None, typer.Option("--evidence")] = None,
) -> None:
    _document_command(
        "parse.product",
        source,
        output,
        repository,
        evidence,
        lambda service, path: service.parse_product_html(path),
    )


@app.command("semantic")
def parse_semantic(
    source: Path,
    output: Annotated[Path, typer.Option("--output")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
    evidence: Annotated[Path | None, typer.Option("--evidence")] = None,
) -> None:
    _document_command(
        "parse.semantic",
        source,
        output,
        repository,
        evidence,
        lambda service, path: service.parse_semantic_document(path),
    )


@app.command("dictionary")
def parse_dictionary_command(
    source: Path,
    output: Annotated[Path, typer.Option("--output")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    command = "parse.dictionary"
    paths, writer = _execution(repository, command)
    try:
        result = ParsingService(paths).parse_dictionary_workbook(source)
        output_path = paths.resolve_write(output)
        _write_atomic(output_path, result.model_dump_json(indent=2) + "\n")
        relative_output = output_path.relative_to(paths.root).as_posix()
        writer.write(
            WorkflowResult(
                command=command,
                status=WorkflowStatus.SUCCESS,
                outputs={"output": relative_output, "source_hash": result.source_hash},
                artifacts=[relative_output],
            )
        )
        typer.echo(result.model_dump_json())
    except (WorkflowError, SourceValidationError, ValueError) as error:
        _fail(writer, command, error)


def _document_command(
    command: str,
    source: Path,
    output: Path,
    repository: Path,
    evidence: Path | None,
    parse: Callable[[ParsingService, Path], ParsedDocumentResult],
) -> None:
    paths, writer = _execution(repository, command)
    try:
        result = parse(ParsingService(paths), source)
        output_path = paths.resolve_write(output)
        evidence_path = paths.resolve_write(
            evidence or output.with_name(f"{output.name}.evidence.json")
        )
        _write_atomic(output_path, result.markdown)
        _write_atomic(
            evidence_path,
            json.dumps(
                [item.model_dump(mode="json") for item in result.evidence],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        artifacts = [
            output_path.relative_to(paths.root).as_posix(),
            evidence_path.relative_to(paths.root).as_posix(),
        ]
        writer.write(
            WorkflowResult(
                command=command,
                status=WorkflowStatus.SUCCESS,
                outputs={"output": artifacts[0], "source_hash": result.source_hash},
                artifacts=artifacts,
            )
        )
        typer.echo(result.model_dump_json())
    except (WorkflowError, SourceValidationError, ValueError) as error:
        _fail(writer, command, error)


def _execution(repository: Path, command: str):
    from ard_ossie.adapters.filesystem import RepositoryPaths

    paths = RepositoryPaths(repository)
    return paths, result_writer(paths.root, command)


def _fail(writer, command: str, error: Exception) -> None:
    if isinstance(error, WorkflowError):
        exit_code = error.exit_code
        code = error.code
    else:
        wrapped = WorkflowValidationError(str(error).partition(":")[0], "source validation failed")
        exit_code = wrapped.exit_code
        code = wrapped.code
    writer.write(
        WorkflowResult(
            command=command,
            status=WorkflowStatus.FAILURE,
            findings=[{"code": code, "message": code}],
        )
    )
    typer.echo(code, err=True)
    raise typer.Exit(int(exit_code)) from error


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
