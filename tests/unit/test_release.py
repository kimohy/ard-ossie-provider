from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ard_ossie.impact import ProductReadiness, build_changeset
from ard_ossie.models import ProductRecord, TableLocator, TableRecord
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


def test_existing_tag_must_point_to_merged_commit() -> None:
    verify_tag_target("product/x/v1", expected_commit="a" * 40, existing_target=None)
    verify_tag_target("product/x/v1", expected_commit="a" * 40, existing_target="a" * 40)

    with pytest.raises(ReleaseBlocked, match="TAG_TARGET_CONFLICT"):
        verify_tag_target("product/x/v1", expected_commit="a" * 40, existing_target="b" * 40)


def test_release_bundle_contains_public_artifacts_manifest_and_reports(tmp_path: Path) -> None:
    product_root = tmp_path / "products" / "sales-order"
    generated = product_root / "generated"
    quality = product_root / "quality"
    generated.mkdir(parents=True)
    quality.mkdir()
    for name in (
        "data-product.md",
        "data-semantic.md",
        "data-dictionary.json",
        "ossie-model.json",
        "source-manifest.json",
    ):
        (generated / name).write_text(name, encoding="utf-8")
    for name in (
        "quality-report.json",
        "duplicate-report.json",
        "version-report.json",
        "impact-report.json",
        "llm-suggestions.json",
    ):
        (quality / name).write_text(json.dumps({"name": name}), encoding="utf-8")

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
        }


def test_changeset_readiness_version_must_match_current_registry(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    from ard_ossie.registry import Registry

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
        json.dumps({"status": "PASS", "hard_errors": []}), encoding="utf-8"
    )

    with pytest.raises(ReleaseBlocked, match="CHANGESET_VERSION_NOT_CURRENT"):
        resolve_release_plan(
            PRODUCT_ID,
            registry_root=registry_root,
            repository_root=tmp_path,
        )
