from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.identity import DuplicateDecision, classify_product, classify_table
from ard_ossie.models import CandidateChange
from ard_ossie.registry import Registry
from ard_ossie.versioning import VersionOutcome, plan_version

app = typer.Typer(no_args_is_help=True)


@app.command("check")
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
    existing_product = loaded.get_product(change.product.product_id)
    version_reports = [
        plan_version(
            current_version=existing_product.version if existing_product else None,
            changed=(
                existing_product is None
                or existing_product.canonical_hash != change.product.canonical_hash
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
