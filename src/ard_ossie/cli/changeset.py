from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.impact import build_changeset
from ard_ossie.registry import Registry

app = typer.Typer(no_args_is_help=True)


@app.command("create")
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


@app.command("ready")
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
    record.mark_ready(product_id, version=version, pr_number=pr_number, head_sha=head_sha)
    loaded.write_changeset(record)
    typer.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))
