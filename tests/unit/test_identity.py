from __future__ import annotations

from ard_ossie.identity import DuplicateDecision, classify_product, classify_table
from ard_ossie.models import EntityStatus, Operation, ProductRecord, TableLocator, TableRecord

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
OTHER_PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
OTHER_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14d"


def product(
    product_id: str = PRODUCT_ID,
    key: str = "sales-order",
    canonical: str = "a" * 64,
    aliases: list[str] | None = None,
) -> ProductRecord:
    return ProductRecord(
        product_id=product_id,
        product_key=key,
        version=1,
        canonical_hash=canonical,
        aliases=aliases or [],
    )


def table(
    table_id: str = TABLE_ID,
    table_name: str = "orders",
    schema: str = "b" * 64,
    canonical: str = "c" * 64,
    status: EntityStatus = EntityStatus.ACTIVE,
) -> TableRecord:
    return TableRecord(
        table_id=table_id,
        locator=TableLocator(
            source_system_id="erp", catalog="analytics", schema_name="sales", table_name=table_name
        ),
        version=1,
        schema_hash=schema,
        canonical_hash=canonical,
        status=status,
    )


def test_existing_product_key_blocks_create() -> None:
    report = classify_product(
        product(product_id=OTHER_PRODUCT_ID),
        [product()],
        operation=Operation.CREATE,
    )

    assert report.decision is DuplicateDecision.BLOCK
    assert report.code == "PRODUCT_KEY_CONFLICT"
    assert report.target_id == PRODUCT_ID


def test_existing_product_alias_blocks_create() -> None:
    report = classify_product(
        product(product_id=OTHER_PRODUCT_ID, key="order-analytics"),
        [product(aliases=["order-analytics"])],
        operation=Operation.CREATE,
    )

    assert report.decision is DuplicateDecision.BLOCK
    assert report.code == "PRODUCT_ALIAS_CONFLICT"


def test_same_product_content_under_another_key_blocks_create() -> None:
    report = classify_product(
        product(product_id=OTHER_PRODUCT_ID, key="order-copy"),
        [product()],
        operation=Operation.CREATE,
    )

    assert report.decision is DuplicateDecision.BLOCK
    assert report.code == "PRODUCT_CONTENT_DUPLICATE"


def test_update_with_same_canonical_hash_is_no_change() -> None:
    report = classify_product(product(), [product()], operation=Operation.UPDATE)

    assert report.decision is DuplicateDecision.NO_CHANGE
    assert report.code == "PRODUCT_NO_CHANGE"


def test_semantic_candidates_never_auto_reuse_product_id() -> None:
    report = classify_product(
        product(product_id=OTHER_PRODUCT_ID, key="commerce-orders", canonical="d" * 64),
        [product()],
        operation=Operation.CREATE,
        semantic_candidate_ids=[PRODUCT_ID],
    )

    assert report.decision is DuplicateDecision.WARN
    assert report.code == "POSSIBLE_DUPLICATE"
    assert report.target_id is None


def test_same_table_locator_reuses_existing_table_id() -> None:
    report = classify_table(table(table_id=OTHER_TABLE_ID), [table()])

    assert report.decision is DuplicateDecision.REUSE
    assert report.target_id == TABLE_ID


def test_same_schema_at_different_locator_is_only_clone_warning() -> None:
    report = classify_table(
        table(table_id=OTHER_TABLE_ID, table_name="orders_copy", canonical="d" * 64),
        [table()],
    )

    assert report.decision is DuplicateDecision.WARN
    assert report.code == "POSSIBLE_CLONE"
    assert report.target_id is None


def test_retired_table_locator_blocks_reuse() -> None:
    report = classify_table(
        table(table_id=OTHER_TABLE_ID),
        [table(status=EntityStatus.RETIRED)],
    )

    assert report.decision is DuplicateDecision.BLOCK
    assert report.code == "RETIRED_ID_REUSE"
