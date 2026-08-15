# Issue #3 Verifier Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Issue #3 artifact verifier accept either validated PDF embedded-text extraction or OCR without weakening fidelity checks.

**Architecture:** Keep `verify_issue_3` as the end-to-end artifact gate and isolate its extraction-mode contract in one guard. The guard accepts the two PDF modes and rejects non-PDF modes before any artifact reuse checks run.

**Tech Stack:** Python 3.12, Pydantic models, pytest, Ruff, markdown-it-py

## Global Constraints

- Accept only `ExtractionMode.PDF_EMBEDDED` and `ExtractionMode.OCR`.
- Preserve all existing fidelity, correction-audit, Markdown safety, and trusted-reuse checks.
- Do not modify generated Issue #3 artifacts manually.

---

### Task 1: Correct the Issue #3 PDF extraction contract

**Files:**
- Modify: `scripts/verify_issue_3_semantic.py`
- Create: `tests/unit/test_issue_3_verifier.py`

**Interfaces:**
- Consumes: `ExtractionMode` and `SemanticFidelityReport.extraction_mode`
- Produces: `_require_issue_3_pdf_mode(mode: ExtractionMode) -> None`

- [ ] **Step 1: Write the failing contract test**

```python
@pytest.mark.parametrize(
    "mode",
    (ExtractionMode.PDF_EMBEDDED, ExtractionMode.OCR),
)
def test_issue_3_verifier_accepts_pdf_extraction_modes(mode: ExtractionMode) -> None:
    _require_issue_3_pdf_mode(mode)


def test_issue_3_verifier_rejects_non_pdf_extraction_mode() -> None:
    with pytest.raises(Issue3VerificationError, match="ISSUE_3_NOT_PDF"):
        _require_issue_3_pdf_mode(ExtractionMode.DOCX_XML)
```

- [ ] **Step 2: Run the test and verify the embedded-text case fails**

Run: `uv run pytest -q tests/unit/test_issue_3_verifier.py`

Expected: FAIL because `_require_issue_3_pdf_mode` does not yet exist.

- [ ] **Step 3: Implement the minimal PDF-mode guard**

```python
def _require_issue_3_pdf_mode(mode: ExtractionMode) -> None:
    _require(
        mode in {ExtractionMode.PDF_EMBEDDED, ExtractionMode.OCR},
        "ISSUE_3_NOT_PDF",
    )
```

Call the guard from `verify_issue_3` in place of the OCR-only assertion and update the script description to say “semantic PDF artifact.”

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
uv run pytest -q tests/unit/test_issue_3_verifier.py
uv run ruff check .
uv run pytest -q
```

Expected: all commands pass.

- [ ] **Step 5: Commit the implementation**

```bash
git add scripts/verify_issue_3_semantic.py tests/unit/test_issue_3_verifier.py
git commit -m "fix: accept embedded PDF issue verifier input"
```

### Task 2: Validate the regenerated Issue #3 artifact

**Files:**
- Read: `products/500138301/generated/data-semantic.md`
- Read: `products/500138301/quality/semantic-fidelity.json`

**Interfaces:**
- Consumes: the regenerated PR #5 product tree
- Produces: a passing Issue #3 verifier result and CommonMark rendering evidence

- [ ] **Step 1: Run the artifact verifier**

Run: `uv run python scripts/verify_issue_3_semantic.py --product-root products/500138301`

Expected: JSON result with a non-failing status, `page_count` 5, and a stable Markdown SHA-256.

- [ ] **Step 2: Parse the generated Markdown as CommonMark/GFM**

Run a read-only `MarkdownIt("commonmark").enable("table")` parse and assert heading levels `[1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]`, 10 tables, zero raw HTML tokens, zero literal backslashes in rendered HTML, and the corrected Korean terms.

- [ ] **Step 3: Confirm GitHub state**

Confirm PR #5 quality checks pass and Issue #3 remains open until PR #5 is merged.

