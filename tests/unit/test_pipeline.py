from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

import ard_ossie.pipeline as pipeline
from ard_ossie.docling_parser import Evidence, ParsedDocument
from ard_ossie.excel_adapter import DictionaryColumn, DictionaryTable, ParsedDictionary
from ard_ossie.impact import build_changeset
from ard_ossie.ingestion import SourceRole
from ard_ossie.llm import MetricSuggestion, ProductFactSuggestion
from ard_ossie.models import (
    MetricRecord,
    ProductRecord,
    ProductTableRef,
    TableLocator,
    TableRecord,
)
from ard_ossie.pipeline import (
    ProductConfig,
    SuggestionBatch,
    _resolve_tables,
    _shared_table_findings,
)
from ard_ossie.registry import Registry
from ard_ossie.semantic.models import SemanticStructureRepairRecord
from ard_ossie.semantic.repair import SemanticStructureRepairPlanner
from ard_ossie.versioning import plan_version

PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
OTHER_PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"


def trusted_repair_payload() -> dict[str, object]:
    return SemanticStructureRepairRecord(
        source_hash="a" * 64,
        ordered_span_hashes=[],
        parser_version="semantic-structure-v1",
        prompt_version="semantic-structure-repair-v1",
        schema_hash="b" * 64,
        provider="test-provider",
        model="test-model",
        outcome="degraded",
        plan=None,
        provider_error_code="LLM_PROVIDER_TRANSIENT_FAILED",
        validation_codes=[],
        applied_orders=[],
        rejected_orders=[],
        plan_hash=None,
    ).model_dump(mode="json")


def test_processing_parser_injects_planner_and_validated_trusted_record() -> None:
    provider = object()

    parser = pipeline._processing_parser(
        provider=provider,
        parser=None,
        trusted_semantic_repair=trusted_repair_payload(),
    )

    assert isinstance(parser._structure_repair_planner, SemanticStructureRepairPlanner)
    assert parser._structure_repair_planner._provider is provider
    assert parser._trusted_repair_record == SemanticStructureRepairRecord.model_validate(
        trusted_repair_payload()
    )


def test_processing_parser_leaves_custom_parser_untouched() -> None:
    custom = pipeline.DoclingParser()

    parser = pipeline._processing_parser(
        provider=object(),
        parser=custom,
        trusted_semantic_repair={"invalid": "ignored for a custom parser"},
    )

    assert parser is custom


def product_evidence(
    *,
    source_hash: str = "b" * 64,
    role: SourceRole = SourceRole.PRODUCT_HTML,
    excerpt: str | None = "사용자가 입력한 근거",
    item_index: int = 1,
) -> Evidence:
    return Evidence(
        source_hash=source_hash,
        role=role,
        locator={
            "document": "product-info/product.html",
            "item_index": item_index,
            "level": 2,
        },
        excerpt=excerpt,
    )


def product_document(
    *,
    excerpt: str = "사용자가 입력한 근거",
    evidence: list[Evidence] | None = None,
    excluded_evidence: list[Evidence] | None = None,
) -> ParsedDocument:
    return ParsedDocument(
        role=SourceRole.PRODUCT_HTML,
        source_hash="b" * 64,
        markdown="# Product",
        evidence=[product_evidence(excerpt=excerpt)] if evidence is None else evidence,
        excluded_product_fact_evidence=excluded_evidence or [],
    )


def metric_drafts(
    root: Path,
    *,
    include_sales_order: bool = False,
) -> list[pipeline._TableDraft]:
    source_hash = "c" * 64
    tables = [
        DictionaryTable(
            locator="warehouse|analytics|marketing|marketing_campaign",
            columns=[
                DictionaryColumn(
                    ordinal=ordinal,
                    name=name,
                    data_type=data_type,
                    nullable=False,
                    primary_key=name == "campaign_id",
                    evidence=Evidence(
                        source_hash=source_hash,
                        role=SourceRole.DICTIONARY_EXCEL,
                        locator={
                            "sheet": "Dictionary",
                            "range": f"A{ordinal + 1}:H{ordinal + 1}",
                        },
                        excerpt=name,
                    ),
                )
                for ordinal, (name, data_type) in enumerate(
                    (
                        ("campaign_id", "STRING"),
                        ("status", "STRING"),
                        ("revenue", "NUMERIC"),
                        ("engagement_count", "INTEGER"),
                        ("impression_count", "INTEGER"),
                    ),
                    start=1,
                )
            ],
        )
    ]
    if include_sales_order:
        tables.append(
            DictionaryTable(
                locator="warehouse|analytics|sales|sales_order",
                columns=[
                    DictionaryColumn(
                        ordinal=ordinal,
                        name=name,
                        data_type=data_type,
                        nullable=False,
                        primary_key=name == "order_id",
                        evidence=Evidence(
                            source_hash=source_hash,
                            role=SourceRole.DICTIONARY_EXCEL,
                            locator={
                                "sheet": "Dictionary",
                                "range": f"A{ordinal + 10}:H{ordinal + 10}",
                            },
                            excerpt=name,
                        ),
                    )
                    for ordinal, (name, data_type) in enumerate(
                        (
                            ("order_id", "STRING"),
                            ("campaign_id", "STRING"),
                            ("cost", "NUMERIC"),
                        ),
                        start=1,
                    )
                ],
            )
        )
    dictionary = ParsedDictionary(
        source_hash=source_hash,
        tables=tables,
    )
    return _resolve_tables(
        ProductConfig(
            operation="create",
            product_id=PRODUCT_ID,
            product_key="campaign-performance",
            version=1,
            display_name="Campaign Performance",
        ),
        dictionary,
        Registry(root / "registry"),
    )


def metric_suggestion(
    expression: str,
    *,
    dataset_names: list[str] | None = None,
    name: str = "Campaign Count",
    confidence: float = 0.9,
) -> MetricSuggestion:
    return MetricSuggestion(
        name=name,
        expression=expression,
        dataset_names=dataset_names or ["marketing_campaign"],
        description="Campaign performance metric",
        synonyms=[],
        confidence=confidence,
        evidence=[
            Evidence(
                source_hash="a" * 64,
                role=SourceRole.SEMANTIC_DOCUMENT,
                locator={"document": "semantic/metrics.docx", "page": 1},
                excerpt="Campaign performance metric definition",
            )
        ],
    )


def reserved_metric_drafts(root: Path) -> list[pipeline._TableDraft]:
    draft = metric_drafts(root)[0]
    return [
        draft.model_copy(
            update={
                "locator": draft.locator.model_copy(
                    update={"schema_name": "reserved", "table_name": "order"}
                ),
                "columns": [
                    draft.columns[0].model_copy(update={"name": "select"})
                ],
            }
        )
    ]


def duplicate_leaf_metric_drafts(root: Path) -> list[pipeline._TableDraft]:
    first = metric_drafts(root)[0]
    second = first.model_copy(
        deep=True,
        update={
            "table_id": "tbl_0198f6ca-2a11-78d1-8672-67d49e69f15a",
            "locator": first.locator.model_copy(update={"schema_name": "duplicate"}),
        },
    )
    return [first, second]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        pytest.param(
            "COUNT(DISTINCT campaign_id)",
            "COUNT(DISTINCT marketing_campaign.campaign_id)",
            id="distinct-bare-column",
        ),
        pytest.param(
            "SUM(CASE WHEN status = 'active' THEN revenue ELSE 0 END)",
            (
                "SUM(CASE WHEN marketing_campaign.status = 'active' "
                "THEN marketing_campaign.revenue ELSE 0 END)"
            ),
            id="case-expression",
        ),
        pytest.param(
            "ROUND(SUM(revenue) / NULLIF(COUNT(*), 0), 2)",
            (
                "ROUND(SUM(marketing_campaign.revenue) / "
                "NULLIF(COUNT(*), 0), 2)"
            ),
            id="nested-functions-and-star",
        ),
        pytest.param(
            "COUNT(DISTINCT MARKETING_CAMPAIGN.CAMPAIGN_ID)",
            "COUNT(DISTINCT marketing_campaign.campaign_id)",
            id="canonical-identifier-spelling",
        ),
        pytest.param("COUNT(*)", "COUNT(*)", id="star-only"),
    ],
)
def test_prepare_metrics_qualifies_single_dataset_columns_without_mutating_raw(
    tmp_path: Path,
    expression: str,
    expected: str,
) -> None:
    raw = metric_suggestion(expression)

    prepared = pipeline._prepare_metrics([raw], metric_drafts(tmp_path))

    assert [item.expression for item in prepared.suggestions] == [expected]
    assert prepared.findings == []
    assert raw.expression == expression


def test_prepare_metrics_casts_integral_division_numerator(tmp_path: Path) -> None:
    raw = metric_suggestion(
        "SUM(engagement_count) / NULLIF(SUM(impression_count), 0)",
        name="Engagement Rate",
    )

    prepared = pipeline._prepare_metrics([raw], metric_drafts(tmp_path))

    assert prepared.suggestions[0].expression == (
        "CAST(SUM(marketing_campaign.engagement_count) AS DECIMAL(38, 12)) / "
        "NULLIF(SUM(marketing_campaign.impression_count), 0)"
    )
    assert raw.expression == (
        "SUM(engagement_count) / NULLIF(SUM(impression_count), 0)"
    )


@pytest.mark.parametrize(
    ("name", "expression", "dataset_names", "expected_code"),
    [
        pytest.param(
            " ",
            "COUNT(campaign_id)",
            ["marketing_campaign"],
            "LLM_METRIC_NAME_OR_EXPRESSION_EMPTY",
            id="blank-name",
        ),
        pytest.param(
            "Campaign Count",
            " ",
            ["marketing_campaign"],
            "LLM_METRIC_NAME_OR_EXPRESSION_EMPTY",
            id="blank-expression",
        ),
        pytest.param(
            "Campaign Count",
            "COUNT(campaign_id)",
            [" "],
            "LLM_METRIC_DATASET_EMPTY",
            id="blank-dataset",
        ),
        pytest.param(
            "Campaign Count",
            "COUNT(campaign_id)",
            ["marketing_campaign", "MARKETING_CAMPAIGN"],
            "LLM_METRIC_DATASET_DUPLICATE",
            id="duplicate-dataset",
        ),
        pytest.param(
            "Campaign Count",
            "COUNT(campaign_id)",
            ["unknown_dataset"],
            "LLM_METRIC_DATASET_UNKNOWN",
            id="unknown-dataset",
        ),
        pytest.param(
            "Campaign Count",
            "COUNT(missing_column)",
            ["marketing_campaign"],
            "LLM_METRIC_REFERENCE_UNKNOWN",
            id="unknown-column",
        ),
        pytest.param(
            "Campaign Count",
            "COUNT(sales_order.campaign_id)",
            ["marketing_campaign"],
            "LLM_METRIC_REFERENCE_UNKNOWN",
            id="undeclared-qualifier",
        ),
        pytest.param(
            "Campaign Count",
            "COUNT(marketing.marketing_campaign.campaign_id)",
            ["marketing_campaign"],
            "LLM_METRIC_REFERENCE_UNKNOWN",
            id="undeclared-parent-namespace",
        ),
        pytest.param(
            "Campaign Count",
            "COUNT(campaign_id); COUNT(status)",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="multiple-statements",
        ),
        pytest.param(
            "Campaign Count",
            "SELECT COUNT(campaign_id) FROM marketing_campaign",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="query",
        ),
        pytest.param(
            "Campaign Count",
            "1 + (SELECT COUNT(campaign_id) FROM marketing_campaign)",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="subquery",
        ),
        pytest.param(
            "Campaign Count",
            "DELETE FROM marketing_campaign",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="mutation",
        ),
        pytest.param(
            "Campaign Count",
            "CREATE TABLE copied AS SELECT * FROM marketing_campaign",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="ddl",
        ),
        pytest.param(
            "Campaign Count",
            "SHOW TABLES",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="command",
        ),
        pytest.param(
            "Campaign Count",
            "SET x = 1",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="set-statement",
        ),
        pytest.param(
            "Campaign Count",
            "USE private_catalog",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="use-statement",
        ),
        pytest.param(
            "Campaign Count",
            "DESCRIBE private_table",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="describe-statement",
        ),
        pytest.param(
            "Campaign Count",
            "PRAGMA private_setting(x)",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="pragma-statement",
        ),
        pytest.param(
            "Campaign Count",
            "TRUNCATE TABLE marketing_campaign",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="truncate-statement",
        ),
        pytest.param(
            "Campaign Count",
            "ALTER TABLE marketing_campaign ADD COLUMN x INT",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="alter-statement",
        ),
        pytest.param(
            "Campaign Count",
            "GRANT SELECT ON marketing_campaign TO analyst",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="grant-statement",
        ),
        pytest.param(
            "Campaign Count",
            "BEGIN",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="begin-statement",
        ),
        pytest.param(
            "Campaign Count",
            "COMMIT",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="commit-statement",
        ),
        pytest.param(
            "Campaign Count",
            "ROLLBACK",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="rollback-statement",
        ),
        pytest.param(
            "Campaign Count",
            "VALUES (1)",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="values-relation",
        ),
        pytest.param(
            "Campaign Count",
            "UNNEST(ARRAY(1, 2))",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="unnest-table-function",
        ),
        pytest.param(
            "Campaign Count",
            "EXPLODE(ARRAY(1, 2))",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="explode-table-function",
        ),
        pytest.param(
            "Campaign Count",
            "JSON_TABLE('{\"x\":1}', '$' COLUMNS (x INT PATH '$.x'))",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_UNSAFE",
            id="json-table-function",
        ),
        pytest.param(
            "Campaign Count",
            "SUM(",
            ["marketing_campaign"],
            "LLM_METRIC_SQL_INVALID",
            id="parse-error",
        ),
    ],
)
def test_prepare_metrics_rejects_invalid_or_unsafe_provider_output(
    tmp_path: Path,
    name: str,
    expression: str,
    dataset_names: list[str],
    expected_code: str,
) -> None:
    suggestion = metric_suggestion(
        expression,
        dataset_names=dataset_names,
        name=name,
        confidence=0.1,
    )

    with pytest.raises(ValueError, match=f"^{expected_code}$"):
        pipeline._prepare_metrics([suggestion], metric_drafts(tmp_path))


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(marketing_campaign.revenue) / SUM(sales_order.cost)",
        "COUNT(campaign_id)",
    ],
    ids=["qualified-columns", "unqualified-column-in-both-datasets"],
)
def test_prepare_metrics_excludes_valid_multi_dataset_metric_with_one_warning(
    tmp_path: Path,
    expression: str,
) -> None:
    raw = metric_suggestion(
        expression,
        dataset_names=["marketing_campaign", "sales_order"],
        name="Modeled Efficiency",
    )

    prepared = pipeline._prepare_metrics(
        [raw],
        metric_drafts(tmp_path, include_sales_order=True),
    )

    assert prepared.suggestions == []
    assert prepared.excluded_names == ["Modeled Efficiency"]
    assert prepared.findings == [
        pipeline.QualityFinding(
            code="METRIC_MULTI_DATASET_UNSUPPORTED",
            path="metrics.Modeled Efficiency",
            message=(
                "Metric uses multiple datasets and was excluded because join path, "
                "cardinality, and grain are not declared"
            ),
        )
    ]
    assert raw.expression == expression


def test_prepare_metrics_rejects_unknown_reference_before_multi_dataset_exclusion(
    tmp_path: Path,
) -> None:
    suggestion = metric_suggestion(
        "SUM(marketing_campaign.revenue) / SUM(unknown_table.cost)",
        dataset_names=["marketing_campaign", "sales_order"],
        name="Modeled Efficiency",
        confidence=0.1,
    )

    with pytest.raises(ValueError, match="^LLM_METRIC_REFERENCE_UNKNOWN$"):
        pipeline._prepare_metrics(
            [suggestion],
            metric_drafts(tmp_path, include_sales_order=True),
        )


@pytest.mark.parametrize(
    "expression",
    ["'private_token", '"private_token', "/* private_token"],
    ids=["string", "identifier", "comment"],
)
def test_prepare_metrics_classifies_tokenizer_failures_as_invalid_output(
    tmp_path: Path,
    expression: str,
) -> None:
    with pytest.raises(ValueError, match="^LLM_METRIC_SQL_INVALID$"):
        pipeline._prepare_metrics(
            [metric_suggestion(expression)],
            metric_drafts(tmp_path),
        )


def test_prepare_metrics_does_not_log_raw_command_fallback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="sqlglot")

    with pytest.raises(ValueError, match="^LLM_METRIC_SQL_UNSAFE$"):
        pipeline._prepare_metrics(
            [metric_suggestion("SHOW PRIVATE_PROVIDER_VALUE")],
            metric_drafts(tmp_path),
        )

    assert "PRIVATE_PROVIDER_VALUE" not in caplog.text


def test_prepare_metrics_quotes_canonical_reserved_identifiers(tmp_path: Path) -> None:
    raw = metric_suggestion(
        'COUNT("order"."select")',
        dataset_names=["order"],
    )

    prepared = pipeline._prepare_metrics([raw], reserved_metric_drafts(tmp_path))

    assert prepared.suggestions[0].expression == 'COUNT("order"."select")'


def test_metric_dataset_catalog_rejects_duplicate_leaf_names_before_provider_call(
    tmp_path: Path,
) -> None:
    class RejectCallProvider:
        def generate_structured(self, *, schema, messages):
            raise AssertionError("provider must not receive an ambiguous dataset catalog")

    with pytest.raises(
        pipeline.PipelineValidationError,
        match="^METRIC_DATASET_NAME_AMBIGUOUS$",
    ):
        pipeline._extract_suggestions(
            RejectCallProvider(),
            product_document(),
            ParsedDocument(
                role=SourceRole.SEMANTIC_DOCUMENT,
                source_hash="a" * 64,
                markdown="# Semantic",
            ),
            duplicate_leaf_metric_drafts(tmp_path),
        )


def test_prepare_metrics_rejects_accepted_and_excluded_name_collision(
    tmp_path: Path,
) -> None:
    suggestions = [
        metric_suggestion(
            "SUM(revenue)",
            name="Modeled Efficiency",
        ),
        metric_suggestion(
            "SUM(marketing_campaign.revenue) / SUM(sales_order.cost)",
            dataset_names=["marketing_campaign", "sales_order"],
            name="Modeled Efficiency",
        ),
    ]

    with pytest.raises(ValueError, match="^LLM_METRIC_NAME_DUPLICATE$"):
        pipeline._prepare_metrics(
            suggestions,
            metric_drafts(tmp_path, include_sales_order=True),
        )


def test_build_metrics_rejects_existing_alias_split_across_accepted_and_excluded(
    tmp_path: Path,
) -> None:
    prepared = pipeline._prepare_metrics(
        [
            metric_suggestion(
                "SUM(revenue)",
                name="Modeled Efficiency",
            ),
            metric_suggestion(
                "SUM(marketing_campaign.revenue) / SUM(sales_order.cost)",
                dataset_names=["marketing_campaign", "sales_order"],
                name="Legacy Efficiency",
            ),
        ],
        metric_drafts(tmp_path, include_sales_order=True),
    )
    existing = ProductRecord(
        product_id=PRODUCT_ID,
        product_key="campaign-performance",
        version=1,
        metrics=[
            MetricRecord(
                metric_id="met_0198f6d2-2a11-78d1-8672-67d49e69f15a",
                name="Legacy Efficiency",
                aliases=["Modeled Efficiency"],
            )
        ],
    )

    with pytest.raises(ValueError, match="^LLM_METRIC_NAME_DUPLICATE$"):
        pipeline._build_metrics(
            prepared.suggestions,
            existing,
            excluded_names=prepared.excluded_names,
        )


def product_fact(
    kind: str,
    value: str,
    *,
    confidence: float = 0.9,
    evidence_ids: list[str] | None = None,
) -> ProductFactSuggestion:
    return ProductFactSuggestion(
        kind=kind,
        value=value,
        confidence=confidence,
        evidence_ids=evidence_ids or ["product-evidence-000001"],
    )


def test_product_evidence_catalog_assigns_stable_request_local_ids() -> None:
    first = product_evidence(item_index=1)
    second = product_evidence(item_index=2)
    document = product_document(evidence=[first, second])

    assert pipeline._product_evidence_catalog(document) == {
        "product-evidence-000001": first,
        "product-evidence-000002": second,
    }


def test_product_prompt_assigns_ids_only_to_accepted_evidence(tmp_path: Path) -> None:
    accepted = product_evidence(excerpt="사용자 설명")
    excluded = product_evidence(excerpt="(AI 자동생성) 요약", item_index=2)

    class CapturingProvider:
        payload: dict[str, object] | None = None
        system_message: str | None = None

        def generate_structured(self, *, schema, messages):
            self.payload = json.loads(messages[1]["content"])
            self.system_message = messages[0]["content"]
            return {"suggestions": [], "metrics": [], "product_facts": []}

    provider = CapturingProvider()
    pipeline._extract_suggestions(
        provider,
        product_document(evidence=[accepted], excluded_evidence=[excluded]),
        ParsedDocument(
            role=SourceRole.SEMANTIC_DOCUMENT,
            source_hash="a" * 64,
            markdown="# Semantic",
        ),
        metric_drafts(tmp_path),
    )

    assert provider.payload is not None
    product_payload = provider.payload["product"]
    assert isinstance(product_payload, dict)
    assert product_payload["evidence"] == [
        {
            "evidence_id": "product-evidence-000001",
            **accepted.model_dump(mode="json"),
        }
    ]
    assert "excluded_product_fact_evidence" not in product_payload
    assert provider.payload["datasets"] == [
        {
            "dataset_name": "marketing_campaign",
            "columns": [
                "campaign_id",
                "status",
                "revenue",
                "engagement_count",
                "impression_count",
            ],
        }
    ]
    assert provider.system_message is not None
    assert "exact dataset names" in provider.system_message
    assert "dataset_names" in provider.system_message


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
    ("evidence", "expected_code"),
    [
        pytest.param(
            product_evidence(role=SourceRole.SEMANTIC_DOCUMENT),
            "LLM_PRODUCT_FACT_EVIDENCE_ROLE_INVALID",
            id="role",
        ),
        pytest.param(
            product_evidence(source_hash="c" * 64),
            "LLM_PRODUCT_FACT_EVIDENCE_SOURCE_UNKNOWN",
            id="source-hash",
        ),
        pytest.param(
            product_evidence(excerpt=" \n "),
            "LLM_PRODUCT_FACT_EVIDENCE_EXCERPT_REQUIRED",
            id="excerpt",
        ),
    ],
)
def test_product_facts_require_product_html_evidence(
    evidence: Evidence,
    expected_code: str,
) -> None:
    with pytest.raises(ValueError, match=f"^{expected_code}$"):
        pipeline._validate_product_facts(
            [product_fact("purpose", "주문 분석")],
            product_document(evidence=[evidence]),
            configured_description=None,
        )


def test_product_facts_reject_unknown_evidence_id() -> None:
    with pytest.raises(ValueError, match="^LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN$"):
        pipeline._validate_product_facts(
            [
                product_fact(
                    "purpose",
                    "주문 분석",
                    evidence_ids=["product-evidence-999999"],
                )
            ],
            product_document(),
            configured_description=None,
        )


def test_product_facts_reject_unknown_evidence_id_below_acceptance_threshold() -> None:
    with pytest.raises(ValueError, match="^LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN$"):
        pipeline._validate_product_facts(
            [
                product_fact(
                    "purpose",
                    "주문 분석",
                    confidence=0.69,
                    evidence_ids=["product-evidence-999999"],
                )
            ],
            product_document(),
            configured_description=None,
        )


def test_product_facts_reject_duplicate_evidence_id() -> None:
    with pytest.raises(
        ValueError,
        match="^LLM_PRODUCT_FACT_EVIDENCE_ID_DUPLICATE$",
    ):
        pipeline._validate_product_facts(
            [
                product_fact(
                    "purpose",
                    "주문 분석",
                    evidence_ids=[
                        "product-evidence-000001",
                        "product-evidence-000001",
                    ],
                )
            ],
            product_document(),
            configured_description=None,
        )


def test_product_facts_reject_ai_generated_summary_evidence() -> None:
    evidence = product_evidence(
        excerpt="사용자가 작성하지 않은 자동 요약 값",
        item_index=63,
    )

    with pytest.raises(ValueError, match="^LLM_PRODUCT_FACT_EVIDENCE_AI_GENERATED$"):
        pipeline._validate_product_facts(
            [product_fact("description", "사용자가 작성하지 않은 자동 요약 값")],
            product_document(evidence=[evidence], excluded_evidence=[evidence]),
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
