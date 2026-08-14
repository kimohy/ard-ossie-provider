# Adaptive Semantic PDF Fidelity Implementation Plan

> **For Codex:** Use `superpowers:executing-plans` and implement each task with strict RED/GREEN TDD. Run only the named focused tests; do not run the full suite locally.

**Goal:** Produce faithful, pure-Markdown semantic documents by preferring usable embedded PDF text, applying image-grounded LLM correction to every PDF extraction mode, using explicit Korean/English OCR only as a fallback, and blocking publication whenever correction or structure fidelity is unresolved.

**Architecture:** PDF text acquisition and semantic interpretation remain separate. The parser first extracts an immutable span catalog from embedded text when every page is readable; otherwise it builds the catalog with full-page Korean/English OCR. A visual correction planner may make only image-supported character-recognition or spacing patches to either PDF catalog. Docling/LLM structure planning then allocates the exact corrected span IDs into Markdown blocks without authoring text. Rendering rejects HTML syntax/entities, and both processing and release gates fail closed on unresolved correction or degraded structure.

**Tech Stack:** Python 3.12, Pydantic, pypdfium2, Docling 2.114.0 with EasyOCR, pytest, Ruff, uv.

---

## Task 1: Prefer embedded PDF text and use Korean/English OCR fallback

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/ard_ossie/semantic/parser.py`
- Modify: `tests/integration/test_docling_pipeline.py`

### Step 1: Write the failing embedded-text preference test

Replace the existing always-OCR regression with a test that supplies a complete `FakePdfium` embedded span catalog and asserts:

```python
assert parsed.semantic_fidelity.extraction_mode is ExtractionMode.PDF_EMBEDDED
assert converter.converted_paths
assert full_page_ocr_converter.converted_paths == []
assert correction_planner.call_count == 1
assert parsed.markdown.startswith("# 개인정보")
```

The test catches a parser that unnecessarily discards valid embedded Korean text and also catches a parser that skips visual correction merely because extraction was embedded.

### Step 2: Verify RED

Run:

```bash
.venv/bin/python -m pytest -q tests/integration/test_docling_pipeline.py -k 'embedded_text'
```

Expected: failure because the current parser always invokes full-page OCR and reports `ExtractionMode.OCR`.

### Step 3: Implement native-first selection and PDF-wide visual correction

In `semantic/parser.py`:

- import and call `extract_pdf_native` for PDFs;
- when it returns a non-empty complete native catalog, build the ordinary Docling structure from the same source;
- when it returns `None`/unreadable/incomplete, retain the existing whole-document full-page OCR fallback;
- invoke the correction planner for both `PDF_EMBEDDED` and `OCR`, but never for DOCX;
- if either PDF extraction mode has no planner, record `SEMANTIC_OCR_CORRECTION_UNAVAILABLE` so the later hard gate prevents publication.

Do not merge structure recognition into correction: correction changes only span text/hash; structure planning continues to allocate span IDs.

### Step 4: Verify GREEN for source selection

Run:

```bash
.venv/bin/python -m pytest -q tests/integration/test_docling_pipeline.py -k 'embedded_text or partial_pdf or embedded_pdf_cannot_open'
```

Expected: focused tests pass and incomplete/unreadable embedded PDFs still use OCR.

### Step 5: Write the failing explicit OCR language test

Update the converter-construction regression to provide a fake `EasyOcrOptions` and assert the consumer-visible configuration:

```python
assert options.ocr_options.force_full_page_ocr is True
assert options.ocr_options.lang == ["ko", "en"]
```

The test catches accidental use of auto-selected OCR languages such as RapidOCR's Chinese default.

### Step 6: Verify RED, implement, and verify GREEN

Run the one test and confirm it fails because `OcrAutoOptions` has no explicit languages. Then:

- construct Docling PDF options with `EasyOcrOptions(lang=["ko", "en"], force_full_page_ocr=True)`;
- change the pinned dependency to `docling[easyocr]==2.114.0` and refresh `uv.lock` with `uv lock`.

Run:

```bash
.venv/bin/python -m pytest -q tests/integration/test_docling_pipeline.py -k 'full_page_ocr_converter'
```

Expected: pass.

### Step 7: Commit

```bash
git add pyproject.toml uv.lock src/ard_ossie/semantic/parser.py tests/integration/test_docling_pipeline.py
git commit -m "fix: prefer embedded PDF text with Korean OCR fallback"
```

---

## Task 2: Apply bounded visual correction to embedded and OCR catalogs

**Files:**
- Modify: `src/ard_ossie/semantic/correction.py`
- Modify: `tests/unit/semantic/test_correction.py`
- Modify: `tests/integration/test_docling_pipeline.py`

### Step 1: Write failing correction-mode tests

Add a parameterized correction test covering `PDF_EMBEDDED` and `OCR`. Give each a page-evidenced span and controlled image/provider response, then assert a supported spacing or single-character patch is applied. Retain a DOCX case asserting no provider/image work occurs.

Also add a test that checks the correction request prompt describes a generic extracted PDF span catalog rather than claiming every input is OCR.

### Step 2: Verify RED

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/semantic/test_correction.py -k 'embedded or extraction_mode or prompt'
```

Expected: the embedded case returns unchanged without a provider request.

### Step 3: Implement the mode boundary

- permit correction when `native.extraction_mode` is either `PDF_EMBEDDED` or `OCR`;
- retain the same image-grounded sparse-patch schema and strict character/whitespace validation;
- rename the human-facing prompt wording to “extracted PDF span catalog” and bump the prompt version so trusted audit reuse cannot cross prompt contracts;
- retain existing audit model/field names for backward compatibility; do not weaken no-add/no-delete/no-paraphrase validation.

### Step 4: Verify GREEN

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/semantic/test_correction.py tests/integration/test_docling_pipeline.py -k 'correction or embedded_text'
```

Expected: relevant correction and integration tests pass.

### Step 5: Commit

```bash
git add src/ard_ossie/semantic/correction.py tests/unit/semantic/test_correction.py tests/integration/test_docling_pipeline.py
git commit -m "fix: visually correct all PDF text catalogs"
```

---

## Task 3: Emit pure Markdown and fail closed on degraded semantic fidelity

**Files:**
- Modify: `src/ard_ossie/semantic/render.py`
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `src/ard_ossie/release.py`
- Modify: `tests/unit/semantic/test_render.py`
- Modify: `tests/integration/test_cli_process.py`
- Modify: `tests/unit/test_release.py`

### Step 1: Write the failing pure-Markdown rendering test

Change the boundary-whitespace regression to assert that rendered Markdown contains neither raw HTML nor HTML entities:

```python
assert not has_raw_html(rendered)
assert not re.search(r"&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);", rendered)
assert "&#32;" not in rendered
assert "&#9;" not in rendered
```

Use representative leading/trailing spaces and tabs. The expected Markdown should preserve readable content while normalizing layout-only block-boundary whitespace rather than encoding it as HTML.

### Step 2: Verify RED, implement pure Markdown, verify GREEN

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/semantic/test_render.py -k 'whitespace or html'
```

Expected RED: current renderer emits `&#32;`/`&#9;`.

Implement:

- normalize layout-only leading/trailing inline whitespace to plain Markdown-safe text;
- never synthesize HTML tags or entities;
- add a final entity detector alongside the existing raw-HTML detector and raise an explicit semantic Markdown error if an entity reaches output.

Rerun the same test selection and expect GREEN.

### Step 3: Write failing processing/release gate tests

Add one focused processing test and one release test for fidelity with full text coverage but either:

- a correction warning/rejected page audit; or
- `degraded_block_count > 0`.

Assert processing records a hard error and release raises `SEMANTIC_FIDELITY_GATE_FAILED`. The tests catch the current WARN-only path that publishes known-garbled documents.

### Step 4: Verify RED

Run:

```bash
.venv/bin/python -m pytest -q tests/integration/test_cli_process.py tests/unit/test_release.py -k 'semantic and (correction or degraded or fidelity)'
```

Expected: the new WARN-fidelity cases are currently accepted.

### Step 5: Implement fail-closed fidelity gates

In `pipeline._semantic_hard_findings` and `release._verify_semantic_fidelity_snapshot`, reject PDF fidelity when any of the following is true:

- report status is `FAIL`;
- coverage is below 1.0, or spans are unmatched/duplicated;
- `warning_codes` is non-empty;
- any page correction audit has outcome `rejected`;
- a PDF report has no page correction audits (visual correction unavailable);
- `degraded_block_count` is greater than zero.

Return explicit codes/messages in processing diagnostics and keep release's stable `SEMANTIC_FIDELITY_GATE_FAILED` public code. DOCX reports are not required to have page correction audits.

### Step 6: Verify GREEN

Run:

```bash
.venv/bin/python -m pytest -q tests/unit/semantic/test_render.py tests/integration/test_cli_process.py tests/unit/test_release.py -k 'semantic or whitespace or html'
```

Expected: pass.

### Step 7: Commit

```bash
git add src/ard_ossie/semantic/render.py src/ard_ossie/pipeline.py src/ard_ossie/release.py tests/unit/semantic/test_render.py tests/integration/test_cli_process.py tests/unit/test_release.py
git commit -m "fix: block degraded semantic Markdown publication"
```

---

## Task 4: Focused verification and delivery

**Files:**
- Verify only; modify no production files unless a focused regression identifies a defect.

### Step 1: Run the bounded affected test set

```bash
.venv/bin/python -m pytest -q \
  tests/unit/semantic/test_correction.py \
  tests/unit/semantic/test_render.py \
  tests/integration/test_docling_pipeline.py \
  tests/integration/test_cli_process.py \
  tests/unit/test_release.py
```

Do not run the full suite locally.

### Step 2: Run static checks only on changed Python files

```bash
.venv/bin/ruff check \
  src/ard_ossie/semantic/parser.py \
  src/ard_ossie/semantic/correction.py \
  src/ard_ossie/semantic/render.py \
  src/ard_ossie/pipeline.py \
  src/ard_ossie/release.py \
  tests/unit/semantic/test_correction.py \
  tests/unit/semantic/test_render.py \
  tests/integration/test_docling_pipeline.py \
  tests/integration/test_cli_process.py \
  tests/unit/test_release.py
```

### Step 3: Review the diff

```bash
git diff --check
git status --short
git diff --stat origin/main...HEAD
```

Confirm there are no raw HTML/entity emitters, no auto-language OCR configuration, and no WARN-only publication path for correction/structure degradation.

### Step 4: Finish branch

Follow `superpowers:verification-before-completion` and `superpowers:finishing-a-development-branch`. Report focused test counts and explicitly state that the full local suite was intentionally not run. Only after CI is green should the semantic source be reprocessed from immutable Issue #3 attachments, and generated PR output must be inspected before publication/merge.
