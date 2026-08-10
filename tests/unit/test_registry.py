from __future__ import annotations

import json
from pathlib import Path

import pytest

from ard_ossie.impact import build_changeset
from ard_ossie.models import EntityStatus, ProductRecord, ProductTableRef, TableLocator, TableRecord
from ard_ossie.registry import IdentityConflict, Registry

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
OTHER_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14d"
LINK_ID = "lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330ec2"


def product() -> ProductRecord:
    return ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=1)


def table(
    table_id: str = TABLE_ID,
    status: EntityStatus = EntityStatus.ACTIVE,
) -> TableRecord:
    return TableRecord(
        table_id=table_id,
        locator=TableLocator(
            source_system_id="erp", catalog="analytics", schema_name="sales", table_name="orders"
        ),
        version=1,
        status=status,
    )


def mapping() -> ProductTableRef:
    return ProductTableRef(
        link_id=LINK_ID,
        product_id=PRODUCT_ID,
        table_id=TABLE_ID,
        table_version=1,
        usage="SOURCE",
    )


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_registry_writes_one_file_per_entity_and_generated_indexes(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.write_product(product())
    registry.write_table(table())
    registry.write_mappings(PRODUCT_ID, [mapping()])

    assert read_json(tmp_path / "products" / f"{PRODUCT_ID}.json")["product_key"] == "sales-order"
    assert read_json(tmp_path / "tables" / f"{TABLE_ID}.json")["locator"]["table_name"] == "orders"
    assert read_json(tmp_path / "mappings" / f"{PRODUCT_ID}.json")[0]["table_id"] == TABLE_ID
    assert read_json(tmp_path / "indexes" / "product-keys.json") == {"sales-order": PRODUCT_ID}
    assert read_json(tmp_path / "indexes" / "table-locators.json") == {
        "erp|analytics|sales|orders": TABLE_ID
    }


def test_registry_reloads_validated_records(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.write_product(product())
    registry.write_table(table())

    loaded = Registry.load(tmp_path)

    assert loaded.get_product(PRODUCT_ID) == product()
    assert loaded.get_table(TABLE_ID) == table()


def test_retired_id_cannot_be_reactivated(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.write_table(table(status=EntityStatus.RETIRED))

    with pytest.raises(IdentityConflict, match="RETIRED_ID_REUSE"):
        registry.write_table(table(status=EntityStatus.ACTIVE))


def test_same_locator_cannot_be_written_under_another_table_id(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.write_table(table())

    with pytest.raises(IdentityConflict, match="TABLE_LOCATOR_CONFLICT"):
        registry.write_table(table(table_id=OTHER_TABLE_ID))


def test_mapping_rejects_unknown_table(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.write_product(product())

    with pytest.raises(IdentityConflict, match="MAPPING_TABLE_NOT_FOUND"):
        registry.write_mappings(PRODUCT_ID, [mapping()])


def test_registry_persists_and_reloads_changeset(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    changeset = build_changeset(
        [TABLE_ID],
        [PRODUCT_ID],
        changeset_id="cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2",
    )

    registry.write_changeset(changeset)

    loaded = Registry.load(tmp_path)
    assert loaded.get_changeset(changeset.changeset_id) == changeset
