from __future__ import annotations

import hashlib
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError

from ard_ossie.impact import ChangeSetRecord, ChangeSetStatus
from ard_ossie.models import ProductRecord, StrictModel, TableRecord
from ard_ossie.pipeline import QualityReport
from ard_ossie.registry import Registry
from ard_ossie.semantic.canonical import (
    SemanticPipelineStatus,
    SemanticValidationReport,
)
from ard_ossie.semantic.diagnostics import DIAGNOSTIC_REPORT_NAMES
from ard_ossie.semantic.models import ExtractionMode, SemanticFidelityReport


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
_REQUIRED_QUALITY_ASSETS = (
    "quality-report.json",
    "duplicate-report.json",
    "version-report.json",
    "impact-report.json",
    "llm-suggestions.json",
    "semantic-fidelity.json",
)
_OPTIONAL_QUALITY_ASSETS = (
    "semantic-structure-repair.json",
    *DIAGNOSTIC_REPORT_NAMES,
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
    snapshots = _snapshot_release_entries(product_root, require_complete=False)
    quality = _parse_quality_report(snapshots["quality/quality-report.json"])
    if quality.product_id != product.product_id:
        raise ReleaseBlocked("QUALITY_REPORT_PRODUCT_MISMATCH")
    if quality.product_version != product.version:
        raise ReleaseBlocked("QUALITY_REPORT_VERSION_MISMATCH")
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

    _require_release_snapshot_entries(snapshots)
    quality_data = quality.model_dump(mode="json")
    artifact_hashes = _verify_release_snapshots(snapshots, quality_data)
    candidate_verified = _verify_candidate_validation_snapshot(snapshots)
    _verify_semantic_fidelity_snapshot(
        snapshots["quality/semantic-fidelity.json"],
        candidate_verified=candidate_verified,
    )
    return build_release_plan(
        product,
        tables,
        changeset=changeset,
        quality_report=quality_data,
        artifact_hashes=artifact_hashes,
    )


def build_release_bundle(product_root: str | Path, output_path: str | Path) -> Path:
    root = Path(product_root)
    output = Path(output_path)
    snapshots = _snapshot_release_entries(root)
    quality = _parse_quality_report(snapshots["quality/quality-report.json"])
    quality_data = quality.model_dump(mode="json")
    _require_quality_pass(quality_data)
    candidate_verified = _verify_candidate_validation_snapshot(snapshots)
    _verify_semantic_fidelity_snapshot(
        snapshots["quality/semantic-fidelity.json"],
        candidate_verified=candidate_verified,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, payload in sorted(snapshots.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload)
        temporary.replace(output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def verify_release_bundle(
    bundle: str | Path | bytes,
    expected_hashes: dict[str, str],
) -> bytes:
    try:
        payload = bundle if isinstance(bundle, bytes) else Path(bundle).read_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected_hashes):
                raise ReleaseBlocked(
                    "RELEASE_BUNDLE_CONTENT_MISMATCH: "
                    "release bundle entries do not match the verified plan"
                )
            for name, digest in expected_hashes.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    raise ReleaseBlocked(
                        f"RELEASE_BUNDLE_HASH_MISMATCH: "
                        f"release bundle source changed after planning: {name}"
                    )
    except ReleaseBlocked:
        raise
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as error:
        raise ReleaseBlocked("RELEASE_BUNDLE_INVALID: release bundle is malformed") from error
    return payload


def release_source_paths(product_root: str | Path) -> tuple[Path, ...]:
    root = Path(product_root)
    return (root / "product.yaml", *_release_bundle_entries(root))


def _release_bundle_entries(root: Path) -> tuple[Path, ...]:
    return (
        *(root / "generated" / name for name in _GENERATED_ASSETS),
        *(root / "quality" / name for name in _quality_asset_names(root)),
    )


def _quality_asset_names(root: Path) -> tuple[str, ...]:
    quality = root / "quality"
    present_optional = tuple(
        name
        for name in _OPTIONAL_QUALITY_ASSETS
        if (quality / name).exists() or (quality / name).is_symlink()
    )
    return (*_REQUIRED_QUALITY_ASSETS, *present_optional)


def verify_tag_target(tag: str, *, expected_commit: str, existing_target: str | None) -> None:
    if existing_target is not None and existing_target != expected_commit:
        raise ReleaseBlocked(
            f"TAG_TARGET_CONFLICT: {tag} -> {existing_target} != {expected_commit}"
        )


def _snapshot_release_entries(
    product_root: Path,
    *,
    require_complete: bool = True,
) -> dict[str, bytes]:
    entries = _release_bundle_entries(product_root)
    present = tuple(path for path in entries if path.is_file())
    if require_complete:
        present_names = {
            path.relative_to(product_root).as_posix() for path in present
        }
        _require_release_snapshot_entries(present_names)
    return {
        path.relative_to(product_root).as_posix(): path.read_bytes()
        for path in sorted(
            present,
            key=lambda item: item.relative_to(product_root).as_posix(),
        )
    }


def _require_release_snapshot_entries(snapshots: dict[str, bytes] | set[str]) -> None:
    present = set(snapshots)
    required = (
        *(f"generated/{name}" for name in _GENERATED_ASSETS),
        *(f"quality/{name}" for name in _REQUIRED_QUALITY_ASSETS),
    )
    missing = [name for name in required if name not in present]
    if missing:
        raise ReleaseBlocked(f"RELEASE_ARTIFACT_MISSING: {missing[0]}")
    diagnostic_entries = {f"quality/{name}" for name in DIAGNOSTIC_REPORT_NAMES}
    present_diagnostics = present & diagnostic_entries
    if present_diagnostics and present_diagnostics != diagnostic_entries:
        missing_diagnostic = sorted(diagnostic_entries - present_diagnostics)[0]
        raise ReleaseBlocked(f"RELEASE_ARTIFACT_MISSING: {missing_diagnostic}")


def _parse_quality_report(payload: bytes) -> QualityReport:
    try:
        return QualityReport.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ReleaseBlocked("QUALITY_REPORT_INVALID") from error


def _require_quality_pass(quality: dict[str, Any]) -> None:
    if quality.get("status") == "FAIL" or quality.get("hard_errors"):
        raise ReleaseBlocked("QUALITY_GATE_FAILED")


def _verify_release_snapshots(
    snapshots: dict[str, bytes],
    quality: dict[str, Any],
) -> dict[str, str]:
    expected = quality.get("artifact_hashes", {})
    if set(expected) != set(_GENERATED_ASSETS):
        raise ReleaseBlocked("QUALITY_ARTIFACT_HASH_SET_MISMATCH")
    quality_names = tuple(
        name
        for name in (*_REQUIRED_QUALITY_ASSETS, *_OPTIONAL_QUALITY_ASSETS)
        if f"quality/{name}" in snapshots
    )
    expected_quality = quality.get("quality_artifact_hashes", {})
    quality_siblings = set(quality_names) - {"quality-report.json"}
    if set(expected_quality) != quality_siblings:
        raise ReleaseBlocked("QUALITY_ARTIFACT_HASH_SET_MISMATCH")
    hashes: dict[str, str] = {}
    for directory, names in (
        ("generated", _GENERATED_ASSETS),
        ("quality", quality_names),
    ):
        for name in names:
            entry_name = f"{directory}/{name}"
            digest = hashlib.sha256(snapshots[entry_name]).hexdigest()
            hashes[entry_name] = digest
            if directory == "generated" and expected[name] != digest:
                raise ReleaseBlocked(f"RELEASE_ARTIFACT_HASH_MISMATCH: {name}")
            if (
                directory == "quality"
                and name != "quality-report.json"
                and expected_quality[name] != digest
            ):
                raise ReleaseBlocked(f"RELEASE_ARTIFACT_HASH_MISMATCH: {name}")
    return hashes


def _verify_semantic_fidelity_snapshot(
    payload: bytes,
    *,
    candidate_verified: bool = False,
) -> SemanticFidelityReport:
    try:
        fidelity = SemanticFidelityReport.model_validate_json(payload)
    except (ValidationError, ValueError) as error:
        raise ReleaseBlocked("SEMANTIC_FIDELITY_REPORT_INVALID") from error
    if candidate_verified:
        return fidelity
    reasons: list[str] = []
    if fidelity.status == "FAIL":
        reasons.append("status=FAIL")
    if fidelity.unmatched_span_count > 0:
        reasons.append(f"unmatched_spans={fidelity.unmatched_span_count}")
    if fidelity.duplicated_span_count > 0:
        reasons.append(f"duplicated_spans={fidelity.duplicated_span_count}")
    if fidelity.source_text_coverage < 1.0:
        reasons.append(f"source_text_coverage={fidelity.source_text_coverage}")
    if fidelity.degraded_block_count > 0:
        reasons.append(f"degraded_blocks={fidelity.degraded_block_count}")
    if fidelity.extraction_mode in {
        ExtractionMode.PDF_EMBEDDED,
        ExtractionMode.OCR,
    }:
        expected_pages = set(range(1, fidelity.page_count + 1))
        audited_pages = {item.page for item in fidelity.ocr_corrections}
        failed_outcomes = sorted(
            {
                item.outcome
                for item in fidelity.ocr_corrections
                if item.outcome not in {"applied", "reused"}
            }
        )
        if fidelity.warning_codes:
            reasons.append("warning_codes=" + ",".join(fidelity.warning_codes))
        if fidelity.ocr_correction_rejected_count > 0:
            reasons.append(
                f"rejected_corrections={fidelity.ocr_correction_rejected_count}"
            )
        if failed_outcomes:
            reasons.append("correction_outcomes=" + ",".join(failed_outcomes))
        if audited_pages != expected_pages:
            missing = ",".join(str(page) for page in sorted(expected_pages - audited_pages))
            reasons.append(f"missing_correction_pages={missing or 'none'}")
    if reasons:
        raise ReleaseBlocked("SEMANTIC_FIDELITY_GATE_FAILED: " + "; ".join(reasons))
    return fidelity


def _verify_candidate_validation_snapshot(snapshots: dict[str, bytes]) -> bool:
    path = "quality/validation-report.json"
    if path not in snapshots:
        return False
    try:
        validation = SemanticValidationReport.model_validate_json(snapshots[path])
    except (ValidationError, ValueError) as error:
        raise ReleaseBlocked("SEMANTIC_VALIDATION_REPORT_INVALID") from error
    if (
        validation.status is not SemanticPipelineStatus.VERIFIED
        or not validation.publishable
    ):
        raise ReleaseBlocked(
            f"SEMANTIC_VALIDATION_NOT_VERIFIED: {validation.status.value}"
        )
    return True
