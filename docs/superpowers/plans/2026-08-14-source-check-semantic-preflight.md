# Source-check Semantic Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow credential-free source preflight to defer only PDF visual-LLM correction completion while preserving every deterministic semantic integrity gate and the normal fail-closed publication default.

**Architecture:** Add an explicit `require_semantic_visual_correction` policy that defaults to `True` at `process_product()` and is forwarded through `ModelingService.validate()`. Only `SourceCheckService` passes `False`; `_semantic_hard_findings()` continues to enforce fidelity and structure failures regardless of that policy and conditionally enforces only `SEMANTIC_VISUAL_CORRECTION_FAILED`.

**Tech Stack:** Python 3.12, Pydantic, pytest, Typer workflow services, Ruff

## Global Constraints

- `source-check` must run without LLM credentials.
- Only provider-dependent visual-correction completion may be deferred.
- Source text loss, duplicated or unmatched spans, and degraded semantic structure remain hard failures.
- `process_product()` remains fail-closed by default.
- Run only directly affected tests and Ruff on changed files; do not run the broad suite.

---

### Task 1: Encode the preflight policy with TDD

**Files:**
- Modify: `tests/integration/test_cli_process.py`
- Modify: `tests/unit/test_source_check_service.py`
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `src/ard_ossie/application/modeling.py`
- Modify: `src/ard_ossie/application/source_check.py`

**Interfaces:**
- Produces: `process_product(..., require_semantic_visual_correction: bool = True) -> ProcessResult`
- Produces: `ModelingService.validate(..., require_semantic_visual_correction: bool = True) -> ValidationResult`
- Consumes: `SemanticFidelityReport`, `PipelineValidationError`, and existing `FidelityParser` test fixtures

- [x] **Step 1: Add a failing pipeline regression for the deferred visual gate**

Add a test beside `test_pipeline_blocks_unresolved_semantic_fidelity_before_promotion` that builds the existing `SEMANTIC_OCR_CORRECTION_UNAVAILABLE` fidelity report and calls:

```python
result = process_product(
    product,
    registry_root=tmp_path / "registry",
    parser=FidelityParser(fidelity),
    require_semantic_visual_correction=False,
)

assert result.quality_report.status is QualityStatus.WARN
assert not result.quality_report.hard_errors
assert {
    finding.code for finding in result.quality_report.warnings
} == {"SEMANTIC_OCR_CORRECTION_UNAVAILABLE"}
```

The test must use `add_complete_dictionary_descriptions(product)` so unrelated completeness warnings do not obscure the assertion.

- [x] **Step 2: Add a failing regression proving deterministic degradation is not deferred**

Add a second test using `degraded_fidelity_report()`:

```python
with pytest.raises(PipelineValidationError) as captured:
    process_product(
        product,
        registry_root=tmp_path / "registry",
        parser=FidelityParser(degraded_fidelity_report()),
        require_semantic_visual_correction=False,
    )

assert captured.value.report is not None
assert "SEMANTIC_STRUCTURE_DEGRADED" in {
    finding.code for finding in captured.value.report.hard_errors
}
```

- [x] **Step 3: Add a failing source-check behavior regression**

In `tests/unit/test_source_check_service.py`, keep the real source-check and
model-validation path, replacing only the slow document parser boundary:

```python
fidelity = pass_fidelity_report().model_copy(
    update={
        "extraction_mode": ExtractionMode.OCR,
        "status": "WARN",
        "warning_codes": ["SEMANTIC_OCR_CORRECTION_UNAVAILABLE"],
    }
)
monkeypatch.setattr(
    pipeline_module,
    "_processing_parser",
    lambda **_kwargs: FidelityParser(fidelity),
)

result = SourceCheckService(RepositoryPaths(tmp_path)).run(
    "sales-order",
    SHA,
)

assert result.status is WorkflowStatus.SUCCESS
```

Import `ard_ossie.pipeline as pipeline_module`, `ExtractionMode`, and the
existing `FidelityParser` and `pass_fidelity_report` fixtures. This test catches
the real bug: removing the source-check opt-out makes the service raise
`WorkflowValidationError("SEMANTIC_VISUAL_CORRECTION_FAILED")`.

- [x] **Step 4: Run the three new tests and verify RED**

Run only the exact new test nodes:

```bash
pytest -q \
  tests/integration/test_cli_process.py::test_pipeline_source_preflight_defers_visual_correction_only \
  tests/integration/test_cli_process.py::test_pipeline_source_preflight_still_blocks_structure_degradation \
  tests/unit/test_source_check_service.py::test_source_check_defers_pdf_visual_correction_until_protected_processing
```

Expected: the pipeline tests error because `process_product()` does not accept
`require_semantic_visual_correction`; the source-check behavior test fails with
`SEMANTIC_VISUAL_CORRECTION_FAILED` because preflight still applies the
publication-only visual gate.

- [x] **Step 5: Implement the minimal explicit policy**

In `src/ard_ossie/pipeline.py`, extend `process_product()`:

```python
def process_product(
    product_path: str | Path,
    *,
    registry_root: str | Path,
    provider: LLMProvider | None = None,
    parser: DoclingParser | None = None,
    pr_number: int | None = None,
    warnings_as_errors: bool = False,
    trusted_semantic_repair: dict[str, object] | None = None,
    trusted_semantic_fidelity: dict[str, object] | None = None,
    require_semantic_visual_correction: bool = True,
) -> ProcessResult:
```

Forward that policy to the semantic hard-finding helper:

```python
hard_errors.extend(
    _semantic_hard_findings(
        semantic_document,
        require_visual_correction=require_semantic_visual_correction,
    )
)
```

Make the helper policy explicit by replacing its current one-line signature
with:

```python
def _semantic_hard_findings(
    document: ParsedDocument,
    *,
    require_visual_correction: bool = True,
) -> list[QualityFinding]:
```

Leave the existing fidelity-loss and degraded-structure blocks unchanged. In
the provider-dependent block only, replace its opening condition with:

```python
if require_visual_correction and fidelity.extraction_mode in {
    ExtractionMode.PDF_EMBEDDED,
    ExtractionMode.OCR,
}:
```

In `src/ard_ossie/application/modeling.py`, extend `validate()` with the same
keyword-only default and pass it to `process_product()`:

```python
def validate(
    self,
    product_path: str | Path,
    registry_path: str | Path,
    *,
    require_semantic_visual_correction: bool = True,
) -> ValidationResult:
```

In `src/ard_ossie/application/source_check.py`, opt out only for preflight:

```python
validation = ModelingService(self.paths).validate(
    product,
    "registry",
    require_semantic_visual_correction=False,
)
```

- [x] **Step 6: Run the three new tests and verify GREEN**

Run the exact command from Step 4. Expected: `3 passed`.

- [x] **Step 7: Run focused regression coverage**

```bash
pytest -q \
  tests/unit/test_source_check_service.py \
  tests/integration/test_cli_process.py -k \
  'source_check or semantic_fidelity or semantic_repair or source_preflight or unresolved_semantic_fidelity'
```

Expected: all selected tests pass, including the existing default publication
test for `SEMANTIC_VISUAL_CORRECTION_FAILED`.

- [x] **Step 8: Run focused static checks**

```bash
ruff check \
  src/ard_ossie/pipeline.py \
  src/ard_ossie/application/modeling.py \
  src/ard_ossie/application/source_check.py \
  tests/integration/test_cli_process.py \
  tests/unit/test_source_check_service.py
git diff --check
```

Expected: both commands exit successfully without output requiring changes.

- [x] **Step 9: Commit the tested fix**

```bash
git add \
  src/ard_ossie/pipeline.py \
  src/ard_ossie/application/modeling.py \
  src/ard_ossie/application/source_check.py \
  tests/integration/test_cli_process.py \
  tests/unit/test_source_check_service.py
git commit -m "fix: defer visual correction during source preflight"
```

### Task 2: Publish and validate the follow-up fix

**Files:**
- No additional production files
- Verify: GitHub pull request checks and workflow run metadata

**Interfaces:**
- Consumes: tested branch `fix/source-check-semantic-preflight`
- Produces: a reviewable GitHub PR and a merged `main` commit after approval

- [ ] **Step 1: Review the branch diff and commit scope**

```bash
git status --short
git log --oneline --decorate -3
git diff 361a46962d3fc11ec4f607542a09911de8fa9562...HEAD --check
git diff --stat 361a46962d3fc11ec4f607542a09911de8fa9562...HEAD
```

Expected: only the approved design, plan, policy implementation, and focused
regressions are present.

- [ ] **Step 2: Push the branch and open a draft PR**

Use the GitHub publication workflow with:

- Branch: `fix/source-check-semantic-preflight`
- Title: `fix: defer semantic visual correction during source preflight`
- Base: `main`
- Body: include the failed run `31771169206`, the root cause, the preserved
  deterministic gates, and focused test evidence.

- [ ] **Step 3: Inspect only the PR-required checks**

Confirm the PR head SHA, required check names, status, and conclusion. If a
GitHub Actions check fails, inspect that check before making any further code
change.

- [ ] **Step 4: Merge only after checks and review are clean**

Use squash merge, then record the exact merge commit SHA. Do not merge while
the PR is draft, checks are pending, or review has an actionable finding.

### Task 3: Reprocess Issue 3 and inspect semantic output

**Files:**
- Verify: `products/500138301/generated/data-semantic.md`
- Verify: `products/500138301/quality/semantic-fidelity.json`
- Verify: `products/500138301/quality/quality-report.json`

**Interfaces:**
- Consumes: merged follow-up fix on `main`
- Produces: a newly generated PR #5 head and a data-quality verdict

- [ ] **Step 1: Trigger one clean reprocessing run**

Remove and re-add only the `ard:approved` label on Issue #3. Confirm the new
workflow run references the merged follow-up commit on `main`.

- [ ] **Step 2: Monitor the workflow without redundant reruns**

Confirm `authorize`, `route`, `base_sync`, and credential-free `validate` pass.
If GitHub requests `ard-llm` environment approval, stop and ask the user to
approve that exact run. Continue monitoring the same run after approval.

- [ ] **Step 3: Inspect the newly generated PR #5 files**

Validate the exact new PR head SHA and assert:

- Markdown contains `Semantics 문서`, `개인정보`, `유효성`, and `캠페인 기간`.
- Markdown excludes `是州`, `号h`, `左叫`, raw HTML tags, `&#32;`, and `&#9;`.
- `semantic-fidelity.json` reports five audited pages, source text coverage
  `1.0`, zero rejected corrections, zero degraded blocks, no warning codes,
  and `PASS`.
- `quality-report.json` contains no hard errors and reports `PASS` or only
  unrelated, explainable warnings.

- [ ] **Step 4: Report the final evidence**

Provide the workflow URL, follow-up PR URL and merge SHA, PR #5 head SHA,
focused test counts, semantic checks, and any remaining risk. Keep PR #5 draft
unless every inspected semantic criterion passes.
