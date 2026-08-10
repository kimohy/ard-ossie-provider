from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.impact import analyze_table_change
from ard_ossie.registry import Registry

app = typer.Typer(no_args_is_help=True)


@app.command("table")
def impact_table(
    table_id: str,
    registry: Annotated[Path, typer.Option("--registry")],
) -> None:
    loaded = Registry.load(registry)
    report = analyze_table_change(table_id, loaded.mappings())
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
