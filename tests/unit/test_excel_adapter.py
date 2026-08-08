from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from ard_ossie.excel_adapter import parse_dictionary


def create_dictionary(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data Dictionary"
    sheet.append(
        [
            "Platform",
            "Catalog",
            "Schema",
            "Table",
            "Column",
            "Logical Name",
            "Data Type",
            "Nullable",
            "PK",
            "FK Table",
            "FK Column",
            "Description",
        ]
    )
    sheet.append(
        [
            "BigQuery",
            "analytics",
            "sales",
            "orders",
            "order_id",
            "Order ID",
            "INT64",
            "N",
            "Y",
            None,
            None,
            "Unique order identifier",
        ]
    )
    sheet.append(
        [
            "BigQuery",
            "analytics",
            "sales",
            "orders",
            "customer_id",
            "Customer ID",
            "INT64",
            "N",
            "N",
            "customers",
            "customer_id",
            "Ordering customer",
        ]
    )
    workbook.save(path)


def test_excel_adapter_preserves_physical_schema_and_cell_evidence(tmp_path: Path) -> None:
    path = tmp_path / "dictionary.xlsx"
    create_dictionary(path)

    parsed = parse_dictionary(path, source_hash="a" * 64)

    assert len(parsed.tables) == 1
    table = parsed.tables[0]
    assert table.locator == "bigquery|analytics|sales|orders"
    assert [column.name for column in table.columns] == ["order_id", "customer_id"]
    assert table.columns[0].primary_key is True
    assert table.columns[1].foreign_key == "customers.customer_id"
    assert table.columns[0].evidence.locator == {
        "sheet": "Data Dictionary",
        "range": "A2:L2",
    }


def test_excel_adapter_rejects_missing_required_header(tmp_path: Path) -> None:
    path = tmp_path / "dictionary.xlsx"
    workbook = Workbook()
    workbook.active.append(["Table", "Column"])
    workbook.save(path)

    try:
        parse_dictionary(path, source_hash="a" * 64)
    except ValueError as error:
        assert "MISSING_DICTIONARY_HEADERS" in str(error)
    else:
        raise AssertionError("missing headers must fail")


def test_excel_adapter_reads_explicit_formula_and_comment_columns(tmp_path: Path) -> None:
    path = tmp_path / "dictionary.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "Platform",
            "Catalog",
            "Schema",
            "Table",
            "Column",
            "Data Type",
            "Nullable",
            "PK",
            "Formula",
            "Comment",
        ]
    )
    sheet.append(
        [
            "erp",
            "analytics",
            "sales",
            "orders",
            "net_amount",
            "DECIMAL",
            "false",
            "no",
            "gross_amount - tax_amount",
            "Validated finance rule",
        ]
    )
    workbook.save(path)

    column = parse_dictionary(path, source_hash="a" * 64).tables[0].columns[0]

    assert column.formula == "gross_amount - tax_amount"
    assert column.comment == "Validated finance rule"


def test_excel_adapter_rejects_unknown_boolean_text(tmp_path: Path) -> None:
    path = tmp_path / "dictionary.xlsx"
    create_dictionary(path)
    workbook = load_workbook(path)
    workbook.active["H2"] = "sometimes"
    workbook.save(path)
    workbook.close()

    with pytest.raises(ValueError, match="INVALID_BOOLEAN_VALUE"):
        parse_dictionary(path, source_hash="a" * 64)


def test_excel_adapter_rejects_half_specified_foreign_key(tmp_path: Path) -> None:
    path = tmp_path / "dictionary.xlsx"
    create_dictionary(path)
    workbook = load_workbook(path)
    workbook.active["K3"] = None
    workbook.save(path)
    workbook.close()

    with pytest.raises(ValueError, match="INCOMPLETE_FOREIGN_KEY"):
        parse_dictionary(path, source_hash="a" * 64)
