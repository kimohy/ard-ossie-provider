from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import ard_ossie.pipeline as pipeline
from ard_ossie.docling_parser import Evidence, ParsedDocument
from ard_ossie.excel_adapter import DictionaryColumn, DictionaryTable, ParsedDictionary
from ard_ossie.impact import build_changeset
from ard_ossie.ingestion import SourceRole
from ard_ossie.llm import ProductFactSuggestion
from ard_ossie.models import ProductRecord, ProductTableRef, TableLocator, TableRecord
from ard_ossie.pipeline import (
    ProductConfig,
    SuggestionBatch,
    _resolve_tables,
    _shared_table_findings,
)
from ard_ossie.registry import Registry
from ard_ossie.versioning import plan_version

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
OTHER_PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"


def product_document(
    *,
    excerpt: str = "사용자가 입력한 근거",
    excluded_evidence: list[Evidence] | None = None,
) -> ParsedDocument:
    return ParsedDocument(
        role=SourceRole.PRODUCT_HTML,
        source_hash="b" * 64,
        markdown="# Product",
        evidence=[
            Evidence(
                source_hash="b" * 64,
                role=SourceRole.PRODUCT_HTML,
                locator={
                    "document": "product-info/product.html",
                    "item_index": 1,
                    "level": 2,
                },
                excerpt=excerpt,
            )
        ],
        excluded_product_fact_evidence=excluded_evidence or [],
    )


def product_fact(
    kind: str,
    value: str,
    *,
    confidence: float = 0.9,
    source_hash: str = "b" * 64,
    role: SourceRole = SourceRole.PRODUCT_HTML,
    excerpt: str | None = "사용자가 입력한 근거",
) -> ProductFactSuggestion:
    return ProductFactSuggestion(
        kind=kind,
        value=value,
        confidence=confidence,
        evidence=[
            Evidence(
                source_hash=source_hash,
                role=role,
                locator={
                    "document": "product-info/product.html",
                    "item_index": 1,
                    "level": 2,
                },
                excerpt=excerpt,
            )
        ],
    )


def test_product_facts_omit_low_confidence_and_sort_deduplicated_repeated_values() -> None:
    facts = pipeline._validate_product_facts(
        [
            product_fact("tag", "Finance"),
            product_fact("tag", "finance"),
            product_fact("tag", "Analytics"),
            product_fact("quality", "검증 예정", confidence=0.69),
        ],
        product_document(),
        configured_description=None,
    )

    assert [(fact.kind, fact.value) for fact in facts] == [
        ("tag", "Analytics"),
        ("tag", "Finance"),
    ]


def test_product_facts_reject_conflicting_singleton_values() -> None:
    with pytest.raises(ValueError, match="^LLM_PRODUCT_FACT_SINGLETON_CONFLICT$"):
        pipeline._validate_product_facts(
            [
                product_fact("purpose", "주문 분석"),
                product_fact("purpose", "수요 예측"),
            ],
            product_document(),
            configured_description=None,
        )


@pytest.mark.parametrize(
    ("fact", "expected_code"),
    [
        pytest.param(
            product_fact("purpose", "주문 분석", role=SourceRole.SEMANTIC_DOCUMENT),
            "LLM_PRODUCT_FACT_EVIDENCE_ROLE_INVALID",
            id="role",
        ),
        pytest.param(
            product_fact("purpose", "주문 분석", source_hash="c" * 64),
            "LLM_PRODUCT_FACT_EVIDENCE_SOURCE_UNKNOWN",
            id="source-hash",
        ),
        pytest.param(
            product_fact("purpose", "주문 분석", excerpt=" \n "),
            "LLM_PRODUCT_FACT_EVIDENCE_EXCERPT_REQUIRED",
            id="excerpt",
        ),
    ],
)
def test_product_facts_require_product_html_evidence(
    fact: ProductFactSuggestion,
    expected_code: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{expected_code}$"):
        pipeline._validate_product_facts(
            [fact],
            product_document(),
            configured_description=None,
        )


@pytest.mark.parametrize(
    "fact",
    [
        pytest.param(
            product_fact("purpose", "주문 분석", excerpt="원문에 없는 근거"),
            id="invented-excerpt",
        ),
        pytest.param(
            product_fact("purpose", "주문 분석").model_copy(
                update={
                    "evidence": [
                        Evidence(
                            source_hash="b" * 64,
                            role=SourceRole.PRODUCT_HTML,
                            locator={
                                "document": "product-info/product.html",
                                "item_index": 999,
                                "level": 2,
                            },
                            excerpt="사용자가 입력한 근거",
                        )
                    ]
                }
            ),
            id="invented-locator",
        ),
    ],
)
def test_product_facts_reject_evidence_not_collected_from_product_document(
    fact: ProductFactSuggestion,
) -> None:
    with pytest.raises(ValueError, match="^LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN$"):
        pipeline._validate_product_facts(
            [fact],
            product_document(),
            configured_description=None,
        )


def test_product_facts_reject_ai_generated_summary_evidence() -> None:
    evidence = Evidence(
        source_hash="b" * 64,
        role=SourceRole.PRODUCT_HTML,
        locator={
            "document": "product-info/product.html",
            "item_index": 63,
            "level": 5,
        },
        excerpt="사용자가 작성하지 않은 자동 요약 값",
    )
    fact = product_fact("description", "사용자가 작성하지 않은 자동 요약 값").model_copy(
        update={"evidence": [evidence]}
    )

    with pytest.raises(ValueError, match="^LLM_PRODUCT_FACT_EVIDENCE_AI_GENERATED$"):
        pipeline._validate_product_facts(
            [fact],
            product_document(excluded_evidence=[evidence]),
            configured_description=None,
        )


def test_product_fact_deduplication_is_independent_of_provider_order() -> None:
    forward = [product_fact("tag", "Finance"), product_fact("tag", "finance")]
    reverse = list(reversed(forward))

    first = pipeline._validate_product_facts(
        forward,
        product_document(),
        configured_description=None,
    )
    second = pipeline._validate_product_facts(
        reverse,
        product_document(),
        configured_description=None,
    )

    assert first == second
    assert [fact.value for fact in first] == ["Finance"]


def test_suggestion_batch_requires_every_structured_output_collection() -> None:
    with pytest.raises(ValidationError):
        SuggestionBatch.model_validate({"suggestions": [], "metrics": []})


def test_configured_description_is_authoritative_document_fact() -> None:
    facts = pipeline._validate_product_facts(
        [
            product_fact("description", "HTML에서 추출한 설명"),
            product_fact("purpose", "주문 분석 지원"),
        ],
        product_document(),
        configured_description="설정에서 작성한 설명",
    )

    assert [(fact.kind, fact.value) for fact in facts] == [
        ("description", "설정에서 작성한 설명"),
        ("purpose", "주문 분석 지원"),
    ]
    assert facts[0].evidence == []
    assert facts[1].evidence[0].role is SourceRole.PRODUCT_HTML


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


def test_dictionary_table_description_seeds_pipeline_draft(tmp_path: Path) -> None:
    dictionary = ParsedDictionary(
        source_hash="a" * 64,
        tables=[
            DictionaryTable(
                locator="unspecified|synthetic_workspace|marketing_insight|marketing_campaign",
                description="가상 캠페인 합성 테이블",
                columns=[
                    DictionaryColumn(
                        ordinal=1,
                        name="campaign_id",
                        data_type="STRING",
                        nullable=False,
                        primary_key=True,
                        evidence=Evidence(
                            source_hash="a" * 64,
                            role=SourceRole.DICTIONARY_EXCEL,
                            locator={"sheet": "marketing_campaign", "range": "B14:J14"},
                            excerpt="campaign_id",
                        ),
                    )
                ],
            )
        ],
    )

    drafts = _resolve_tables(config(), dictionary, Registry(tmp_path / "registry"))

    assert drafts[0].description == "가상 캠페인 합성 테이블"
