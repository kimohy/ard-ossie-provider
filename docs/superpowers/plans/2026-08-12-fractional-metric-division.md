# Fractional Metric Division Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure generated ANSI SQL metric ratios cannot truncate to integer results when their numerator is inferred as an integral type.

**Architecture:** Keep the provider response untrusted and normalize it inside the existing single-dataset metric preparation boundary. After canonical column qualification, wrap the scalar expression in a temporary typed `SELECT`, let sqlglot infer types from the approved dictionary schema, and cast only integral division numerators to `DECIMAL(38, 12)` before compiling the Ossie model.

**Tech Stack:** Python 3.12, sqlglot, Pydantic, pytest, Ruff

## Global Constraints

- Preserve the existing scalar-SQL and dataset-reference security checks.
- Do not modify generated ARD artifacts or quality hashes by hand.
- Do not change already-fractional `NUMERIC`, `DECIMAL`, `FLOAT`, or `DOUBLE` division numerators.
- Keep the normalization deterministic and idempotent.
- Publish the processor fix separately from PR #5, then regenerate PR #5 through the trusted workflow.

---

### Task 1: Reproduce integral metric division at the preparation boundary

**Files:**
- Modify: `tests/unit/test_pipeline.py`

**Interfaces:**
- Consumes: `pipeline._prepare_metrics(suggestions, drafts) -> _PreparedMetrics`
- Produces: A regression test asserting the exact portable ANSI SQL expression emitted for an integer ratio.

- [ ] **Step 1: Extend the existing metric dictionary fixture with integral measures**

Add `engagement_count` and `impression_count` as `INTEGER` columns to the existing `marketing_campaign` draft fixture so the test exercises real dictionary-to-IR type propagation.

- [ ] **Step 2: Write the failing regression test**

```python
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
```

- [ ] **Step 3: Run the test and verify RED**

Run:

```bash
PYTHONPATH=src python -m pytest tests/unit/test_pipeline.py::test_prepare_metrics_casts_integral_division_numerator -q
```

Expected: FAIL because the actual expression remains integer division without `CAST(... AS DECIMAL(38, 12))`.

---

### Task 2: Normalize integral division using approved dictionary types

**Files:**
- Modify: `src/ard_ossie/pipeline.py`
- Test: `tests/unit/test_pipeline.py`

**Interfaces:**
- Consumes: canonical single-dataset expression, canonical dataset name, and `list[ColumnIR]`
- Produces: `_normalize_metric_divisions(expression, dataset_name, columns) -> exp.Expr`

- [ ] **Step 1: Import sqlglot type annotation**

```python
from sqlglot.optimizer.annotate_types import annotate_types
```

- [ ] **Step 2: Add the minimal deterministic normalizer**

```python
_METRIC_DIVISION_TYPE = "DECIMAL(38, 12)"


def _normalize_metric_divisions(
    expression: exp.Expr,
    *,
    dataset_name: str,
    columns: list[ColumnIR],
) -> exp.Expr:
    if expression.find(exp.Div) is None:
        return expression
    query = exp.select(expression.copy()).from_(
        exp.Table(this=_canonical_metric_identifier(dataset_name, source=None))
    )
    annotate_types(
        query,
        schema={dataset_name: {column.name: column.data_type for column in columns}},
    )
    normalized = query.expressions[0]
    for division in reversed(list(normalized.find_all(exp.Div))):
        numerator = division.this
        if numerator.type.is_type(*exp.DataType.INTEGER_TYPES):
            division.set(
                "this",
                exp.cast(numerator.copy(), _METRIC_DIVISION_TYPE),
            )
    return normalized
```

- [ ] **Step 3: Invoke the normalizer after canonical column qualification**

Resolve the current single-dataset draft, then replace the qualified AST with `_normalize_metric_divisions(...)` before calling `.sql()` and the second scalar safety parse.

```python
dataset_draft = next(
    draft
    for draft in drafts
    if draft.locator.table_name.casefold() == dataset_key
)
normalized = _normalize_metric_divisions(
    normalized,
    dataset_name=canonical_dataset,
    columns=dataset_draft.columns,
)
normalized_expression = normalized.sql()
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
PYTHONPATH=src python -m pytest tests/unit/test_pipeline.py::test_prepare_metrics_casts_integral_division_numerator -q
```

Expected: PASS.

- [ ] **Step 5: Verify existing fractional behavior remains unchanged**

Run:

```bash
PYTHONPATH=src python -m pytest tests/unit/test_pipeline.py -q
```

Expected: PASS, including the existing `SUM(NUMERIC) / COUNT(*)` case without a redundant cast.

- [ ] **Step 6: Commit the implementation**

```bash
git add src/ard_ossie/pipeline.py tests/unit/test_pipeline.py
git commit -m "fix: preserve fractional metric division"
```

---

### Task 3: Verify, publish, and regenerate the reviewed data PR

**Files:**
- Verify only: repository test, schema, workflow, and package assets
- Regenerate through: `.github/workflows/ard-issue-intake.yml`

**Interfaces:**
- Consumes: processor fix commit and Issue #3 `ard:approved` reprocessing route
- Produces: a new PR #5 head whose `Engagement Rate` includes the decimal cast and whose required statuses are successful.

- [ ] **Step 1: Run the complete local gate**

```bash
PYTHONPATH=src python -m pytest -q
ruff check .
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 2: Publish the processor fix as a separate Draft PR**

Create a remote commit whose parent is the current `main` SHA and whose tree exactly matches the verified local tree. Open a Draft PR against `main`; do not add processor code to PR #5.

- [ ] **Step 3: Review and merge the processor fix**

Require all repository checks, no unresolved review threads, exact-head merge protection, and a fresh review before squash merging.

- [ ] **Step 4: Re-run Issue #3 through trusted base-sync and processing**

Reapply `ard:approved`, confirm PR #5 merges the new `main`, and approve only the protected `ard-llm` environment deployment if GitHub requests it.

- [ ] **Step 5: Verify regenerated artifacts before merging PR #5**

Assert all of the following on the exact new PR head:

```text
Engagement Rate = CAST(SUM(marketing_delivery.engagement_count) AS DECIMAL(38, 12))
                  / NULLIF(SUM(marketing_delivery.impression_count), 0)
ard/changeset = success
ard/quality-gate = success
quality completeness = 1.0
hard errors = 0
unresolved review threads = 0
```

- [ ] **Step 6: Request a fresh exact-head review and squash merge PR #5**

Use expected-head protection for the merge, then verify the merged PR state and the resulting `main` SHA.
