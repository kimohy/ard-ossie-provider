from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import validate

from ard_ossie.ir import ProductIR
from ard_ossie.ossie_compiler import compile_ossie, load_ossie_011_schema

SCHEMA_DIR = Path(__file__).parents[2] / "schemas" / "ossie" / "0.1.1"


def test_vendored_schema_matches_recorded_upstream_checksum() -> None:
    expected = (SCHEMA_DIR / "SHA256SUMS").read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256((SCHEMA_DIR / "osi-schema.json").read_bytes()).hexdigest()

    assert actual == expected
    assert actual == "c1e9adec39562786aa78809665fba568797b15f4c53a0847d9cbcf2dead1bc94"


def test_compiler_emits_schema_valid_ossie_011_with_internal_ids(
    resolved_sales_order_ir: ProductIR,
) -> None:
    model = compile_ossie(resolved_sales_order_ir)

    validate(instance=model, schema=load_ossie_011_schema())
    assert model["version"] == "0.1.1"
    semantic_model = model["semantic_model"][0]
    product_extension = json.loads(semantic_model["custom_extensions"][0]["data"])
    assert product_extension == {
        "id": "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631",
        "kind": "product",
        "namespace": "ai_ready_data",
        "version": 12,
    }
    assert [dataset["name"] for dataset in semantic_model["datasets"]] == [
        "orders",
        "customers",
    ]
    assert semantic_model["metrics"][0]["expression"] == {
        "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.net_amount)"}]
    }


def test_compiler_rejects_relationship_to_unresolved_table(
    resolved_sales_order_ir: ProductIR,
) -> None:
    relationship = resolved_sales_order_ir.relationships[0].model_copy(
        update={"to_table_id": "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14e"}
    )
    invalid = resolved_sales_order_ir.model_copy(update={"relationships": [relationship]})

    with pytest.raises(ValueError, match="OSSIE_UNRESOLVED_REFERENCE"):
        compile_ossie(invalid)
