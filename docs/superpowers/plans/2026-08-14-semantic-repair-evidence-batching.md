# Semantic Structure Repair Evidence Batching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair large semantic documents through deterministic source-evidence batches while preserving one lossless, globally validated document plan.

**Architecture:** Keep trusted-plan reuse and the existing single-batch path intact. For multi-page inputs, derive contiguous ordinal batches from page identity and native-table ownership, validate each provider response locally, retry only failed batches during a second logical pass, normalize the successful blocks into one plan, and run the existing validator over the complete span context before applying anything.

**Tech Stack:** Python 3.12, Pydantic 2, JSON Schema Draft 2020-12, pytest, Ruff.

## Global Constraints

- Do not introduce a fixed span, token, page, or block count for batching.
- Do not correct, paraphrase, translate, add, or delete source text.
- Preserve every unresolved source span exactly once and in source order.
- Keep all unresolved spans owned by one native table in one evidence batch.
- Make provider calls serially; do not add concurrency.
- Retry invalid output at most once at the logical document-pass level.
- Never apply a partial set of successful batches.
- Do not change the public semantic-repair report schema.
- Run only focused local tests; GitHub CI is the broader integration gate.

---

### Task 1: Partition and merge valid evidence batches

**Files:**
- Modify: `tests/unit/semantic/test_repair.py`
- Modify: `src/ard_ossie/semantic/repair.py`

**Interfaces:**
- Consumes: `_RepairContext`, `NativeDocument.tables`, `StructureDocument`, and the existing `SemanticStructureRepairPlanner._validate_plan()` contract.
- Produces: `_RepairBatch`, `_evidence_batches(context: _RepairContext, skeleton: StructureDocument) -> tuple[_RepairBatch, ...]`, `_repair_messages(context: _RepairContext, skeleton: StructureDocument) -> list[dict[str, str]]`, and `_merge_batch_plans(context: _RepairContext, plans: Sequence[_ValidatedPlan]) -> RepairPlan`.

- [ ] **Step 1: Add a two-page fixture and a failing merge regression**

Add imports for `NativeTable` and `NativeTableCell`, then add this fixture shape to `tests/unit/semantic/test_repair.py`:

```python
def multipage_fixture() -> tuple[NativeDocument, StructureDocument, tuple[str, ...]]:
    spans = (
        span(0, "Page 1 heading", page=1),
        span(1, "Page 1 body", page=1),
        span(2, "Page 2 heading", page=2),
        span(3, "Page 2 body", page=2),
    )
    native = NativeDocument(
        source_hash=SOURCE_HASH,
        extraction_mode=ExtractionMode.PDF_EMBEDDED,
        page_count=2,
        parser_versions={"pdf": "fixture-v1"},
        spans=spans,
        groups=(),
        tables=(),
    )
    skeleton = StructureDocument(
        blocks=(
            StructureBlock(kind="paragraph", order=0, page=1, bbox=None, text_hint="p1"),
            StructureBlock(kind="paragraph", order=1, page=2, bbox=None, text_hint="p2"),
        )
    )
    return native, skeleton, tuple(item.span_id for item in spans)
```

Add a regression whose provider responses are one valid paragraph plan for page 1 and one for page 2. Assert observable behavior, not private helper structure:

```python
def test_page_evidence_batches_merge_into_one_globally_ordered_plan() -> None:
    native, skeleton, span_ids = multipage_fixture()
    provider = RecordingProvider(
        responses=[
            {"blocks": [repair_block(kind="paragraph", order=40, span_ids=list(span_ids[:2]))]},
            {"blocks": [repair_block(kind="paragraph", order=7, span_ids=list(span_ids[2:]))]},
        ]
    )

    application = SemanticStructureRepairPlanner(provider).repair(
        native, skeleton, span_ids, trusted_record=None
    )

    assert application.record.outcome == "applied"
    assert application.record.plan is not None
    assert [block.order for block in application.record.plan.blocks] == [0, 1]
    assert [span_id for block in application.record.plan.blocks for span_id in block.span_ids] == list(span_ids)
    assert len(provider.calls) == 2
    payloads = [json.loads(call["messages"][1]["content"]) for call in provider.calls]
    assert [[item["span_id"] for item in payload["unresolved_spans"]] for payload in payloads] == [list(span_ids[:2]), list(span_ids[2:])]
    assert [[hint["page"] for hint in payload["structural_hints"]] for payload in payloads] == [[1], [2]]

    reuse_provider = RecordingProvider({"blocks": []})
    reused = SemanticStructureRepairPlanner(reuse_provider).repair(
        native, skeleton, span_ids, trusted_record=application.record
    )
    assert reuse_provider.calls == []
    assert reused.record.outcome == "reused"
    assert reused.record.plan_hash == application.record.plan_hash
    assert reused.blocks == application.blocks
```

The production mutation this catches is reverting to one whole-document request or merging batch blocks without global order normalization.

- [ ] **Step 2: Run the merge regression and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/semantic/test_repair.py::test_page_evidence_batches_merge_into_one_globally_ordered_plan
```

Expected: FAIL because the current planner sends all four spans in one request and rejects the first page-only response as missing spans.

- [ ] **Step 3: Implement page evidence intervals and deterministic merge**

In `src/ard_ossie/semantic/repair.py`:

1. Advance `REPAIR_PROMPT_VERSION` to `semantic-structure-repair-v3`.
2. Extend `_SYSTEM_PROMPT` with: `The current request may be one evidence batch from a larger document. Allocate every supplied span exactly once and never reference a span outside this request.`
3. Add the immutable batch type:

```python
@dataclass(frozen=True)
class _RepairBatch:
    context: _RepairContext
    skeleton: StructureDocument
```

4. Implement the first version of `_evidence_batches()` by mapping every full-context span ID to its index and creating inclusive intervals for each page and each contiguous page-less run. Do not add native-table intervals yet; the following RED cycle covers that behavior. Sort and merge overlapping intervals, then build a batch context from each final contiguous slice through `_repair_context()`. Filter structural hints to pages present in that slice; retain page-less hints only for a slice that contains page-less spans.
5. Extract existing message construction into `_repair_messages()` so single and multi-batch requests share the same prompt and JSON serialization.
6. Add `evidence_batch` metadata to `_request_payload()` with literal fields `ordinal_start`, `ordinal_end`, and `pages`; this metadata contains no source text beyond the already supplied immutable spans.

Use inclusive interval merging with this exact rule:

```python
for start, end in sorted(intervals):
    if merged and start <= merged[-1][1]:
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    else:
        merged.append((start, end))
```

Every ordered index must be covered by exactly one final interval; raise `AssertionError` if an internal construction violates that invariant.

- [ ] **Step 4: Implement first-pass validation and deterministic merge**

After trusted reuse and the empty-context branch in `repair()`:

- Keep the existing code path when `_evidence_batches()` returns one batch.
- For multiple batches, call the provider serially once per batch and validate against the batch context.
- Collect valid `_ValidatedPlan` instances.
- Initially reject the entire repair on the first invalid batch; Task 2 replaces this temporary branch with bounded batch retry.
- Merge valid blocks by their first span position in the full context, replace their model-supplied orders with consecutive `0..n-1`, create one `RepairPlan`, and pass its JSON payload through `_validate_plan(full_context, payload)`.
- Return `_application(full_context, globally_validated, provider=provider, model=model, outcome="applied", validation_codes=validation_codes)` only if global validation succeeds.

The merge key must use existing `_plan_span_ids(RepairPlan(blocks=[block]))` allocations and the full context's literal span-position map. Do not sort by model-supplied `order` across batches.

- [ ] **Step 5: Run the page merge regression and verify GREEN**

Run the page merge node ID from Step 2. Expected: PASS with two provider calls and one globally ordered plan.

- [ ] **Step 6: Add a failing native-table connectivity regression**

Add imports for `NativeTable` and `NativeTableCell`. Create a two-page `NativeTable` whose four cells own all four fixture spans. Return a single valid 2x2 table plan and assert `outcome == "applied"` and `len(provider.calls) == 1`. This proves that removing table connectivity would incorrectly split the plan across two page requests.

Use the literal table structure:

```python
table = NativeTable(
    order=0,
    row_count=2,
    column_count=2,
    cells=tuple(
        NativeTableCell(
            start_row=index // 2,
            end_row=index // 2 + 1,
            start_column=index % 2,
            end_column=index % 2 + 1,
            span_ids=(span_id,),
            column_header=index < 2,
        )
        for index, span_id in enumerate(span_ids)
    ),
)
```

- [ ] **Step 7: Run the table regression and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/semantic/test_repair.py::test_native_table_connects_page_evidence_into_one_batch
```

Expected: FAIL because page-only evidence batching makes two provider calls and neither batch can validate the complete table response locally.

- [ ] **Step 8: Add native-table ownership intervals**

Extend `_evidence_batches()` with one inclusive interval per native table after filtering its cell span IDs to the full unresolved context. The interval starts at the minimum owned span index and ends at the maximum. The existing overlap merge takes the convex closure, joining the table's pages and every intervening unresolved span without an arbitrary size rule.

- [ ] **Step 9: Run the two focused regressions and the existing repair unit file**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/unit/semantic/test_repair.py::test_page_evidence_batches_merge_into_one_globally_ordered_plan \
  tests/unit/semantic/test_repair.py::test_native_table_connects_page_evidence_into_one_batch
.venv/bin/python -m pytest -q tests/unit/semantic/test_repair.py
```

Expected: the two focused tests PASS, then the complete repair unit file PASS.

- [ ] **Step 10: Commit Task 1**

```bash
git add src/ard_ossie/semantic/repair.py tests/unit/semantic/test_repair.py
git commit -m "feat: batch semantic repair by source evidence"
```

---

### Task 2: Retry failed batches atomically

**Files:**
- Modify: `tests/unit/semantic/test_repair.py`
- Modify: `src/ard_ossie/semantic/repair.py`

**Interfaces:**
- Consumes: `_RepairBatch`, `_repair_messages()`, `_retry_feedback()`, and `_merge_batch_plans()` from Task 1.
- Produces: two-pass multi-batch behavior with ordered validation codes and all-or-nothing application.

- [ ] **Step 1: Add a failing regression for retrying only the invalid page**

Use `multipage_fixture()` with three responses in this order: valid page 1, page 2 missing its final span, valid replacement page 2. Assert:

```python
assert len(provider.calls) == 3
assert application.record.outcome == "applied"
assert application.record.validation_codes == ["SEMANTIC_REPAIR_MISSING_SPAN"]
retry_payload = json.loads(provider.calls[2]["messages"][-1]["content"])
assert retry_payload["repair_retry"]["affected_span_ids"] == [span_ids[-1]]
first_payload = json.loads(provider.calls[0]["messages"][1]["content"])
assert [item["span_id"] for item in first_payload["unresolved_spans"]] == list(span_ids[:2])
```

The production mutation this catches is rerunning a valid sibling batch or retrying the original 677-span document.

- [ ] **Step 2: Run the selective retry regression and verify RED**

Run only the new node ID. Expected: FAIL because Task 1's temporary multi-batch path rejects the first invalid batch without retrying it.

- [ ] **Step 3: Add failing atomic rejection and provider-failure regressions**

Add one test where page 2 is missing a span on both attempts. Assert three calls (page 1 once and page 2 twice), `blocks == ()`, `outcome == "rejected"`, and duplicate missing-span codes remain in order.

Extend `RecordingProvider.generate_structured()` so an `Exception` stored in `responses` is raised after being removed. Then add a three-page test with a valid page-1 response, an invalid page-2 response, and a `ProviderExecutionError("LLM_PROVIDER_TIMEOUT", kind=ProviderFailureKind.TRANSIENT)` for page 3. Assert three calls, `blocks == ()`, `outcome == "degraded"`, `validation_codes == ["SEMANTIC_REPAIR_MISSING_SPAN"]`, and `provider_error_code == "LLM_PROVIDER_TIMEOUT"`.

These tests exercise the real planner while faking only the external provider boundary.

- [ ] **Step 4: Run both new regressions and verify RED**

Run the two exact node IDs. Expected: atomic rejection test FAIL because retry is absent; provider-failure test FAIL because the temporary implementation stops at page 2 and never reaches the page-3 provider error.

- [ ] **Step 5: Implement two logical passes**

Refactor the multi-batch path to:

```python
validated_by_index: dict[int, _ValidatedPlan] = {}
failed: list[_FailedBatch] = []
validation_codes: list[str] = []

for index, batch in enumerate(batches):
    messages = _repair_messages(batch.context, batch.skeleton)
    response = self._provider.generate_structured(schema=batch.context.schema, messages=messages)
    validated, code, parsed = self._validate_plan(batch.context, response)
    if validated is not None:
        validated_by_index[index] = validated
    else:
        failure_code = code or _SCHEMA_INVALID
        validation_codes.append(failure_code)
        failed.append(_FailedBatch(index, batch, messages, response, parsed, failure_code))

for failure in failed:
    retry_messages = [
        *failure.messages,
        {
            "role": "user",
            "content": json.dumps(
                _retry_feedback(
                    failure.batch.context,
                    code=failure.code,
                    response=failure.response,
                    parsed_plan=failure.parsed_plan,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    ]
    retry_response = self._provider.generate_structured(
        schema=failure.batch.context.schema,
        messages=retry_messages,
    )
    validated, code, _parsed = self._validate_plan(failure.batch.context, retry_response)
    if validated is None:
        validation_codes.append(code or _SCHEMA_INVALID)
    else:
        validated_by_index[failure.index] = validated
```

Add this frozen state type:

```python
@dataclass(frozen=True)
class _FailedBatch:
    index: int
    batch: _RepairBatch
    messages: list[dict[str, str]]
    response: object
    parsed_plan: RepairPlan | None
    code: str
```

Catch `ProviderExecutionError` around every provider call. Re-raise when `self.propagate_provider_errors` is true; otherwise return `_empty_application(full_context, provider=provider, model=model, outcome="degraded", validation_codes=validation_codes, provider_error_code=error.code)` so validated siblings never escape. If any batch remains absent after pass two, return `_empty_application(full_context, provider=provider, model=model, outcome="rejected", validation_codes=validation_codes)`. Otherwise merge in numeric batch-index order and globally validate before applying.

- [ ] **Step 6: Run the new regressions and complete repair unit file**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/semantic/test_repair.py::test_only_invalid_evidence_batch_is_retried \
  tests/unit/semantic/test_repair.py::test_second_invalid_batch_response_rejects_all_batches \
  tests/unit/semantic/test_repair.py::test_provider_failure_in_one_batch_discards_valid_siblings
.venv/bin/python -m pytest -q tests/unit/semantic/test_repair.py
```

Expected: all focused tests PASS and the repair unit file PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/ard_ossie/semantic/repair.py tests/unit/semantic/test_repair.py
git commit -m "fix: retry semantic evidence batches atomically"
```

---

### Task 3: Keep logical-attempt diagnostics accurate

**Files:**
- Modify: `tests/integration/test_docling_pipeline.py`
- Modify: `src/ard_ossie/pipeline.py`

**Interfaces:**
- Consumes: `SemanticStructureRepairRecord.validation_codes` in deterministic pass/batch order.
- Produces: `_semantic_repair_diagnostic()` reporting at most two logical document attempts even when several batches emit validation failures.

- [ ] **Step 1: Add a third batch failure code to the controlled diagnostic regression**

In `DetailedRejectingPlanner`, use these literal codes:

```python
validation_codes=[
    "SEMANTIC_REPAIR_MISSING_SPAN",
    "SEMANTIC_REPAIR_ORDER_INVALID",
    "SEMANTIC_REPAIR_MISSING_SPAN",
]
```

Update `test_semantic_structure_degraded_finding_includes_safe_repair_diagnostics` to assert the complete three-code string and retain `assert "attempts=2" in specific.message`.

The production mutation this catches is deriving logical attempts from the raw number of failed batch responses.

- [ ] **Step 2: Run the diagnostic regression and verify RED**

```bash
.venv/bin/python -m pytest -q tests/integration/test_docling_pipeline.py::test_semantic_structure_degraded_finding_includes_safe_repair_diagnostics
```

Expected: FAIL with `attempts=3` from the current `len(codes)` calculation.

- [ ] **Step 3: Bound diagnostic attempts by logical pass**

Replace only the attempt calculation in `_semantic_repair_diagnostic()`:

```python
if repair.outcome == "reused":
    attempts = 0
else:
    attempts = 2 if codes else 1
```

Keep provider, model, applied/rejected block counts, validation-code rendering, and source-text redaction unchanged.

- [ ] **Step 4: Run the focused diagnostic and existing source-check diagnostic tests**

```bash
.venv/bin/python -m pytest -q \
  tests/integration/test_docling_pipeline.py::test_semantic_structure_degraded_finding_includes_safe_repair_diagnostics \
  tests/integration/test_docling_pipeline.py::test_native_table_rejection_preserves_same_code_from_prior_attempt \
  tests/unit/test_source_check_service.py
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/ard_ossie/pipeline.py tests/integration/test_docling_pipeline.py
git commit -m "fix: report semantic repair logical attempts"
```

---

### Task 4: Focused final verification and branch handoff

**Files:**
- Verify: `src/ard_ossie/semantic/repair.py`
- Verify: `src/ard_ossie/pipeline.py`
- Verify: `tests/unit/semantic/test_repair.py`
- Verify: `tests/integration/test_docling_pipeline.py`
- Verify: `tests/unit/test_source_check_service.py`

**Interfaces:**
- Consumes: all prior task commits.
- Produces: a clean branch ready for remote CI and Issue #3 acceptance testing.

- [ ] **Step 1: Run the bounded local gate**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/semantic/test_repair.py \
  tests/unit/test_source_check_service.py \
  tests/integration/test_docling_pipeline.py::test_semantic_structure_degraded_finding_includes_safe_repair_diagnostics \
  tests/integration/test_docling_pipeline.py::test_native_table_rejection_preserves_same_code_from_prior_attempt
.venv/bin/ruff check \
  src/ard_ossie/semantic/repair.py \
  src/ard_ossie/pipeline.py \
  tests/unit/semantic/test_repair.py \
  tests/integration/test_docling_pipeline.py
git diff --check origin/main...HEAD
```

Expected: selected tests PASS, Ruff PASS, and no whitespace errors. Do not run the complete local pytest suite.

- [ ] **Step 2: Inspect the final diff and invariants**

Confirm from the diff that:

- no source text is written into `SemanticStructureRepairRecord`;
- there is no numeric batch-size limit;
- all provider calls remain serial;
- one failed batch returns no semantic blocks;
- the merged plan is globally validated before `_application()`;
- public JSON schema files are unchanged; and
- only expected production, test, design, and plan files changed.

- [ ] **Step 3: Push and open the fix PR**

Push `fix/semantic-repair-evidence-batching` and open a PR against `main` describing the reproduced `677`-span failure, evidence batching, atomic retry, focused local results, and Issue #3 acceptance criteria. Keep GitHub environment reviewers disabled as previously requested. Let required GitHub CI jobs provide the broad suite.

- [ ] **Step 4: Merge only after required CI is green**

Resolve only actionable review findings, rerun the smallest affected local test, and merge after required checks pass. Do not add a review requirement during this temporary no-review period.

- [ ] **Step 5: Retrigger Issue #3 exactly once and inspect artifacts**

Remove and restore only the `ard:approved` label once. Verify the resulting run uses the merged commit. Inspect the generated semantic markdown, semantic fidelity report, semantic repair report, and quality report against the acceptance criteria in the design document. Keep PR #5 draft unless every semantic acceptance criterion passes.
