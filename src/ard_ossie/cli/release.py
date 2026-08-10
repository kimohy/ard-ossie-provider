from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.cli.common import write_json
from ard_ossie.release import build_release_bundle, resolve_release_plan

app = typer.Typer(no_args_is_help=True)


@app.command("plan")
def release_plan(
    product_id: str,
    registry: Annotated[Path, typer.Option("--registry")],
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    plan = resolve_release_plan(product_id, registry_root=registry, repository_root=repository)
    typer.echo(json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True))


@app.command("build")
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
    write_json(output / "release-plan.json", plan.model_dump(mode="json"))
    typer.echo(json.dumps({"bundle": str(bundle), **plan.model_dump(mode="json")}))
