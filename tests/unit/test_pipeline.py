from __future__ import annotations

from pathlib import Path

from ard_ossie.impact import build_changeset
from ard_ossie.models import ProductRecord, ProductTableRef, TableLocator, TableRecord
from ard_ossie.pipeline import ProductConfig, _shared_table_findings
from ard_ossie.registry import Registry
from ard_ossie.versioning import plan_version

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
OTHER_PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"


def shared_registry(root: Path) -> Registry:
    registry = Registry(root)
    for product_id, key in ((PRODUCT_ID, "sales-order"), (OTHER_PRODUCT_ID, "finance-order")):
        registry.write_product(ProductRecord(product_id=product_id, product_key=key, version=1))
    table = TableRecord(
        table_id=TABLE_ID,
        locator=TableLocator(
            source_system_id="erp",
            catalog="analytics",
            schema_name="sales",
            table_name="orders",
        ),
        version=1,
    )
    registry.write_table(table)
    registry.write_mappings(
        PRODUCT_ID,
        [
            ProductTableRef(
                link_id="lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330ec2",
                product_id=PRODUCT_ID,
                table_id=TABLE_ID,
                table_version=1,
                usage="SOURCE",
            )
        ],
    )
    registry.write_mappings(
        OTHER_PRODUCT_ID,
        [
            ProductTableRef(
                link_id="lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330ec3",
                product_id=OTHER_PRODUCT_ID,
                table_id=TABLE_ID,
                table_version=1,
                usage="SOURCE",
            )
        ],
    )
    return registry


def config(*, changeset_id: str | None = None) -> ProductConfig:
    return ProductConfig(
        operation="update",
        product_id=PRODUCT_ID,
        product_key="sales-order",
        version=2,
        display_name="Sales Order",
        changeset_id=changeset_id,
    )


def changed_table() -> TableRecord:
    return TableRecord(
        table_id=TABLE_ID,
        locator=TableLocator(
            source_system_id="erp",
            catalog="analytics",
            schema_name="sales",
            table_name="orders",
        ),
        version=2,
    )


def changed_version():
    return plan_version(
        current_version=1,
        changed=True,
        base_version=1,
        proposed_version=2,
    )


def test_shared_table_change_requires_changeset(tmp_path: Path) -> None:
    registry = shared_registry(tmp_path / "registry")

    findings = _shared_table_findings(
        config(), PRODUCT_ID, [changed_table()], [changed_version()], registry, pr_number=7
    )

    assert [finding.code for finding in findings] == ["SHARED_TABLE_CHANGESET_REQUIRED"]


def test_valid_changeset_covers_shared_table_product_and_pr(tmp_path: Path) -> None:
    registry = shared_registry(tmp_path / "registry")
    changeset = build_changeset(
        [TABLE_ID],
        [PRODUCT_ID, OTHER_PRODUCT_ID],
        changeset_id="cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2",
    )
    registry.write_changeset(changeset)

    findings = _shared_table_findings(
        config(changeset_id=changeset.changeset_id),
        PRODUCT_ID,
        [changed_table()],
        [changed_version()],
        registry,
        pr_number=7,
    )

    assert findings == []
