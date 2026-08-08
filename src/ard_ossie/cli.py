from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from pydantic import SecretStr

from ard_ossie.identity import DuplicateDecision, classify_product, classify_table
from ard_ossie.impact import analyze_table_change, build_changeset
from ard_ossie.ingestion import SourceValidationError
from ard_ossie.llm import OpenAICompatibleProvider
from ard_ossie.models import CandidateChange
from ard_ossie.pipeline import (
    PipelineValidationError,
    ProviderExecutionError,
    QualityFinding,
    QualityReport,
    QualityStatus,
    process_product,
)
from ard_ossie.registry import Registry
from ard_ossie.release import build_release_bundle, resolve_release_plan
from ard_ossie.versioning import VersionOutcome, plan_version

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
registry_app = typer.Typer(no_args_is_help=True)
impact_app = typer.Typer(no_args_is_help=True)
release_app = typer.Typer(no_args_is_help=True)
changeset_app = typer.Typer(no_args_is_help=True)
app.add_typer(registry_app, name="registry")
app.add_typer(impact_app, name="impact")
app.add_typer(release_app, name="release")
app.add_typer(changeset_app, name="changeset")


@app.command()
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
        _write_json(report, result.quality_report.model_dump(mode="json"))
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


@registry_app.command("check")
def registry_check(
    candidate: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    registry: Annotated[Path, typer.Option("--registry")],
) -> None:
    loaded = Registry.load(registry)
    change = CandidateChange.model_validate_json(candidate.read_text(encoding="utf-8"))
    reports = [
        classify_product(change.product, loaded.products(), operation=change.operation),
        *(classify_table(table, loaded.tables()) for table in change.tables),
    ]
    version_reports = [
        plan_version(
            current_version=(
                loaded.get_product(change.product.product_id).version
                if loaded.get_product(change.product.product_id)
                else None
            ),
            changed=(
                loaded.get_product(change.product.product_id) is None
                or loaded.get_product(change.product.product_id).canonical_hash
                != change.product.canonical_hash
            ),
            base_version=change.base_product_version,
            proposed_version=change.proposed_product_version or change.product.version,
        )
    ]
    for table in change.tables:
        existing = loaded.get_table(table.table_id)
        version_reports.append(
            plan_version(
                current_version=existing.version if existing else None,
                changed=existing is None or existing.canonical_hash != table.canonical_hash,
                base_version=change.base_table_versions.get(table.table_id),
                proposed_version=change.proposed_table_versions.get(table.table_id, table.version),
            )
        )
    typer.echo(
        json.dumps(
            {
                "duplicates": [report.model_dump(mode="json") for report in reports],
                "versions": [report.model_dump(mode="json") for report in version_reports],
            },
            indent=2,
            sort_keys=True,
        )
    )
    if any(report.decision is DuplicateDecision.BLOCK for report in reports) or any(
        report.outcome is VersionOutcome.BLOCK for report in version_reports
    ):
        raise typer.Exit(2)


@impact_app.command("table")
def impact_table(
    table_id: str,
    registry: Annotated[Path, typer.Option("--registry")],
) -> None:
    loaded = Registry.load(registry)
    report = analyze_table_change(table_id, loaded.mappings())
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))


@changeset_app.command("create")
def changeset_create(
    table_id: Annotated[list[str], typer.Option("--table-id")],
    product_id: Annotated[list[str], typer.Option("--product-id")],
    registry: Annotated[Path, typer.Option("--registry")],
    changeset_id: Annotated[str | None, typer.Option("--changeset-id")] = None,
) -> None:
    loaded = Registry.load(registry)
    existing = loaded.get_changeset(changeset_id) if changeset_id else None
    if existing is None:
        record = build_changeset(table_id, product_id, changeset_id=changeset_id)
    else:
        required = sorted(set(product_id))
        record = existing.model_copy(
            update={
                "table_ids": sorted(set(table_id)),
                "required_product_ids": required,
                "ready_products": {
                    key: value for key, value in existing.ready_products.items() if key in required
                },
            }
        )
    loaded.write_changeset(record)
    typer.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@changeset_app.command("ready")
def changeset_ready(
    changeset_id: str,
    product_id: str,
    version: Annotated[int, typer.Option("--version", min=1, max=999)],
    pr_number: Annotated[int, typer.Option("--pr-number", min=1)],
    head_sha: Annotated[str, typer.Option("--head-sha")],
    registry: Annotated[Path, typer.Option("--registry")],
) -> None:
    loaded = Registry.load(registry)
    record = loaded.get_changeset(changeset_id)
    if record is None:
        typer.echo(f"CHANGESET_NOT_FOUND: {changeset_id}", err=True)
        raise typer.Exit(2)
    record.mark_ready(
        product_id,
        version=version,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    loaded.write_changeset(record)
    typer.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@release_app.command("plan")
def release_plan(
    product_id: str,
    registry: Annotated[Path, typer.Option("--registry")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    plan = resolve_release_plan(product_id, registry_root=registry, repository_root=repository)
    typer.echo(json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True))


@release_app.command("build")
def release_build(
    product_id: str,
    registry: Annotated[Path, typer.Option("--registry")],
    output: Annotated[Path, typer.Option("--output")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
    table_id: Annotated[list[str] | None, typer.Option("--table-id")] = None,
) -> None:
    plan = resolve_release_plan(
        product_id,
        registry_root=registry,
        repository_root=repository,
        table_ids=set(table_id or []),
    )
    output.mkdir(parents=True, exist_ok=True)
    bundle = build_release_bundle(
        repository / "products" / plan.product_key,
        output / f"{plan.product_id}-v{plan.product_version}.zip",
    )
    _write_json(output / "release-plan.json", plan.model_dump(mode="json"))
    typer.echo(json.dumps({"bundle": str(bundle), **plan.model_dump(mode="json")}))


@app.command()
def history(product_key: str) -> None:
    _run_git(["log", "--oneline", "--", f"products/{product_key}"])


@app.command()
def show(
    reference: str,
    registry: Annotated[Path, typer.Option("--registry")] = Path("registry"),
) -> None:
    product_key, version = _parse_ref(reference)
    tag = _product_tag(product_key, version, registry)
    _run_git(
        [
            "show",
            f"{tag}:products/{product_key}/generated/ossie-model.json",
        ]
    )


@app.command("diff")
def diff_command(
    range_expression: str,
    registry: Annotated[Path, typer.Option("--registry")] = Path("registry"),
) -> None:
    left, separator, right = range_expression.partition("..")
    if not separator:
        raise typer.BadParameter("expected <product>@<version>..<version>")
    product_key, left_version = _parse_ref(left)
    right_version = int(right.removeprefix("v"))
    left_tag = _product_tag(product_key, left_version, registry)
    right_tag = _product_tag(product_key, right_version, registry)
    _run_git(
        [
            "diff",
            left_tag,
            right_tag,
            "--",
            f"products/{product_key}",
        ]
    )


def _provider_from_environment() -> OpenAICompatibleProvider | None:
    api_style = os.environ.get("ARD_LLM_API_STYLE", "chat_completions")
    if api_style != "chat_completions":
        raise ProviderExecutionError(f"LLM_API_STYLE_UNSUPPORTED: {api_style}")
    names = ("ARD_LLM_BASE_URL", "ARD_LLM_API_KEY", "ARD_LLM_MODEL")
    values = {name: os.environ.get(name) for name in names}
    present = [name for name, value in values.items() if value]
    if not present:
        return None
    if len(present) != len(names):
        missing = sorted(set(names) - set(present))
        raise ProviderExecutionError(f"LLM_PROVIDER_CONFIG_INCOMPLETE: {','.join(missing)}")
    return OpenAICompatibleProvider(
        base_url=values["ARD_LLM_BASE_URL"] or "",
        api_key=SecretStr(values["ARD_LLM_API_KEY"] or ""),
        model=values["ARD_LLM_MODEL"] or "",
    )


def _parse_ref(reference: str) -> tuple[str, int]:
    product_key, separator, version = reference.partition("@")
    if not separator:
        raise typer.BadParameter("expected <product>@<version>")
    return product_key, int(version.removeprefix("v"))


def _product_tag(product_key: str, version: int, registry: Path) -> str:
    index_path = registry / "indexes" / "product-keys.json"
    if not index_path.is_file():
        raise typer.BadParameter(f"registry product index not found: {index_path}")
    product_id = json.loads(index_path.read_text(encoding="utf-8")).get(product_key)
    if product_id is None:
        raise typer.BadParameter(f"product key not found: {product_key}")
    return f"product/{product_id}/v{version}"


def _run_git(arguments: list[str]) -> None:
    result = subprocess.run(["git", *arguments], check=False, text=True, capture_output=True)
    if result.returncode:
        typer.echo(result.stderr, err=True)
        raise typer.Exit(2)
    typer.echo(result.stdout, nl=False)


def _write_failure_report(product_path: Path, report_path: Path | None, error: Exception) -> None:
    message = str(error)
    detailed = error.report if isinstance(error, PipelineValidationError) else None
    report = detailed or QualityReport(
        status=QualityStatus.FAIL,
        completeness=0,
        hard_errors=[QualityFinding(code=message.partition(":")[0], message=message)],
    )
    default_path = product_path / "quality" / "quality-report.json"
    _write_json(default_path, report.model_dump(mode="json"))
    if report_path is not None and report_path.resolve() != default_path.resolve():
        _write_json(report_path, report.model_dump(mode="json"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
