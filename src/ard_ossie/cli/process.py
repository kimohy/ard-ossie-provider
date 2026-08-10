from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.application.processing import provider_from_environment
from ard_ossie.cli.common import write_json
from ard_ossie.ingestion import SourceValidationError
from ard_ossie.pipeline import (
    PipelineValidationError,
    ProviderExecutionError,
    QualityFinding,
    QualityReport,
    QualityStatus,
    process_product,
)


def process(
    product_path: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    registry: Annotated[Path, typer.Option("--registry")],
    report: Annotated[Path | None, typer.Option("--report")] = None,
    pr_number: Annotated[int | None, typer.Option("--pr-number", min=1)] = None,
    warnings_as_errors: Annotated[bool, typer.Option("--warnings-as-errors")] = False,
) -> None:
    try:
        result = process_product(
            product_path,
            registry_root=registry,
            provider=_provider_from_environment(),
            pr_number=pr_number,
        )
    except (PipelineValidationError, SourceValidationError, ValueError) as error:
        _write_failure_report(product_path, report, error)
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from error
    except ProviderExecutionError as error:
        _write_failure_report(product_path, report, error)
        typer.echo(str(error), err=True)
        raise typer.Exit(3) from error
    if report is not None:
        write_json(report, result.quality_report.model_dump(mode="json"))
    typer.echo(
        json.dumps(
            {
                "product_id": result.product_id,
                "version": f"v{result.product_version}",
                "status": result.quality_report.status,
            }
        )
    )
    if warnings_as_errors and result.quality_report.warnings:
        raise typer.Exit(1)


def _provider_from_environment():
    return provider_from_environment()


def _write_failure_report(
    product_path: Path,
    report_path: Path | None,
    error: Exception,
) -> None:
    message = str(error)
    detailed = error.report if isinstance(error, PipelineValidationError) else None
    report = detailed or QualityReport(
        status=QualityStatus.FAIL,
        completeness=0,
        hard_errors=[QualityFinding(code=message.partition(":")[0], message=message)],
    )
    default_path = product_path / "quality" / "quality-report.json"
    write_json(default_path, report.model_dump(mode="json"))
    if report_path is not None and report_path.resolve() != default_path.resolve():
        write_json(report_path, report.model_dump(mode="json"))
