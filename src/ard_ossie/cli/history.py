from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Annotated

import typer


def history(product_key: str) -> None:
    _run_git(["log", "--oneline", "--", f"products/{product_key}"])


def show(
    reference: str,
    registry: Annotated[Path, typer.Option("--registry")] = Path("registry"),
) -> None:
    product_key, version = _parse_ref(reference)
    tag = _product_tag(product_key, version, registry)
    _run_git(["show", f"{tag}:products/{product_key}/generated/ossie-model.json"])


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
    _run_git(["diff", left_tag, right_tag, "--", f"products/{product_key}"])


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
