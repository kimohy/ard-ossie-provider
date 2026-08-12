from __future__ import annotations

import pytest

from ard_ossie.docling_parser import Evidence
from ard_ossie.ingestion import SourceRole
from ard_ossie.ir import (
    ColumnIR,
    MetricIR,
    ProductFactIR,
    ProductIR,
    RelationshipIR,
    TableIR,
)

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
ORDERS_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
CUSTOMERS_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14d"


def dictionary_evidence(row: int, excerpt: str) -> Evidence:
    return Evidence(
        source_hash="a" * 64,
        role=SourceRole.DICTIONARY_EXCEL,
        locator={"sheet": "Data Dictionary", "range": f"A{row}:L{row}"},
        excerpt=excerpt,
    )


@pytest.fixture
def resolved_sales_order_ir() -> ProductIR:
    customers = TableIR(
        table_id=CUSTOMERS_TABLE_ID,
        table_version=3,
        dataset_name="customers",
        source="analytics.sales.customers",
        description="Customer master",
        columns=[
            ColumnIR(
                column_id="col_0198f6d0-2a11-78d1-8672-67d49e69f14d",
                ordinal=1,
                name="customer_id",
                logical_name="Customer ID",
                data_type="INT64",
                nullable=False,
                primary_key=True,
                description="Unique customer identifier",
                evidence=[dictionary_evidence(4, "customer_id")],
            )
        ],
    )
    orders = TableIR(
        table_id=ORDERS_TABLE_ID,
        table_version=7,
        dataset_name="orders",
        source="analytics.sales.orders",
        description="Sales orders",
        columns=[
            ColumnIR(
                column_id="col_0198f6d0-2a11-78d1-8672-67d49e69f14c",
                ordinal=1,
                name="order_id",
                logical_name="Order ID",
                data_type="INT64",
                nullable=False,
                primary_key=True,
                description="Unique order identifier",
                evidence=[dictionary_evidence(2, "order_id")],
            ),
            ColumnIR(
                column_id="col_0198f6d0-2a11-78d1-8672-67d49e69f14e",
                ordinal=2,
                name="customer_id",
                logical_name="Customer ID",
                data_type="INT64",
                nullable=False,
                primary_key=False,
                description="Ordering customer",
                evidence=[dictionary_evidence(3, "customer_id")],
            ),
        ],
    )
    return ProductIR(
        product_id=PRODUCT_ID,
        product_key="sales-order",
        version=12,
        display_name="Sales Order",
        description="Order analytics product.",
        product_facts=[
            ProductFactIR(kind="description", value="Order analytics product."),
            ProductFactIR(kind="purpose", value="Analyze customer purchase orders."),
            ProductFactIR(kind="domain", value="Sales"),
            ProductFactIR(kind="data_type", value="Transactional"),
            ProductFactIR(kind="storage_location", value="BigQuery analytics.sales"),
            ProductFactIR(kind="source_system", value="ERP"),
            ProductFactIR(kind="source_name", value="Order Management"),
            ProductFactIR(kind="tag", value="Orders"),
            ProductFactIR(kind="access", value="Approved analysts"),
            ProductFactIR(kind="security_classification", value="Internal"),
            ProductFactIR(kind="owner", value="Sales Data Office"),
            ProductFactIR(kind="contact", value="sales-data@example.com"),
            ProductFactIR(kind="consumer", value="Sales analysts"),
            ProductFactIR(kind="refresh_schedule", value="Daily at 02:00 UTC"),
            ProductFactIR(kind="freshness", value="Within 24 hours"),
            ProductFactIR(kind="sla", value="99.9% monthly availability"),
            ProductFactIR(
                kind="ai_readiness",
                value="Documented semantics and stable identifiers",
            ),
            ProductFactIR(kind="quality", value="Primary keys are validated"),
            ProductFactIR(
                kind="constraint",
                value="Tax is excluded from net revenue",
            ),
            ProductFactIR(kind="related_link", value="https://example.com/orders"),
        ],
        source_hashes={"dictionary_excel": "a" * 64, "semantic_document": "b" * 64},
        tables=[customers, orders],
        relationships=[
            RelationshipIR(
                relationship_id="rel_0198f6d1-2a11-78d1-8672-67d49e69f14c",
                name="orders_customer",
                from_table_id=ORDERS_TABLE_ID,
                to_table_id=CUSTOMERS_TABLE_ID,
                from_columns=["customer_id"],
                to_columns=["customer_id"],
                description="Each order belongs to one customer.",
                evidence=[dictionary_evidence(3, "customers.customer_id")],
            )
        ],
        metrics=[
            MetricIR(
                metric_id="met_0198f6d2-2a11-78d1-8672-67d49e69f14c",
                name="net_revenue",
                expression="SUM(orders.net_amount)",
                description="Revenue excluding tax.",
                synonyms=["net sales"],
                examples=["What was net revenue last month?"],
                evidence=[
                    Evidence(
                        source_hash="b" * 64,
                        role=SourceRole.SEMANTIC_DOCUMENT,
                        locator={"page": 2},
                        excerpt="Net revenue excludes tax.",
                    )
                ],
            )
        ],
    )
