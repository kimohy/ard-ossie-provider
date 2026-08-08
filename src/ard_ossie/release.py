from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from ard_ossie.impact import ChangeSetRecord, ChangeSetStatus
from ard_ossie.models import ProductRecord, StrictModel, TableRecord
from ard_ossie.registry import Registry


class ReleaseBlocked(ValueError):
    pass


class ReleasePlan(StrictModel):
    product_id: str
    product_key: str
    product_version: int = Field(ge=1, le=999)
    product_tag: str
    table_tags: list[str]
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    changeset_id: str | None = None


_GENERATED_ASSETS = (
    "data-product.md",
    "data-semantic.md",
    "data-dictionary.json",
    "ossie-model.json",
    "source-manifest.json",
)
_QUALITY_ASSETS = (
    "quality-report.json",
    "duplicate-report.json",
    "version-report.json",
    "impact-report.json",
    "llm-suggestions.json",
)


def build_release_plan(
    product: ProductRecord,
    tables: list[TableRecord],
    *,
    changeset: ChangeSetRecord | None = None,
    quality_report: dict[str, Any] | None = None,
    artifact_hashes: dict[str, str] | None = None,
) -> ReleasePlan:
    if changeset is not None and changeset.status is not ChangeSetStatus.READY:
        missing = sorted(set(changeset.required_product_ids) - set(changeset.ready_products))
        raise ReleaseBlocked(f"CHANGESET_INCOMPLETE: {','.join(missing)}")
    if quality_report is not None and (
        quality_report.get("status") == "FAIL" or quality_report.get("hard_errors")
    ):
        raise ReleaseBlocked("QUALITY_GATE_FAILED")
    return ReleasePlan(
        product_id=product.product_id,
        product_key=product.product_key,
        product_version=product.version,
        product_tag=f"product/{product.product_id}/v{product.version}",
        table_tags=sorted(f"table/{table.table_id}/v{table.version}" for table in tables),
        artifact_hashes=artifact_hashes or {},
        changeset_id=changeset.changeset_id if changeset else None,
    )


def resolve_release_plan(
    identifier: str,
    *,
    registry_root: str | Path,
    repository_root: str | Path = ".",
    table_ids: set[str] | None = None,
) -> ReleasePlan:
    registry = Registry.load(registry_root)
    product = registry.get_product(identifier) or next(
        (item for item in registry.products() if item.product_key == identifier), None
    )
    if product is None:
        raise ReleaseBlocked(f"PRODUCT_NOT_FOUND: {identifier}")
    mappings = [item for item in registry.mappings() if item.product_id == product.product_id]
    tables = []
    for mapping in mappings:
        if table_ids is not None and mapping.table_id not in table_ids:
            continue
        table = registry.get_table(mapping.table_id)
        if table is None or table.version != mapping.table_version:
            raise ReleaseBlocked(f"RELEASE_TABLE_VERSION_MISMATCH: {mapping.table_id}")
        tables.append(table)

    product_root = Path(repository_root) / "products" / product.product_key
    quality_path = product_root / "quality" / "quality-report.json"
    if not quality_path.is_file():
        raise ReleaseBlocked("QUALITY_REPORT_MISSING")
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    config_path = product_root / "product.yaml"
    config = (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    )
    changeset_id = config.get("changeset_id") if isinstance(config, dict) else None
    changeset = registry.get_changeset(changeset_id) if changeset_id else None
    if changeset_id and changeset is None:
        raise ReleaseBlocked(f"CHANGESET_NOT_FOUND: {changeset_id}")
    if changeset is not None:
        for required_product_id in changeset.required_product_ids:
            readiness = changeset.ready_products.get(required_product_id)
            if readiness is None:
                continue
            current = registry.get_product(required_product_id)
            if current is None or current.version != readiness.version:
                raise ReleaseBlocked(
                    "CHANGESET_VERSION_NOT_CURRENT: "
                    f"{required_product_id}:v{readiness.version}"
                )

    artifact_hashes = _verify_release_files(product_root, quality)
    return build_release_plan(
        product,
        tables,
        changeset=changeset,
        quality_report=quality,
        artifact_hashes=artifact_hashes,
    )


def build_release_bundle(product_root: str | Path, output_path: str | Path) -> Path:
    root = Path(product_root)
    output = Path(output_path)
    entries = [
        *(root / "generated" / name for name in _GENERATED_ASSETS),
        *(root / "quality" / name for name in _QUALITY_ASSETS),
    ]
    missing = [path.relative_to(root).as_posix() for path in entries if not path.is_file()]
    if missing:
        raise ReleaseBlocked(f"RELEASE_ARTIFACT_MISSING: {missing[0]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(entries, key=lambda item: item.relative_to(root).as_posix()):
            name = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return output


def verify_tag_target(tag: str, *, expected_commit: str, existing_target: str | None) -> None:
    if existing_target is not None and existing_target != expected_commit:
        raise ReleaseBlocked(
            f"TAG_TARGET_CONFLICT: {tag} -> {existing_target} != {expected_commit}"
        )


def _verify_release_files(product_root: Path, quality: dict[str, Any]) -> dict[str, str]:
    expected = quality.get("artifact_hashes", {})
    hashes: dict[str, str] = {}
    for directory, names in (("generated", _GENERATED_ASSETS), ("quality", _QUALITY_ASSETS)):
        for name in names:
            path = product_root / directory / name
            if not path.is_file():
                raise ReleaseBlocked(f"RELEASE_ARTIFACT_MISSING: {directory}/{name}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            hashes[f"{directory}/{name}"] = digest
            if directory == "generated" and expected.get(name) not in (None, digest):
                raise ReleaseBlocked(f"RELEASE_ARTIFACT_HASH_MISMATCH: {name}")
    return hashes
