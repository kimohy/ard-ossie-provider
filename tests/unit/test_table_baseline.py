from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ard_ossie.docling_parser import Evidence
from ard_ossie.ingestion import SourceRole
from ard_ossie.ir import ColumnIR
from ard_ossie.models import (
    ProductRecord,
    ProductTableRef,
    TableLocator,
    TableRecord,
)
from ard_ossie.registry import Registry
from ard_ossie.table_baseline import (
    TableBaselineError,
    parse_table_baseline,
    published_table_from_ir,
    read_local_table_baseline,
    table_content_hash,
    validate_table_baseline,
)

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
OTHER_PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
ORDERS_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
CUSTOMERS_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14d"
OTHER_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14e"
ORDER_ID_COLUMN_ID = "col_0198f6ca-2a11-78d1-8672-67d49e69f14c"
CUSTOMER_ID_COLUMN_ID = "col_0198f6ca-2a11-78d1-8672-67d49e69f14d"


def column_payload(
    *,
    column_id: str = ORDER_ID_COLUMN_ID,
    ordinal: int = 1,
    name: str = "order_id",
    description: str = "Unique order identifier",
) -> dict[str, object]:
    return {
        "column_id": column_id,
        "ordinal": ordinal,
        "name": name,
        "logical_name": None,
        "data_type": "INT64",
        "nullable": False,
        "primary_key": True,
        "description": description,
    }


def table_payload(
    *,
    table_id: str = ORDERS_TABLE_ID,
    table_version: int = 1,
    dataset_name: str = "orders",
    source: str = "analytics.sales.orders",
    description: str = "Confirmed sales orders",
    columns: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "table_id": table_id,
        "table_version": table_version,
        "dataset_name": dataset_name,
        "source": source,
        "description": description,
        "columns": columns if columns is not None else [column_payload()],
    }


def baseline_payload() -> dict[str, object]:
    return {
        "product_id": PRODUCT_ID,
        "product_version": 2,
        "tables": [
            table_payload(),
            table_payload(
                table_id=CUSTOMERS_TABLE_ID,
                dataset_name="customers",
                source="analytics.sales.customers",
                description="Sales customers",
                columns=[
                    column_payload(
                        column_id=CUSTOMER_ID_COLUMN_ID,
                        name="customer_id",
                        description="Unique customer identifier",
                    )
                ],
            ),
        ],
    }


def baseline_bytes(payload: dict[str, object] | None = None) -> bytes:
    return (json.dumps(payload or baseline_payload(), ensure_ascii=False) + "\n").encode()


def registry_fixture(root: Path) -> Registry:
    registry = Registry(root)
    registry.write_product(
        ProductRecord(
            product_id=PRODUCT_ID,
            product_key="sales-order",
            version=2,
        )
    )
    registry.write_product(
        ProductRecord(
            product_id=OTHER_PRODUCT_ID,
            product_key="finance-order",
            version=1,
        )
    )
    for table_id, table_name in (
        (ORDERS_TABLE_ID, "orders"),
        (CUSTOMERS_TABLE_ID, "customers"),
    ):
        registry.write_table(
            TableRecord(
                table_id=table_id,
                locator=TableLocator(
                    source_system_id="erp",
                    catalog="analytics",
                    schema_name="sales",
                    table_name=table_name,
                ),
                version=1,
            )
        )
    registry.write_mappings(
        PRODUCT_ID,
        [
            ProductTableRef(
                link_id="lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330ec2",
                product_id=PRODUCT_ID,
                table_id=ORDERS_TABLE_ID,
                table_version=1,
                usage="SOURCE",
            ),
            ProductTableRef(
                link_id="lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330ec3",
                product_id=PRODUCT_ID,
                table_id=CUSTOMERS_TABLE_ID,
                table_version=1,
                usage="SOURCE",
            ),
        ],
    )
    return registry


def assert_invalid(payload: bytes) -> None:
    with pytest.raises(TableBaselineError) as captured:
        parse_table_baseline(payload)
    assert str(captured.value) == "TABLE_BASELINE_INVALID"


def test_parse_table_baseline_accepts_the_published_dictionary_contract() -> None:
    baseline = parse_table_baseline(baseline_bytes())

    assert baseline.product_id == PRODUCT_ID
    assert baseline.product_version == 2
    assert [table.table_id for table in baseline.tables] == [
        ORDERS_TABLE_ID,
        CUSTOMERS_TABLE_ID,
    ]
    assert baseline.tables[0].columns[0].description == "Unique order identifier"


def test_parse_table_baseline_redacts_malformed_input() -> None:
    payload = b'{"secret marker":"do-not-log", "tables": [\xff]}'

    with pytest.raises(TableBaselineError) as captured:
        parse_table_baseline(payload)

    assert str(captured.value) == "TABLE_BASELINE_INVALID"
    assert "secret marker" not in str(captured.value)
    assert "do-not-log" not in str(captured.value)


def test_parse_table_baseline_rejects_unknown_fields() -> None:
    payload = baseline_payload()
    payload["unexpected"] = "value"

    assert_invalid(baseline_bytes(payload))


@pytest.mark.parametrize(
    ("scope", "field"),
    [
        ("table", "description"),
        ("column", "logical_name"),
        ("column", "description"),
    ],
)
def test_parse_table_baseline_requires_nullable_renderer_fields(
    scope: str,
    field: str,
) -> None:
    payload = baseline_payload()
    target = payload["tables"][0]
    if scope == "column":
        target = target["columns"][0]
    del target[field]

    assert_invalid(baseline_bytes(payload))


@pytest.mark.parametrize(
    ("scope", "field", "value"),
    [
        ("table", "dataset_name", 7),
        ("table", "source", {"secret marker": "do-not-log"}),
        ("table", "description", 12),
        ("column", "name", ["order_id"]),
        ("column", "logical_name", 42),
        ("column", "data_type", 12),
        ("column", "nullable", "false"),
        ("column", "primary_key", 1),
        ("column", "description", {"secret marker": "do-not-log"}),
    ],
)
def test_parse_table_baseline_rejects_coerced_renderer_field_types(
    scope: str,
    field: str,
    value: object,
) -> None:
    payload = baseline_payload()
    target = payload["tables"][0]
    if scope == "column":
        target = target["columns"][0]
    target[field] = value

    assert_invalid(baseline_bytes(payload))


def test_parse_table_baseline_rejects_duplicate_table_ids() -> None:
    payload = baseline_payload()
    payload["tables"] = [table_payload(), table_payload()]

    assert_invalid(baseline_bytes(payload))


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("column_id", ORDER_ID_COLUMN_ID),
        ("ordinal", 1),
        ("name", "ORDER_ID"),
    ],
)
def test_parse_table_baseline_rejects_duplicate_column_identity(
    mutation: str,
    value: object,
) -> None:
    payload = baseline_payload()
    first_table = payload["tables"][0]
    second = column_payload(
        column_id=CUSTOMER_ID_COLUMN_ID,
        ordinal=2,
        name="customer_id",
    )
    second[mutation] = value
    first_table["columns"].append(second)

    assert_invalid(baseline_bytes(payload))


def test_validate_table_baseline_binds_the_exact_product_mapping(tmp_path: Path) -> None:
    registry = registry_fixture(tmp_path / "registry")
    product = registry.get_product(PRODUCT_ID)
    assert product is not None

    tables = validate_table_baseline(
        parse_table_baseline(baseline_bytes()),
        product=product,
        registry=registry,
    )

    assert list(tables) == [ORDERS_TABLE_ID, CUSTOMERS_TABLE_ID]
    assert tables[ORDERS_TABLE_ID].dataset_name == "orders"


@pytest.mark.parametrize(
    "mutation",
    [
        "product_id",
        "product_version",
        "missing_table",
        "extra_table",
        "table_version",
        "dataset_name",
        "source",
    ],
)
def test_validate_table_baseline_rejects_registry_binding_mismatches(
    tmp_path: Path,
    mutation: str,
) -> None:
    registry = registry_fixture(tmp_path / "registry")
    product = registry.get_product(PRODUCT_ID)
    assert product is not None
    payload = copy.deepcopy(baseline_payload())
    if mutation == "product_id":
        payload["product_id"] = OTHER_PRODUCT_ID
    elif mutation == "product_version":
        payload["product_version"] = 1
    elif mutation == "missing_table":
        payload["tables"] = payload["tables"][:1]
    elif mutation == "extra_table":
        payload["tables"].append(
            table_payload(
                table_id=OTHER_TABLE_ID,
                dataset_name="other",
                source="analytics.sales.other",
            )
        )
    elif mutation == "table_version":
        payload["tables"][0]["table_version"] = 2
    elif mutation == "dataset_name":
        payload["tables"][0]["dataset_name"] = "renamed_orders"
    elif mutation == "source":
        payload["tables"][0]["source"] = "analytics.finance.orders"

    with pytest.raises(TableBaselineError) as captured:
        validate_table_baseline(
            parse_table_baseline(baseline_bytes(payload)),
            product=product,
            registry=registry,
        )

    assert str(captured.value) == "TABLE_BASELINE_INVALID"


def test_table_content_hash_ignores_version_but_detects_content_and_locator_changes() -> None:
    first = parse_table_baseline(baseline_bytes()).tables[0]
    version_only = first.model_copy(update={"table_version": 2})
    description_change = first.model_copy(update={"description": "Changed description"})
    locator = TableLocator(
        source_system_id="erp",
        catalog="analytics",
        schema_name="sales",
        table_name="orders",
    )
    other_locator = locator.model_copy(update={"source_system_id": "warehouse"})

    original_hash = table_content_hash(first, locator=locator)

    assert table_content_hash(version_only, locator=locator) == original_hash
    assert table_content_hash(description_change, locator=locator) != original_hash
    assert table_content_hash(first, locator=other_locator) != original_hash


def test_published_table_projection_excludes_current_evidence_provenance() -> None:
    locator = TableLocator(
        source_system_id="erp",
        catalog="analytics",
        schema_name="sales",
        table_name="orders",
    )

    def column(source_hash: str) -> ColumnIR:
        return ColumnIR(
            column_id=ORDER_ID_COLUMN_ID,
            ordinal=1,
            name="order_id",
            data_type="INT64",
            nullable=False,
            primary_key=True,
            description="Unique order identifier",
            evidence=[
                Evidence(
                    source_hash=source_hash,
                    role=SourceRole.DICTIONARY_EXCEL,
                    locator={"sheet": "Dictionary", "range": "A2:I2"},
                    excerpt="order_id",
                )
            ],
        )

    first = published_table_from_ir(
        table_id=ORDERS_TABLE_ID,
        table_version=2,
        locator=locator,
        description="Confirmed sales orders",
        columns=[column("a" * 64)],
    )
    second = published_table_from_ir(
        table_id=ORDERS_TABLE_ID,
        table_version=2,
        locator=locator,
        description="Confirmed sales orders",
        columns=[column("b" * 64)],
    )

    assert first == second
    assert first.source == "analytics.sales.orders"
    assert table_content_hash(first, locator=locator) == table_content_hash(
        second,
        locator=locator,
    )


def test_read_local_table_baseline_returns_none_when_generated_dictionary_is_absent(
    tmp_path: Path,
) -> None:
    product = tmp_path / "product"
    product.mkdir()

    assert read_local_table_baseline(product) is None


def test_read_local_table_baseline_snapshots_regular_file_bytes(tmp_path: Path) -> None:
    product = tmp_path / "product"
    generated = product / "generated"
    generated.mkdir(parents=True)
    expected = baseline_bytes()
    (generated / "data-dictionary.json").write_bytes(expected)

    assert read_local_table_baseline(product) == expected


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_read_local_table_baseline_rejects_non_regular_paths(
    tmp_path: Path,
    kind: str,
) -> None:
    product = tmp_path / "product"
    generated = product / "generated"
    generated.mkdir(parents=True)
    baseline_path = generated / "data-dictionary.json"
    if kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(baseline_bytes())
        baseline_path.symlink_to(outside)
    else:
        baseline_path.mkdir()

    with pytest.raises(TableBaselineError) as captured:
        read_local_table_baseline(product)

    assert str(captured.value) == "TABLE_BASELINE_INVALID"
