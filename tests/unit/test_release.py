from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import ard_ossie.release as release_module
from ard_ossie.impact import ProductReadiness, build_changeset
from ard_ossie.models import ProductRecord, TableLocator, TableRecord
from ard_ossie.registry import Registry
from ard_ossie.release import (
    ReleaseBlocked,
    build_release_bundle,
    build_release_plan,
    resolve_release_plan,
    verify_tag_target,
)

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
OTHER_PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"


def semantic_fidelity_payload(
    *,
    status: str = "PASS",
    extraction_mode: str = "docx_xml",
    warning_codes: list[str] | None = None,
    degraded: bool = False,
) -> dict[str, object]:
    failed = status == "FAIL"
    return {
        "source_hash": "a" * 64,
        "extraction_mode": extraction_mode,
        "page_count": 1 if extraction_mode != "docx_xml" else 0,
        "parser_versions": {"semantic": "test"},
        "status": status,
        "heading_count": 0,
        "paragraph_count": 1,
        "list_item_count": 0,
        "table_count": 0,
        "row_count": 0,
        "cell_count": 0,
        "source_span_count": 1,
        "preserved_span_count": 0 if failed else 1,
        "excluded_span_count": 0,
        "unmatched_span_count": 1 if failed else 0,
        "duplicated_span_count": 0,
        "degraded_block_count": 1 if degraded else 0,
        "source_text_coverage": 0.0 if failed else 1.0,
        "removed_elements": [],
        "degraded_blocks": (
            [
                {
                    "order": 0,
                    "reason": "structure_unresolved",
                    "spans": [{"bbox": None, "text_hash": "b" * 64}],
                }
            ]
            if degraded
            else []
        ),
        "table_results": [],
        "warning_codes": warning_codes or [],
    }


def product(version: int = 12) -> ProductRecord:
    return ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=version)


def table(version: int = 7) -> TableRecord:
    return TableRecord(
        table_id=TABLE_ID,
        locator=TableLocator(
            source_system_id="erp",
            catalog="analytics",
            schema_name="sales",
            table_name="orders",
        ),
        version=version,
    )


def release_product_root(tmp_path: Path, *, include_repair: bool = True) -> Path:
    registry = Registry.load(tmp_path / "registry")
    registry.write_product(product())
    product_root = tmp_path / "products" / "sales-order"
    generated = product_root / "generated"
    quality = product_root / "quality"
    generated.mkdir(parents=True)
    quality.mkdir()
    generated_hashes: dict[str, str] = {}
    for name in (
        "data-product.md",
        "data-semantic.md",
        "data-dictionary.json",
        "ossie-model.json",
        "source-manifest.json",
    ):
        payload = f"generated:{name}".encode()
        (generated / name).write_bytes(payload)
        generated_hashes[name] = hashlib.sha256(payload).hexdigest()
    quality_names = [
        "duplicate-report.json",
        "version-report.json",
        "impact-report.json",
        "llm-suggestions.json",
        "semantic-fidelity.json",
    ]
    if include_repair:
        quality_names.append("semantic-structure-repair.json")
    quality_hashes: dict[str, str] = {}
    for name in quality_names:
        value = semantic_fidelity_payload() if name == "semantic-fidelity.json" else {"name": name}
        payload = (json.dumps(value) + "\n").encode()
        (quality / name).write_bytes(payload)
        quality_hashes[name] = hashlib.sha256(payload).hexdigest()
    (quality / "quality-report.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "product_id": PRODUCT_ID,
                "product_version": 12,
                "completeness": 1,
                "hard_errors": [],
                "warnings": [],
                "artifact_hashes": generated_hashes,
                "quality_artifact_hashes": quality_hashes,
            }
        ),
        encoding="utf-8",
    )
    (product_root / "product.yaml").write_text("changeset_id:\n", encoding="utf-8")
    return product_root


def replace_quality_sibling(product_root: Path, name: str, value: object) -> None:
    quality = product_root / "quality"
    payload = (json.dumps(value) + "\n").encode()
    (quality / name).write_bytes(payload)
    report_path = quality / "quality-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["quality_artifact_hashes"][name] = hashlib.sha256(payload).hexdigest()
    report_path.write_text(json.dumps(report), encoding="utf-8")


def add_candidate_diagnostics(product_root: Path, *, status: str) -> None:
    validation = {
        "status": status,
        "publishable": status == "verified",
        "source_hash": "a" * 64,
        "canonical_hash": "b" * 64,
        "findings": [],
        "character_coverage": 1.0,
        "missing_atom_count": 0,
        "duplicate_atom_count": 0,
        "degraded_block_count": 0,
        "model_call_count": 0,
    }
    for name in (
        "manifest.json",
        "evidence-summary.json",
        "candidate-report.json",
        "decision-report.json",
        "application-report.json",
        "failure-report.json",
    ):
        replace_quality_sibling(product_root, name, {"name": name})
    replace_quality_sibling(product_root, "validation-report.json", validation)


def test_release_tags_use_immutable_ids_and_numeric_versions() -> None:
    plan = build_release_plan(product(), [table()])

    assert plan.product_tag == f"product/{PRODUCT_ID}/v12"
    assert plan.table_tags == [f"table/{TABLE_ID}/v7"]


def test_incomplete_changeset_blocks_release_dispatch() -> None:
    changeset = build_changeset([TABLE_ID], [PRODUCT_ID, OTHER_PRODUCT_ID])
    changeset.ready_products[PRODUCT_ID] = ProductReadiness(
        version=12, pr_number=10, head_sha="a" * 40
    )

    with pytest.raises(ReleaseBlocked, match="CHANGESET_INCOMPLETE"):
        build_release_plan(product(), [table()], changeset=changeset)


def test_quality_hard_errors_block_release() -> None:
    with pytest.raises(ReleaseBlocked, match="QUALITY_GATE_FAILED"):
        build_release_plan(
            product(),
            [table()],
            quality_report={"status": "FAIL", "hard_errors": [{"code": "BAD"}]},
        )


def test_candidate_release_requires_verified_semantic_validation(tmp_path: Path) -> None:
    product_root = release_product_root(tmp_path)
    add_candidate_diagnostics(product_root, status="review_required")

    with pytest.raises(ReleaseBlocked, match="SEMANTIC_VALIDATION_NOT_VERIFIED"):
        build_release_bundle(product_root, tmp_path / "candidate.zip")


def test_existing_tag_must_point_to_merged_commit() -> None:
    verify_tag_target("product/x/v1", expected_commit="a" * 40, existing_target=None)
    verify_tag_target("product/x/v1", expected_commit="a" * 40, existing_target="a" * 40)

    with pytest.raises(ReleaseBlocked, match="TAG_TARGET_CONFLICT"):
        verify_tag_target("product/x/v1", expected_commit="a" * 40, existing_target="b" * 40)


def test_release_bundle_contains_public_artifacts_manifest_and_reports(tmp_path: Path) -> None:
    product_root = release_product_root(tmp_path)

    bundle = build_release_bundle(product_root, tmp_path / "dist" / "release.zip")

    with zipfile.ZipFile(bundle) as archive:
        assert set(archive.namelist()) == {
            "generated/data-product.md",
            "generated/data-semantic.md",
            "generated/data-dictionary.json",
            "generated/ossie-model.json",
            "generated/source-manifest.json",
            "quality/quality-report.json",
            "quality/duplicate-report.json",
            "quality/version-report.json",
            "quality/impact-report.json",
            "quality/llm-suggestions.json",
            "quality/semantic-fidelity.json",
            "quality/semantic-structure-repair.json",
        }


def test_release_bundle_succeeds_without_optional_semantic_repair(tmp_path: Path) -> None:
    product_root = release_product_root(tmp_path, include_repair=False)

    bundle = build_release_bundle(product_root, tmp_path / "dist" / "release.zip")

    with zipfile.ZipFile(bundle) as archive:
        assert "quality/semantic-fidelity.json" in archive.namelist()
        assert "quality/semantic-structure-repair.json" not in archive.namelist()


def test_release_bundle_requires_semantic_fidelity(tmp_path: Path) -> None:
    product_root = release_product_root(tmp_path)
    (product_root / "quality" / "semantic-fidelity.json").unlink()

    with pytest.raises(
        ReleaseBlocked,
        match="RELEASE_ARTIFACT_MISSING: quality/semantic-fidelity.json",
    ):
        build_release_bundle(product_root, tmp_path / "dist" / "release.zip")


@pytest.mark.parametrize("entrypoint", ["resolve", "bundle"])
def test_release_independently_rejects_failed_semantic_fidelity(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    product_root = release_product_root(tmp_path)
    replace_quality_sibling(
        product_root,
        "semantic-fidelity.json",
        semantic_fidelity_payload(status="FAIL"),
    )

    with pytest.raises(ReleaseBlocked, match="SEMANTIC_FIDELITY_GATE_FAILED"):
        if entrypoint == "resolve":
            resolve_release_plan(
                PRODUCT_ID,
                registry_root=tmp_path / "registry",
                repository_root=tmp_path,
            )
        else:
            build_release_bundle(product_root, tmp_path / "dist" / "release.zip")


@pytest.mark.parametrize("entrypoint", ["resolve", "bundle"])
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            semantic_fidelity_payload(
                status="WARN",
                extraction_mode="ocr",
                warning_codes=["SEMANTIC_OCR_CORRECTION_UNAVAILABLE"],
            ),
            id="visual-correction-unavailable",
        ),
        pytest.param(
            semantic_fidelity_payload(status="WARN", degraded=True),
            id="structure-degraded",
        ),
    ],
)
def test_release_independently_rejects_unresolved_semantic_fidelity(
    tmp_path: Path,
    entrypoint: str,
    payload: dict[str, object],
) -> None:
    product_root = release_product_root(tmp_path)
    replace_quality_sibling(product_root, "semantic-fidelity.json", payload)

    with pytest.raises(ReleaseBlocked, match="SEMANTIC_FIDELITY_GATE_FAILED"):
        if entrypoint == "resolve":
            resolve_release_plan(
                PRODUCT_ID,
                registry_root=tmp_path / "registry",
                repository_root=tmp_path,
            )
        else:
            build_release_bundle(product_root, tmp_path / "dist" / "release.zip")


@pytest.mark.parametrize(
    "name",
    ["semantic-fidelity.json", "semantic-structure-repair.json"],
)
def test_release_plan_rejects_quality_sibling_digest_mismatch(
    tmp_path: Path,
    name: str,
) -> None:
    product_root = release_product_root(tmp_path)
    (product_root / "quality" / name).write_text("corrupt", encoding="utf-8")

    with pytest.raises(ReleaseBlocked, match=f"RELEASE_ARTIFACT_HASH_MISMATCH: {name}"):
        resolve_release_plan(
            PRODUCT_ID,
            registry_root=tmp_path / "registry",
            repository_root=tmp_path,
        )


def test_release_plan_hashes_the_quality_snapshot_that_passed_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_root = release_product_root(tmp_path)
    quality_path = product_root / "quality" / "quality-report.json"
    passed_payload = quality_path.read_bytes()
    failed = json.loads(passed_payload)
    failed["status"] = "FAIL"
    failed_payload = json.dumps(failed).encode()
    real_validate = release_module.QualityReport.model_validate_json

    def mutate_after_parse(cls, value: bytes):
        quality = real_validate(value)
        quality_path.write_bytes(failed_payload)
        return quality

    monkeypatch.setattr(
        release_module.QualityReport,
        "model_validate_json",
        classmethod(mutate_after_parse),
    )

    plan = resolve_release_plan(
        PRODUCT_ID,
        registry_root=tmp_path / "registry",
        repository_root=tmp_path,
    )

    assert plan.artifact_hashes["quality/quality-report.json"] == hashlib.sha256(
        passed_payload
    ).hexdigest()
    assert quality_path.read_bytes() == failed_payload


def test_release_plan_gates_the_fidelity_snapshot_that_was_hashed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_root = release_product_root(tmp_path)
    fidelity_path = product_root / "quality" / "semantic-fidelity.json"
    replace_quality_sibling(
        product_root,
        "semantic-fidelity.json",
        semantic_fidelity_payload(status="FAIL"),
    )
    passed_payload = (json.dumps(semantic_fidelity_payload()) + "\n").encode()
    real_read_bytes = Path.read_bytes
    expected_entries = {
        path
        for directory in (product_root / "generated", product_root / "quality")
        for path in directory.iterdir()
        if path.is_file()
    }
    reads: dict[Path, int] = {}

    def mutate_after_hash(path: Path) -> bytes:
        payload = real_read_bytes(path)
        if path in expected_entries:
            reads[path] = reads.get(path, 0) + 1
        if path == fidelity_path and reads[path] == 1:
            fidelity_path.write_bytes(passed_payload)
        return payload

    monkeypatch.setattr(Path, "read_bytes", mutate_after_hash)

    with pytest.raises(ReleaseBlocked, match="SEMANTIC_FIDELITY_GATE_FAILED"):
        resolve_release_plan(
            PRODUCT_ID,
            registry_root=tmp_path / "registry",
            repository_root=tmp_path,
        )

    assert set(reads) == expected_entries
    assert set(reads.values()) == {1}


def test_release_bundle_gates_quality_from_its_single_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_root = release_product_root(tmp_path)
    quality_path = product_root / "quality" / "quality-report.json"
    failed = json.loads(quality_path.read_bytes())
    failed["status"] = "FAIL"
    failed_payload = json.dumps(failed).encode()
    quality_path.write_bytes(failed_payload)
    real_read_bytes = Path.read_bytes
    reads = 0

    def replace_after_snapshot(path: Path) -> bytes:
        nonlocal reads
        payload = real_read_bytes(path)
        if path == quality_path:
            reads += 1
            if reads == 1:
                passed = dict(failed)
                passed["status"] = "PASS"
                quality_path.write_text(json.dumps(passed), encoding="utf-8")
        return payload

    monkeypatch.setattr(Path, "read_bytes", replace_after_snapshot)

    with pytest.raises(ReleaseBlocked, match="QUALITY_GATE_FAILED"):
        build_release_bundle(product_root, tmp_path / "dist" / "release.zip")

    assert reads == 1


def test_release_bundle_archives_the_fidelity_snapshot_that_passed_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_root = release_product_root(tmp_path)
    fidelity_path = product_root / "quality" / "semantic-fidelity.json"
    passed_payload = fidelity_path.read_bytes()
    failed_payload = (json.dumps(semantic_fidelity_payload(status="FAIL")) + "\n").encode()
    real_read_bytes = Path.read_bytes
    expected_entries = {
        path
        for directory in (product_root / "generated", product_root / "quality")
        for path in directory.iterdir()
        if path.is_file()
    }
    reads: dict[Path, int] = {}

    def mutate_after_validation_read(path: Path) -> bytes:
        payload = real_read_bytes(path)
        if path in expected_entries:
            reads[path] = reads.get(path, 0) + 1
        if path == fidelity_path and reads[path] == 1:
            fidelity_path.write_bytes(failed_payload)
        return payload

    monkeypatch.setattr(Path, "read_bytes", mutate_after_validation_read)

    bundle = build_release_bundle(product_root, tmp_path / "dist" / "release.zip")

    with zipfile.ZipFile(bundle) as archive:
        assert archive.read("quality/semantic-fidelity.json") == passed_payload
    assert set(reads) == expected_entries
    assert set(reads.values()) == {1}


def test_changeset_readiness_version_must_match_current_registry(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry = Registry.load(registry_root)
    registry.write_product(product(version=13))
    registry.write_table(table())
    changeset = build_changeset(
        [TABLE_ID],
        [PRODUCT_ID],
        changeset_id="cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2",
    )
    changeset.mark_ready(PRODUCT_ID, version=12, pr_number=10, head_sha="a" * 40)
    registry.write_changeset(changeset)

    product_root = tmp_path / "products" / "sales-order"
    product_root.mkdir(parents=True)
    (product_root / "product.yaml").write_text(
        "changeset_id: cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2\n",
        encoding="utf-8",
    )
    quality = product_root / "quality"
    quality.mkdir()
    (quality / "quality-report.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "product_id": PRODUCT_ID,
                "product_version": 13,
                "completeness": 1,
                "hard_errors": [],
                "warnings": [],
                "artifact_hashes": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseBlocked, match="CHANGESET_VERSION_NOT_CURRENT"):
        resolve_release_plan(
            PRODUCT_ID,
            registry_root=registry_root,
            repository_root=tmp_path,
        )
