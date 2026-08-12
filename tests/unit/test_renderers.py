from __future__ import annotations

from pathlib import Path

from ard_ossie.ir import ProductIR
from ard_ossie.renderers import (
    render_dictionary_json,
    render_product_markdown,
    render_semantic_markdown,
)

GOLDEN = Path(__file__).parents[1] / "golden" / "sales-order"


def golden(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def test_renderers_match_hand_authored_golden_files(resolved_sales_order_ir: ProductIR) -> None:
    assert render_product_markdown(resolved_sales_order_ir) == golden("data-product.md")
    assert render_semantic_markdown(resolved_sales_order_ir) == golden("data-semantic.md")
    assert render_dictionary_json(resolved_sales_order_ir) == golden("data-dictionary.json")


def test_renderer_output_is_stable_when_input_lists_are_reversed(
    resolved_sales_order_ir: ProductIR,
) -> None:
    baseline_dictionary = render_dictionary_json(resolved_sales_order_ir)
    baseline_product = render_product_markdown(resolved_sales_order_ir)
    reversed_ir = resolved_sales_order_ir.model_copy(
        update={
            "tables": list(reversed(resolved_sales_order_ir.tables)),
            "metrics": list(reversed(resolved_sales_order_ir.metrics)),
            "product_facts": list(reversed(resolved_sales_order_ir.product_facts)),
        }
    )

    assert render_dictionary_json(reversed_ir) == baseline_dictionary
    assert render_product_markdown(reversed_ir) == baseline_product


def test_product_renderer_omits_empty_optional_sections(
    resolved_sales_order_ir: ProductIR,
) -> None:
    sparse = resolved_sales_order_ir.model_copy(update={"product_facts": []})

    rendered = render_product_markdown(sparse)

    assert "## Parsed source" not in rendered
    assert "미제공" not in rendered
    assert "N/A" not in rendered
    assert "## Overview" not in rendered
    assert "## Data source" not in rendered
    assert "## Tags" not in rendered
    assert "## Access and security" not in rendered
    assert "## Ownership" not in rendered
    assert "## Freshness and SLA" not in rendered
    assert "## AI readiness and quality" not in rendered
    assert "## Constraints and notes" not in rendered
    assert "## Datasets" in rendered
