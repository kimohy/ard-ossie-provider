# OCR Overlapping Table Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every distinct OCR text span when Docling emits overlapping or otherwise invalid table cells, while allowing semantic processing to continue with HTML-free Markdown and a fidelity warning.

**Architecture:** Keep `NativeTable` strict and do not guess which overlapping cell is correct. During OCR extraction, collect the cell text evidence first; if the native table grid cannot validate, return those spans as ordinary paragraph groups and omit the invalid native table so the existing lossless-degradation path renders and audits them.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, Docling-compatible test doubles.

## Global Constraints

- Add exactly one regression test.
- Preserve all distinct OCR cell text; do not delete, merge, paraphrase, or select a winner.
- Do not relax `NativeTable` grid validation.
- Do not add HTML to Markdown rendering.
- Run only the new regression, the containing focused test module, Ruff on changed files, and the Issue #3 workflow reprocessing gate.

---

### Task 1: Lossless invalid OCR-table fallback

**Files:**
- Modify: `src/ard_ossie/semantic/sources.py`
- Test: `tests/unit/semantic/test_pdf_source.py`

**Interfaces:**
- Consumes: Docling `TableItem.data.table_cells` with cell text, row/column offsets, optional bounding boxes, and the existing `_source_span`/`NativeGroup` models.
- Produces: `_ocr_table(...) -> tuple[NativeTable | None, list[NativeGroup], list[SourceSpan]]`; valid grids return one table group, invalid grids return one paragraph group per nonempty cell span and no `NativeTable`.

- [x] **Step 1: Write the failing regression**

Add one test that passes two distinct OCR cell objects occupying the same 1x1 coordinate into `extract_ocr_native`. Assert that extraction does not raise, `native.tables` is empty, both texts remain in source order, and each span belongs to its own paragraph group.

- [x] **Step 2: Run the regression and verify RED**

Run: `.venv/bin/python -m pytest -q tests/unit/semantic/test_pdf_source.py::test_ocr_source_degrades_overlapping_table_cells_without_losing_text`

Expected: FAIL with `NATIVE_TABLE_CELL_REGIONS_OVERLAP` from current `NativeTable` construction.

- [x] **Step 3: Implement the minimal fallback**

Change `_ocr_table` to build OCR spans before committing to a `NativeTable`. Catch only Pydantic validation failures from native cell/table construction, return no table plus paragraph groups bound one-to-one to the collected spans, and retain the existing valid-table output unchanged. Update `extract_ocr_native` to append the optional table and extend returned groups.

- [x] **Step 4: Verify focused behavior**

Run the exact regression, then `tests/unit/semantic/test_pdf_source.py`, then `ruff check src/ard_ossie/semantic/sources.py tests/unit/semantic/test_pdf_source.py`.

Expected: all commands PASS with no warnings introduced by project code.

- [ ] **Step 5: Commit and publish**

Commit the plan, test, and implementation as `fix: preserve overlapping OCR table text`; push a dedicated branch, open a PR, wait for `ard/changeset` and `ard/quality-gate`, then merge only if all checks succeed.

- [ ] **Step 6: Reprocess Issue #3 once**

Reapply `ard:approved` to Issue #3, monitor the official workflow, and verify that PR #5 receives regenerated HTML-free semantic Markdown. Do not run another local full suite.
