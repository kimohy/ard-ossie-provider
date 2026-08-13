from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Annotated

import typer

from ard_ossie.cli.common import write_json
from ard_ossie.release import (
    ReleaseBlocked,
    build_release_bundle,
    resolve_release_plan,
    verify_release_bundle,
)

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
    destination = output / f"{plan.product_id}-v{plan.product_version}.zip"
    temporary: Path | None = None
    promotion: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
        bundle = Path(
            build_release_bundle(
                repository / "products" / plan.product_key,
                temporary,
            )
        )
        try:
            bundle_status = bundle.lstat()
        except OSError as error:
            raise ReleaseBlocked("RELEASE_BUNDLE_OUTPUT_INVALID") from error
        if bundle != temporary or bundle.is_symlink() or not stat.S_ISREG(bundle_status.st_mode):
            raise ReleaseBlocked("RELEASE_BUNDLE_OUTPUT_MISMATCH")
        verified_payload = verify_release_bundle(bundle, plan.artifact_hashes)
        with tempfile.NamedTemporaryFile(
            dir=output,
            prefix=f".{destination.name}.",
            suffix=".verified",
            delete=False,
        ) as stream:
            promotion = Path(stream.name)
            stream.write(verified_payload)
            stream.flush()
            os.fsync(stream.fileno())
        promotion.replace(destination)
        promotion = None
        bundle = destination
    finally:
        if promotion is not None:
            _discard_release_temporary(promotion)
        if temporary is not None:
            _discard_release_temporary(temporary)
    write_json(output / "release-plan.json", plan.model_dump(mode="json"))
    typer.echo(json.dumps({"bundle": str(bundle), **plan.model_dump(mode="json")}))


def _discard_release_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except IsADirectoryError:
        with suppress(OSError):
            path.rmdir()
