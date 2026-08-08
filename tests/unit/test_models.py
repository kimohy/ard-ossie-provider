from __future__ import annotations

import pytest
from pydantic import ValidationError

from ard_ossie.models import (
    CandidateChange,
    ColumnRecord,
    EntityStatus,
    Operation,
    ProductRecord,
    ProductTableRef,
    TableLocator,
    TableRecord,
)

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
LINK_ID = "lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330ec2"


def test_versions_accept_1_and_999_but_reject_outside_range() -> None:
    assert ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=1).version == 1
    assert (
        ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=999).version == 999
    )

    with pytest.raises(ValidationError):
        ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=0)
    with pytest.raises(ValidationError):
        ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=1000)


def test_table_locator_normalizes_case_and_whitespace_without_credentials() -> None:
    locator = TableLocator(
        source_system_id=" ERP-PROD ",
        catalog=" Analytics ",
        schema_name=" Sales ",
        table_name=" Orders ",
    )

    assert locator.key == "erp-prod|analytics|sales|orders"
    assert locator.model_dump() == {
        "source_system_id": "erp-prod",
        "catalog": "analytics",
        "schema_name": "sales",
        "table_name": "orders",
    }


def test_candidate_change_pins_product_and_table_base_versions() -> None:
    locator = TableLocator(
        source_system_id="erp-prod",
        catalog="analytics",
        schema_name="sales",
        table_name="orders",
    )
    product = ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=12)
    table = TableRecord(table_id=TABLE_ID, locator=locator, version=7)
    mapping = ProductTableRef(
        link_id=LINK_ID,
        product_id=PRODUCT_ID,
        table_id=TABLE_ID,
        table_version=7,
        usage="SOURCE",
    )

    change = CandidateChange(
        operation=Operation.UPDATE,
        product=product,
        tables=[table],
        mappings=[mapping],
        base_product_version=11,
        proposed_product_version=12,
        base_table_versions={TABLE_ID: 6},
        proposed_table_versions={TABLE_ID: 7},
        source_hashes={"product_html": "a" * 64},
    )

    assert change.product.status is EntityStatus.ACTIVE
    assert change.mappings[0].table_version == 7
    assert change.base_table_versions[TABLE_ID] == 6


def test_candidate_change_rejects_mapping_to_an_unlisted_table() -> None:
    product = ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=1)
    mapping = ProductTableRef(
        link_id=LINK_ID,
        product_id=PRODUCT_ID,
        table_id=TABLE_ID,
        table_version=1,
        usage="SOURCE",
    )

    with pytest.raises(ValidationError, match="unlisted table"):
        CandidateChange(operation="create", product=product, mappings=[mapping])


def test_table_registry_rejects_duplicate_column_names_and_ids() -> None:
    locator = TableLocator(
        source_system_id="erp",
        catalog="analytics",
        schema_name="sales",
        table_name="orders",
    )

    with pytest.raises(ValidationError, match="column names and IDs must be unique"):
        TableRecord(
            table_id=TABLE_ID,
            locator=locator,
            version=1,
            columns=[
                ColumnRecord(
                    column_id="col_0198f6d0-2a11-78d1-8672-67d49e69f14c",
                    name="order_id",
                ),
                ColumnRecord(
                    column_id="col_0198f6d0-2a11-78d1-8672-67d49e69f14d",
                    name="ORDER_ID",
                ),
            ],
        )
