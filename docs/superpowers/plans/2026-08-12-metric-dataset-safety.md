# Metric Dataset Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate and normalize every LLM-derived metric against trusted Dictionary datasets, publish only fully qualified single-dataset metrics, and warn when valid multi-dataset metrics are excluded.

**Architecture:** The provider contract declares each metric's `dataset_names`, while a pure trusted preparation function in `pipeline.py` parses the scalar SQL with SQLGlot, resolves identifiers against `_TableDraft` objects, and returns a separate accepted-metric list plus quality findings. `process_product` preserves the untouched `SuggestionBatch` for audit, builds IR and Registry records only from accepted normalized metrics, and applies warning policy before atomic promotion.

**Tech Stack:** Python 3.12, Pydantic 2, SQLGlot 30.16.0, pytest, Typer, uv, Ruff

## Global Constraints

- `MetricSuggestion.dataset_names` is required and non-empty in both Pydantic and OpenAI strict JSON Schema.
- SQLGlot must be locked exactly at `30.16.0` and used only for parsing, AST validation, transformation, and rendering.
- A published metric declares exactly one known dataset and every referenced column is fully qualified with canonical Dictionary spelling.
- A valid metric declaring two or more datasets is excluded with exactly one `METRIC_MULTI_DATASET_UNSUPPORTED` warning and creates no IR or Registry record.
- Invalid/unsafe SQL and unknown dataset/column references remain fail-closed provider output errors before promotion.
- The raw `SuggestionBatch`, including excluded metrics and original expressions, must remain unchanged in `llm-suggestions.json`.
- `--warnings-as-errors` must reach `process_product` so warning policy is decided before generated/Registry promotion.
- Do not infer relationships or grain and do not modify public IR, Registry, Ossie, or report schemas.
- Do not include the separate base-synchronization workflow fix.

---

### Task 1: Provider Metric Contract and Locked Parser Dependency

**Files:**
- Modify: `tests/unit/test_llm.py`
- Modify: `src/ard_ossie/llm.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: `MetricSuggestion.dataset_names: list[str]` with `Field(min_length=1)`.
- Produces: strict schema property `dataset_names` as a non-empty string array.
- Produces: runtime dependency `sqlglot==30.16.0` available to `pipeline.py`.

- [ ] **Step 1: Write the failing contract tests**

Add tests that hand-check the exact metric schema requirements and Pydantic behavior:

```python
def test_metric_contract_requires_non_empty_dataset_names() -> None:
    metric_schema = semantic_extraction_schema()["properties"]["metrics"]["items"]
    assert metric_schema["properties"]["dataset_names"] == {
        "type": "array",
        "minItems": 1,
        "items": {"type": "string"},
    }
    assert "dataset_names" in metric_schema["required"]

    with pytest.raises(PydanticValidationError):
        MetricSuggestion.model_validate(
            {
                "name": "order_count",
                "expression": "COUNT(order_id)",
                "description": None,
                "synonyms": [],
                "confidence": 0.9,
                "evidence": [semantic_evidence()],
                "status": "ai_suggested",
            }
        )
```

Also validate that `dataset_names=[]` fails and `dataset_names=["orders"]` succeeds.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv run --frozen pytest tests/unit/test_llm.py -q`

Expected: FAIL because `dataset_names` is absent from the schema/model contract.

- [ ] **Step 3: Implement the minimal contract and prompt change**

Add this field to `MetricSuggestion`:

```python
dataset_names: list[str] = Field(min_length=1)
```

Add the corresponding closed JSON Schema property and required name. Extend `_extract_suggestions`'s system message to require exact Dictionary-derived dataset names and include a `datasets` payload containing each canonical table name and canonical column names. Do not change evidence or product-fact contracts.

- [ ] **Step 4: Lock SQLGlot and verify GREEN**

Add `"sqlglot==30.16.0"` to `[project].dependencies`, run `VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv lock --offline`, then rerun the focused tests with `--frozen`.

Expected: all `tests/unit/test_llm.py` tests pass and `uv.lock` contains SQLGlot 30.16.0.

- [ ] **Step 5: Commit the contract**

```bash
git add pyproject.toml uv.lock src/ard_ossie/llm.py src/ard_ossie/pipeline.py tests/unit/test_llm.py
git commit -m "feat: require metric datasets"
```

### Task 2: Trusted Scalar SQL Preparation

**Files:**
- Modify: `tests/unit/test_pipeline.py`
- Modify: `src/ard_ossie/pipeline.py`

**Interfaces:**
- Consumes: `MetricSuggestion.dataset_names`, `_TableDraft.locator.table_name`, and `_TableDraft.columns`.
- Produces: `_PreparedMetrics(suggestions: list[MetricSuggestion], findings: list[QualityFinding], excluded_names: list[str])`.
- Produces: `_prepare_metrics(suggestions: list[MetricSuggestion], drafts: list[_TableDraft]) -> _PreparedMetrics`.

- [ ] **Step 1: Add fixture helpers and failing qualification tests**

Create real `_TableDraft` fixtures through `_resolve_tables` using a two-table `ParsedDictionary`; construct metric suggestions with literal Evidence. Add parameterized tests for:

```python
(
    "COUNT(DISTINCT campaign_id)",
    "COUNT(DISTINCT marketing_campaign.campaign_id)",
),
(
    "SUM(CASE WHEN status = 'active' THEN revenue ELSE 0 END)",
    "SUM(CASE WHEN marketing_campaign.status = 'active' "
    "THEN marketing_campaign.revenue ELSE 0 END)",
),
(
    "ROUND(SUM(revenue) / NULLIF(COUNT(*), 0), 2)",
    "ROUND(SUM(marketing_campaign.revenue) / NULLIF(COUNT(*), 0), 2)",
),
```

Assert the raw `MetricSuggestion.expression` remains byte-for-byte unchanged while the returned accepted copy contains normalized SQL. Include already-qualified canonical/case-insensitive inputs and `COUNT(*)`.

- [ ] **Step 2: Run qualification tests and verify RED**

Run: `VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv run --frozen pytest tests/unit/test_pipeline.py -q`

Expected: FAIL because `_prepare_metrics` and `_PreparedMetrics` do not exist.

- [ ] **Step 3: Implement minimal single-dataset parsing and normalization**

Use `sqlglot.parse` and `sqlglot.expressions` to require exactly one scalar expression. Build case-insensitive catalogs that retain canonical dataset and column spelling. Reject statement/query/subquery/DDL/DML/command ASTs. Traverse every `exp.Column`, validate its qualifier and identifier, copy the metric with `model_copy(update={"expression": normalized})`, and render canonical SQL. `COUNT(*)` is accepted without manufacturing a column.

Map failures to these exact `ValueError` codes so `process_product` classifies them as provider output errors:

```text
LLM_METRIC_NAME_OR_EXPRESSION_EMPTY
LLM_METRIC_DATASET_EMPTY
LLM_METRIC_DATASET_DUPLICATE
LLM_METRIC_DATASET_UNKNOWN
LLM_METRIC_SQL_UNSAFE
LLM_METRIC_SQL_INVALID
LLM_METRIC_REFERENCE_UNKNOWN
```

- [ ] **Step 4: Add failing invalid-input tests**

Add table-driven tests proving rejection of blank/duplicate/unknown datasets, unknown columns, undeclared qualifiers, multiple statements, `SELECT`, subqueries, and `DELETE`/`CREATE`/command SQL. Include confidence `0.1` cases to prove validation runs before confidence filtering.

- [ ] **Step 5: Run invalid-input tests, complete validation, and verify GREEN**

Run the full unit file after each error-family implementation until every test passes. Confirm errors expose only the stable code in `ProviderExecutionError`, not SQL text or identifiers.

- [ ] **Step 6: Add RED/GREEN multi-dataset exclusion behavior**

Add a valid two-dataset metric fixture and assert:

```python
prepared = pipeline._prepare_metrics([metric], drafts)
assert prepared.suggestions == []
assert prepared.findings == [
    QualityFinding(
        code="METRIC_MULTI_DATASET_UNSUPPORTED",
        path="metrics.Modeled Efficiency",
        message=(
            "Metric uses multiple datasets and was excluded because join path, "
            "cardinality, and grain are not declared"
        ),
    )
]
```

Qualified unknown references still fail before exclusion. Unqualified columns only need to exist in at least one declared dataset because no expression is published.

- [ ] **Step 7: Commit the trusted preparation boundary**

```bash
git add src/ard_ossie/pipeline.py tests/unit/test_pipeline.py
git commit -m "feat: normalize safe metric SQL"
```

### Task 3: Pipeline Warning, Audit, and Publication Integration

**Files:**
- Modify: `tests/integration/test_cli_process.py`
- Modify: `tests/integration/test_atomic_promotion.py`
- Modify: `src/ard_ossie/pipeline.py`

**Interfaces:**
- Consumes: `_PreparedMetrics.suggestions` and `.findings`.
- Produces: generated/Registry state containing accepted metrics only.
- Produces: quality status `WARN` and raw `llm-suggestions.json` retaining all provider metrics.

- [ ] **Step 1: Write the failing PR #5-shaped integration test**

Create a provider that returns:

```python
{
    "name": "Campaign Count",
    "expression": "COUNT(DISTINCT campaign_id)",
    "dataset_names": ["marketing_campaign"],
    ...
},
{
    "name": "Modeled Efficiency",
    "expression": "SUM(marketing_campaign.revenue) / SUM(sales_order.cost)",
    "dataset_names": ["marketing_campaign", "sales_order"],
    ...
}
```

Use a two-table Dictionary and assert the Ossie metric expression is `COUNT(DISTINCT marketing_campaign.campaign_id)`, the cross-dataset metric is absent from Ossie/Markdown/product Registry metrics, including when reprocessing a Registry that previously contained that metric, the report is `WARN` with exactly one exclusion finding, and the raw audit retains both original expressions and both `dataset_names` arrays.

- [ ] **Step 2: Run the integration test and verify RED**

Run: `VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv run --frozen pytest tests/integration/test_cli_process.py -q`

Expected: FAIL because `process_product` still sends raw metrics to `_build_metrics` and does not append exclusion findings.

- [ ] **Step 3: Wire prepared metrics into `process_product`**

Immediately after trusted provider extraction, call `_prepare_metrics` with every raw metric and current drafts. Keep `suggestion_batch` untouched, pass only prepared accepted suggestions to `_build_metrics`, and extend completeness warnings with prepared findings before evaluating `warnings_as_errors`. Update the strict failure message from completeness-specific wording to general quality-warning wording.

- [ ] **Step 4: Verify pipeline integration GREEN**

Run the two integration test files. Confirm normal mode promotes accepted output with `WARN`, while malformed metric errors still leave generated/Registry state unchanged.

- [ ] **Step 5: Add and verify strict atomic-promotion test**

Run an initial successful product process to create a known generated/Registry state, then process an update with the multi-dataset provider and `warnings_as_errors=True`. Assert `PipelineValidationError("WARNINGS_AS_ERRORS")`, unchanged generated and Registry tree hashes, and a retained `quality-report.json` containing both `WARNINGS_AS_ERRORS` and `METRIC_MULTI_DATASET_UNSUPPORTED`.

- [ ] **Step 6: Commit integration behavior**

```bash
git add src/ard_ossie/pipeline.py tests/integration/test_cli_process.py tests/integration/test_atomic_promotion.py
git commit -m "feat: exclude unsupported metrics"
```

### Task 4: Direct CLI Strict Warning Propagation

**Files:**
- Modify: `tests/integration/test_cli_process.py`
- Modify: `src/ard_ossie/cli/process.py`

**Interfaces:**
- Consumes: existing Typer flag `--warnings-as-errors`.
- Produces: `process_product(..., warnings_as_errors=warnings_as_errors)` before promotion.

- [ ] **Step 1: Write the failing CLI adapter test**

Monkeypatch `src.ard_ossie.cli.process.process_product` with a capturing callable, invoke `ard process ... --warnings-as-errors`, and assert the call receives `warnings_as_errors=True`. Return a `ProcessResult` without warnings so the test isolates argument propagation rather than post-hoc CLI exit handling.

- [ ] **Step 2: Run the test and verify RED**

Run the single test with `pytest -q` and confirm the captured keyword is missing.

- [ ] **Step 3: Pass the option into the pipeline and remove post-promotion enforcement**

Add `warnings_as_errors=warnings_as_errors` to the `process_product` call. Delete the post-success `if warnings_as_errors and result.quality_report.warnings: raise typer.Exit(1)` branch because strict failures now occur atomically inside the pipeline and use the established validation exit code/report path.

- [ ] **Step 4: Run CLI and atomic tests and verify GREEN**

Run: `VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv run --frozen pytest tests/integration/test_cli_process.py tests/integration/test_atomic_promotion.py -q`

Expected: all tests pass; strict warnings exit through `PipelineValidationError` before promotion.

- [ ] **Step 5: Commit CLI propagation**

```bash
git add src/ard_ossie/cli/process.py tests/integration/test_cli_process.py
git commit -m "fix: enforce strict warnings atomically"
```

### Task 5: Repository Verification and Review Readiness

**Files:**
- Verify: `docs/superpowers/specs/2026-08-12-metric-dataset-safety-design.md`
- Verify: `docs/superpowers/plans/2026-08-12-metric-dataset-safety.md`
- Verify: all changed source, test, dependency, lock, and schema files

**Interfaces:**
- Produces: a reviewable branch whose diff is limited to metric dataset safety.

- [ ] **Step 1: Run focused regression and mutation checks**

Run the metric contract, pipeline unit, CLI integration, and atomic-promotion tests. Temporarily revert the normalization call (without committing), prove at least one bare-column test fails, restore it, and rerun the focused suite to prove GREEN.

- [ ] **Step 2: Run full repository verification**

Run with the existing shared virtual environment explicitly activated:

```bash
VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv run --frozen pytest -q
VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv run --frozen ruff check .
VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv run --frozen python -m ard_ossie.application.model_schema_verification --repository .
VIRTUAL_ENV=../../.venv PATH=../../.venv/bin:$PATH uv build --no-build-isolation
```

Parse every `.github/workflows/*.yml` with `yaml.safe_load`, run the repository's schema catalog/Ossie checksum/secret scan commands exposed by repository checks, and confirm the sdist/wheel contain SQLGlot only as metadata dependency rather than vendored code.

- [ ] **Step 3: Re-read the approved design and inspect the complete diff**

Check every acceptance criterion explicitly. Confirm no public IR/Ossie/report schema changed, excluded metrics do not affect canonical hash or Registry records, raw audit is unchanged, all provider test fixtures now include `dataset_names`, and no base-synchronization workflow file changed.

- [ ] **Step 4: Request independent code review and address findings**

Provide the reviewer `origin/main` as base, current HEAD, this plan, and the approved design. Fix every Critical or Important finding with a fresh RED→GREEN cycle and rerun the full verification commands.

The review-remediation regression set must cover the complete non-scalar SQL statement families,
table- and set-valued functions, tokenizer failures, absence of raw rejected SQL in logs, reserved
identifier quoting plus final reparse, duplicate case-insensitive leaf dataset names before
provider execution, accepted/excluded metric-name or existing-alias identity collisions, and
provider-output exception classification through `process_product`.

- [ ] **Step 5: Publish a Draft PR**

Confirm `git status -sb` and the exact changed-file set, push `agent/metric-dataset-safety` without force, and open a Draft PR targeting `main`. The PR body must describe the P1 root cause, provider-contract change, trusted SQL boundary, exclusion policy, compatibility, and exact verification evidence. Do not mark Ready or merge in this task.
