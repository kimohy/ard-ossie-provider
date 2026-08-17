from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from openpyxl import Workbook

from ard_ossie.impact import build_changeset
from ard_ossie.models import ProductRecord, ProductTableRef
from ard_ossie.pipeline import PipelineValidationError, process_product
from ard_ossie.registry import Registry
from tests.integration.test_cli_process import create_product_fixture

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
OTHER_PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
ORDERS_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
CUSTOMERS_TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14d"
CHANGESET_ID = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class ProviderMustNotRun:
    def generate_structured(self, **_kwargs):
        raise AssertionError("provider must not run before table baseline validation")


def write_two_table_dictionary(product: Path, *, order_description: str) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "platform",
            "catalog",
            "schema",
            "table",
            "column",
            "data_type",
            "nullable",
            "pk",
            "description",
        ]
    )
    sheet.append(
        [
            "erp",
            "analytics",
            "sales",
            "orders",
            "order_id",
            "INT64",
            "false",
            "true",
            order_description,
        ]
    )
    sheet.append(
        [
            "erp",
            "analytics",
            "sales",
            "customers",
            "customer_id",
            "INT64",
            "false",
            "true",
            "Stable customer identifier",
        ]
    )
    workbook.save(product / "sources" / "dictionary" / "dictionary.xlsx")


def configure_two_tables(product: Path) -> None:
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["tables"] = [
        {
            "locator": "erp|analytics|sales|orders",
            "table_id": ORDERS_TABLE_ID,
            "version": 1,
            "usage": "SOURCE",
        },
        {
            "locator": "erp|analytics|sales|customers",
            "table_id": CUSTOMERS_TABLE_ID,
            "version": 1,
            "usage": "SOURCE",
        },
    ]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def configure_changeset_update(product: Path) -> None:
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config.update(
        {
            "operation": "update",
            "version": 2,
            "base_version": 1,
            "changeset_id": CHANGESET_ID,
        }
    )
    config["tables"][0].update({"version": 2, "base_version": 1})
    config["tables"][1].update({"version": 1, "base_version": 1})
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def test_existing_product_requires_a_baseline_before_provider_or_mutation(
    tmp_path: Path,
) -> None:
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    before = {
        "generated": tree_hash(product / "generated"),
        "registry": tree_hash(registry),
    }

    with pytest.raises(PipelineValidationError) as captured:
        process_product(
            product,
            registry_root=registry,
            provider=ProviderMustNotRun(),
        )

    assert str(captured.value) == "TABLE_BASELINE_REQUIRED"
    assert tree_hash(product / "generated") == before["generated"]
    assert tree_hash(registry) == before["registry"]


@pytest.mark.parametrize("mutation", ["malformed", "wrong_product", "missing_table"])
def test_existing_product_rejects_invalid_baseline_without_disclosing_input(
    tmp_path: Path,
    mutation: str,
) -> None:
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    baseline_path = product / "generated" / "data-dictionary.json"
    if mutation == "malformed":
        baseline = b'{"secret marker":"do-not-log"'
    else:
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        if mutation == "wrong_product":
            payload["product_id"] = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
        else:
            payload["tables"] = []
        baseline = json.dumps(payload).encode()
    before = {
        "generated": tree_hash(product / "generated"),
        "registry": tree_hash(registry),
    }

    with pytest.raises(PipelineValidationError) as captured:
        process_product(
            product,
            registry_root=registry,
            provider=ProviderMustNotRun(),
            table_baseline=baseline,
        )

    assert str(captured.value) == "TABLE_BASELINE_INVALID"
    assert "secret marker" not in str(captured.value)
    assert "do-not-log" not in str(captured.value)
    assert tree_hash(product / "generated") == before["generated"]
    assert tree_hash(registry) == before["registry"]


def test_one_workbook_cell_change_versions_only_its_changeset_table(
    tmp_path: Path,
) -> None:
    product = create_product_fixture(tmp_path)
    configure_two_tables(product)
    write_two_table_dictionary(product, order_description="Original order identifier")
    registry_path = tmp_path / "registry"
    process_product(product, registry_root=registry_path)
    baseline = (product / "generated" / "data-dictionary.json").read_bytes()

    registry = Registry.load(registry_path)
    existing_orders = registry.get_table(ORDERS_TABLE_ID)
    existing_customers = registry.get_table(CUSTOMERS_TABLE_ID)
    assert existing_orders is not None
    assert existing_customers is not None
    registry.write_table(existing_orders.model_copy(update={"canonical_hash": "1" * 64}))
    registry.write_table(existing_customers.model_copy(update={"canonical_hash": "2" * 64}))
    registry.write_product(
        ProductRecord(
            product_id=OTHER_PRODUCT_ID,
            product_key="finance-order",
            version=1,
        )
    )
    registry.write_mappings(
        OTHER_PRODUCT_ID,
        [
            ProductTableRef(
                link_id="lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330ec4",
                product_id=OTHER_PRODUCT_ID,
                table_id=ORDERS_TABLE_ID,
                table_version=1,
                usage="SOURCE",
            ),
            ProductTableRef(
                link_id="lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330ec5",
                product_id=OTHER_PRODUCT_ID,
                table_id=CUSTOMERS_TABLE_ID,
                table_version=1,
                usage="SOURCE",
            ),
        ],
    )
    registry.write_changeset(
        build_changeset(
            [ORDERS_TABLE_ID],
            [PRODUCT_ID, OTHER_PRODUCT_ID],
            changeset_id=CHANGESET_ID,
        )
    )
    configure_changeset_update(product)
    write_two_table_dictionary(product, order_description="Changed order identifier")

    result = process_product(
        product,
        registry_root=registry_path,
        table_baseline=baseline,
        pr_number=7,
    )

    assert result.quality_report.hard_errors == []
    versions = json.loads(
        (product / "quality" / "version-report.json").read_text(encoding="utf-8")
    )
    assert [(item["changed"], item["proposed_version"]) for item in versions] == [
        (True, 2),
        (False, 1),
        (True, 2),
    ]
    promoted = Registry.load(registry_path)
    promoted_orders = promoted.get_table(ORDERS_TABLE_ID)
    promoted_customers = promoted.get_table(CUSTOMERS_TABLE_ID)
    assert promoted_orders is not None and promoted_orders.version == 2
    assert promoted_orders.canonical_hash != "1" * 64
    assert promoted_customers is not None and promoted_customers.version == 1
    assert promoted_customers.canonical_hash == "2" * 64
    dictionary = json.loads(
        (product / "generated" / "data-dictionary.json").read_text(encoding="utf-8")
    )
    assert dictionary["tables"][0]["columns"][0]["description"] == (
        "Changed order identifier"
    )
