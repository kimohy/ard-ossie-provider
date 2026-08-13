# OCR-first Semantic Markdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate semantic PDF Markdown from full-page OCR plus bounded image-grounded text corrections, with deterministic structure, trusted correction reuse, and no raw HTML.

**Architecture:** PDF parsing always creates one authoritative Docling OCR catalog and a deterministic page-image snapshot; a vision-capable provider may return only sparse recognition/spacing patches bound to that page and catalog. Deterministic code validates and applies patches before existing geometry-based structure reconciliation, while DOCX keeps OOXML authority. The renderer emits only Markdown constructs and the fidelity report stores correction bindings so exact-source reruns can reuse accepted text without another provider call.

**Tech Stack:** Python 3.12, Docling 2.114.0, pypdfium2 5.12.1, Pydantic 2, OpenAI 2.46.0, Google GenAI, Anthropic Vertex, JSON Schema Draft 2020-12, pytest, Ruff, uv

## Global Constraints

- Every semantic PDF uses whole-document, full-page OCR even when embedded text is available.
- DOCX remains OOXML-authoritative and receives no OCR or LLM text correction.
- LLM output may change only image-grounded character recognition or spacing; it may not summarize, paraphrase, translate, add, delete, or reorder content.
- Correction requests are page-local, sparse, content-addressed, pixel/byte/span bounded, and never log or persist image bytes.
- `data-semantic.md` contains no raw HTML; multiline table-cell text is joined with exactly one ordinary space.
- Provider absence, lack of vision capability, transient failure, or invalid patches retain original OCR text and produce `WARN`; OCR unreadability, source loss/duplication, invalid table allocation, or raw HTML are hard failures.
- Do not manually edit generated product or Registry artifacts; regenerate Issue #3 through the trusted workflow after merge.
- Add only five focused verification cases: OCR authority, parameterized correction validation, HTML-free rendering/table joining, one multimodal provider boundary, and one real Issue #3 acceptance script.
- During development run only the test targeted by the active task. Run the focused semantic/provider set once before publication, then Ruff/schema/workflow gates, real Issue #3 acceptance, and the complete suite exactly once unless a failure requires one targeted rerun.

---

## File Structure

- `src/ard_ossie/llm/contracts.py`: immutable internal text/image message blocks and the multimodal structured-generation protocol.
- `src/ard_ossie/llm/service.py`: retry/repair orchestration shared by text-only and multimodal structured calls.
- `src/ard_ossie/llm/openai_adapters.py`: translate internal multimodal blocks to Chat Completions and Responses payloads; Azure inherits the same translation.
- `src/ard_ossie/llm/vertex_adapters.py`: translate the same blocks to Gemini parts and Claude image sources.
- `src/ard_ossie/llm/profiles.py`, `src/ard_ossie/llm/factory.py`, `config/llm-profiles.yaml`: declare and verify the `vision` profile capability.
- `src/ard_ossie/semantic/correction.py`: page rasterization, correction schema/request binding, sparse patch validation/application, audit creation, and exact-record reuse.
- `src/ard_ossie/semantic/models.py`: correction plan/audit models and fidelity warning/count fields.
- `src/ard_ossie/semantic/sources.py`: keep OCR items at publishable block/table-cell granularity and expose no embedded-text authority to the parser.
- `src/ard_ossie/semantic/parser.py`: always OCR PDFs, apply correction before reconciliation, keep PDF structure deterministic, and assemble fidelity.
- `src/ard_ossie/semantic/render.py`: HTML-free headings/paragraphs/lists/tables, table newline joining, and final raw-HTML guard.
- `src/ard_ossie/docling_parser.py`, `src/ard_ossie/pipeline.py`, `src/ard_ossie/application/processing.py`: inject correction, load trusted fidelity, and preserve correction records through processing.
- `schemas/reports/semantic-fidelity.schema.json`: generated schema for the extended fidelity model.
- `tests/unit/semantic/test_correction.py`: the single parameterized correction validator case.
- `tests/unit/semantic/test_pdf_source.py`, `tests/integration/test_docling_pipeline.py`: the single always-OCR authority case and updates to obsolete embedded-PDF/HTML expectations.
- `tests/unit/semantic/test_render.py`: the single HTML-free/table-cell joining regression plus adjusted legacy expectations.
- `tests/unit/test_openai_adapters.py`: the single multimodal structured provider-boundary case.
- `tests/unit/test_llm_profiles.py`, existing pipeline/processing tests: update existing assertions for the new capability and trusted report argument without adding scenario matrices.
- `scripts/verify_issue_3_semantic.py`: real Issue #3 output and trusted-reuse acceptance.

---

### Task 1: Multimodal provider contract and profile capability

**Files:**
- Modify: `src/ard_ossie/llm/contracts.py`
- Modify: `src/ard_ossie/llm/service.py`
- Modify: `src/ard_ossie/llm/openai_adapters.py`
- Modify: `src/ard_ossie/llm/vertex_adapters.py`
- Modify: `src/ard_ossie/llm/profiles.py`
- Modify: `src/ard_ossie/llm/factory.py`
- Modify: `src/ard_ossie/llm/__init__.py`
- Modify: `config/llm-profiles.yaml`
- Modify: `tests/unit/test_openai_adapters.py`
- Modify: `tests/unit/test_llm_profiles.py`

**Interfaces:**
- Consumes: existing `LLMResult`, `LLMMetadata`, provider retry policy, and structured-output adapters.
- Produces: `LLMTextPart`, `LLMImagePart`, `LLMMultimodalMessage`, and `LLMProvider.generate_multimodal_structured(*, schema, messages) -> LLMResult`; profiles expose `vision: bool` and runtime capabilities expose the identical boolean.

- [ ] **Step 1: Write the one multimodal provider-boundary regression**

Add one test that constructs a `LLMMultimodalMessage` with one PNG byte payload, invokes `OpenAICompatibleProvider.generate_multimodal_structured`, and asserts the recorded Chat Completions request contains one text part and one `data:image/png;base64,...` image URL while the result validates against the supplied closed schema. Extend the existing packaged-profile assertion with `assert profile.vision is True`; do not add a second profile test.

```python
message = LLMMultimodalMessage(
    role="user",
    content=(
        LLMTextPart(text="Correct only the supplied OCR spans."),
        LLMImagePart(mime_type="image/png", data=b"\x89PNG\r\n\x1a\nfixture"),
    ),
)
result = provider.generate_multimodal_structured(schema=schema, messages=[message])
content = client.chat.completions.calls[0]["messages"][0]["content"]
assert content[0] == {"type": "text", "text": message.content[0].text}
assert content[1]["type"] == "image_url"
assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
assert result.structured == {"patches": []}
```

- [ ] **Step 2: Run the boundary test to verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q tests/unit/test_openai_adapters.py -k multimodal`

Expected: FAIL because the multimodal message models and provider method do not exist.

- [ ] **Step 3: Add immutable internal message models and protocol method**

Define closed, frozen Pydantic models in `contracts.py`. Keep image bytes `repr=False`, permit only PNG/JPEG, cap one image at 8 MiB, and cap a message at one image plus bounded text.

```python
class LLMTextPart(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=200_000)


class LLMImagePart(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["image"] = "image"
    mime_type: Literal["image/png", "image/jpeg"]
    data: bytes = Field(min_length=1, max_length=8 * 1024 * 1024, repr=False)


class LLMMultimodalMessage(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["system", "user", "assistant"]
    content: tuple[LLMTextPart | LLMImagePart, ...] = Field(min_length=1, max_length=8)
```

Add `generate_multimodal_structured` to `LLMProvider`. In `LLMService`, reuse the current three-attempt structured retry/repair budget; a repair prepends a system `LLMTextPart` and retains the original page image unchanged.

- [ ] **Step 4: Translate internal blocks in all four provider adapters**

For Chat Completions emit `text` and `image_url` content blocks; for Responses emit `input_text` and `input_image`; for Gemini emit `types.Part.from_text` and `types.Part.from_bytes`; for Claude emit `text` and base64 `image` source blocks. Keep schema parsing, error classification, metadata, and secret-safe `repr` behavior unchanged. `AzureOpenAIProvider` inherits the OpenAI translation.

- [ ] **Step 5: Bind declared and runtime vision capability**

Add required `vision: bool` to `_BaseProfile`, pass it from `LLMProviderFactory` to every provider constructor, return it from `capabilities()`, and reject a factory result whose runtime value differs from the profile. Set the packaged `openai-compatible-default` profile to `vision: true`.

```yaml
  openai-compatible-default:
    provider: openai_compatible
    model: gpt-5.6-terra
    structured_output: native
    vision: true
```

- [ ] **Step 6: Run only the task tests and commit**

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q tests/unit/test_openai_adapters.py -k multimodal tests/unit/test_llm_profiles.py -k packaged`

Expected: `2 passed`.

```bash
git add src/ard_ossie/llm config/llm-profiles.yaml tests/unit/test_openai_adapters.py tests/unit/test_llm_profiles.py
git commit -m "feat: add bounded multimodal LLM requests"
```

---

### Task 2: Sparse OCR correction model, validation, and reuse

**Files:**
- Create: `src/ard_ossie/semantic/correction.py`
- Modify: `src/ard_ossie/semantic/models.py`
- Modify: `src/ard_ossie/semantic/__init__.py`
- Create: `tests/unit/semantic/test_correction.py`

**Interfaces:**
- Consumes: `NativeDocument`, page-local `SourceSpan` objects with normalized boxes, multimodal structured generation from Task 1, and optional trusted `SemanticFidelityReport` records.
- Produces: `OcrCorrectionPlanner.correct(source, native, *, trusted_fidelity, pdfium) -> OcrCorrectionApplication`, where the application contains an immutable corrected `NativeDocument`, page audit records, and deterministic warning codes.

- [ ] **Step 1: Write one parameterized validator test**

Create one `test_image_grounded_patch_validation` function. Its valid row changes `개 인정보` to `개인정보`. Parameter rows exercise exactly these rejection codes: unknown span, hash mismatch, box mismatch, duplicate span, empty/oversized text, line-block change, non-allowlisted kind, low confidence, control character/raw tag, and request-binding mismatch. Each rejected page must return the original catalog with no partial mutation and must store only the rejected text hash.

```python
@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        pytest.param(lambda patch: patch | {"span_id": "span_0000000000000000"}, "SEMANTIC_OCR_CORRECTION_UNKNOWN_SPAN", id="unknown-span"),
        pytest.param(lambda patch: patch | {"original_text_hash": "0" * 64}, "SEMANTIC_OCR_CORRECTION_HASH_MISMATCH", id="hash"),
        pytest.param(lambda patch: patch | {"confidence": 0.49}, "SEMANTIC_OCR_CORRECTION_CONFIDENCE_LOW", id="confidence"),
    ],
)
def test_image_grounded_patch_validation(mutate, expected_code):
    application = validate_page_corrections(snapshot, {"patches": [mutate(valid_patch)]})
    assert application.warning_codes == (expected_code,)
    assert application.spans == snapshot.spans
    assert application.audit.patches[0].corrected_text is None
```

Keep all rejection rows in this one parametrized function; do not create a test per code.

- [ ] **Step 2: Run the validator test to verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q tests/unit/semantic/test_correction.py`

Expected: collection FAIL because `semantic.correction` and its models do not exist.

- [ ] **Step 3: Define closed correction and audit models**

Add `OcrCorrectionPatch`, `OcrCorrectionPlan`, `OcrCorrectionPatchAudit`, and `OcrCorrectionPageAudit`. A page audit binds `source_hash`, one-based page, `page_image_hash`, ordered OCR catalog hash, prompt version/hash, schema hash, provider, model, retry/repair counts, outcome, provider error code, and patch audits. Patch audit stores accepted/reused corrected text and hash; rejected output stores only `rejected_text_hash` plus one validation code.

Extend `SemanticFidelityReport` with defaulted `ocr_corrections`, `ocr_correction_applied_count`, `ocr_correction_rejected_count`, and `warning_codes`. Validate the counts and require `WARN` only when warning codes or degraded blocks exist; OCR with fully valid correction is allowed to be `PASS`.

- [ ] **Step 4: Implement page snapshot and schema binding**

In `correction.py`, define fixed limits: maximum 200 pages, 16 million decoded pixels per page, 8 MiB encoded image bytes, 2,000 spans per page, 2x corrected-text growth, and confidence threshold `0.80`. Render from `source.snapshot` through injected/default pypdfium2 at fixed scale `2.0`, encode PNG deterministically, close every document/page handle, and hash the exact bytes sent to the provider.

Build a closed schema whose only top-level field is sparse `patches`; include `span_id`, `original_text_hash`, `corrected_text`, `correction_kind`, copied `bbox`, and `confidence`. Bind the request by hashing canonical source/page/image/catalog/prompt/schema/provider/model values.

- [ ] **Step 5: Implement all-or-nothing page validation and trusted reuse**

`validate_page_corrections` validates every patch against the page allowlist before replacing any span. Reject duplicate IDs, mismatched hashes/boxes, empty or excessive text, changed newline count, kinds outside `character_recognition|spacing`, confidence below `0.80`, control characters, and raw HTML tags. Accepted spans retain ID/ordinal/page/box and receive corrected text plus its new hash.

Before a provider call, select a trusted page audit only when every binding value matches. Reapply only its accepted patch text, mark the new audit `reused`, and do not invoke provider. When no provider/vision is available, return unchanged spans with `SEMANTIC_OCR_CORRECTION_UNAVAILABLE`; provider/validation failures similarly return unchanged page spans and a stable warning.

- [ ] **Step 6: Run only the correction test and commit**

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q tests/unit/semantic/test_correction.py`

Expected: all parameter rows pass in the single test function.

```bash
git add src/ard_ossie/semantic/correction.py src/ard_ossie/semantic/models.py src/ard_ossie/semantic/__init__.py tests/unit/semantic/test_correction.py
git commit -m "feat: validate sparse OCR corrections"
```

---

### Task 3: Always-OCR PDF orchestration and deterministic structure

**Files:**
- Modify: `src/ard_ossie/semantic/sources.py`
- Modify: `src/ard_ossie/semantic/parser.py`
- Modify: `src/ard_ossie/docling_parser.py`
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `tests/unit/semantic/test_pdf_source.py`
- Modify: `tests/integration/test_docling_pipeline.py`
- Modify: existing parser/pipeline fixtures that construct `DoclingParser` or `_processing_parser`

**Interfaces:**
- Consumes: `OcrCorrectionPlanner` and `OcrCorrectionApplication` from Task 2; the existing `DoclingParser.parse(source) -> ParsedDocument` public boundary stays unchanged.
- Produces: PDF `NativeDocument` objects that are always `ExtractionMode.OCR`, corrected before `reconcile_structure`; DOCX flow and `SemanticStructureRepairPlanner` remain available only for DOCX unresolved structure.

- [ ] **Step 1: Convert the existing PDF authority case to the single always-OCR regression**

Use a fake PDFium text page that visibly contains embedded text and a recording full-page OCR converter that returns `structured_ocr_document()`. Assert the parsed Markdown contains the OCR catalog, excludes embedded text, records `ExtractionMode.OCR`, and calls only the full-page OCR converter once.

```python
parsed = DoclingParser(
    converter=ordinary_converter_that_must_not_run,
    full_page_ocr_converter=full_page_ocr_converter,
    pdfium=FakePdfium(["EMBEDDED TEXT MUST NOT WIN"]),
).parse(semantic_pdf_source(tmp_path))
assert parsed.semantic_fidelity.extraction_mode is ExtractionMode.OCR
assert "개인정보" in parsed.markdown
assert "EMBEDDED TEXT MUST NOT WIN" not in parsed.markdown
assert len(full_page_ocr_converter.converted_paths) == 1
```

- [ ] **Step 2: Run the authority test to verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q tests/integration/test_docling_pipeline.py -k always_uses_full_page_ocr`

Expected: FAIL because readable embedded PDF text still selects `extract_pdf_native`.

- [ ] **Step 3: Make PDF authority unconditional OCR**

In `_native_and_structure`, remove the embedded-text branch: every `.pdf` invokes `_convert_with_full_page_ocr`, passes that same Docling document to `extract_ocr_native` and `build_docling_skeleton`, and rejects an empty catalog. Keep `extract_pdf_native` callable for compatibility tests but never call it from production parsing.

Ensure `extract_ocr_native` emits one source span per Docling publishable text item and one per table cell, never lower-level OCR words or glyphs. Derive native group metadata from the Docling item/table geometry; structure type/order/table coordinates continue to come from deterministic Docling evidence, not the LLM response.

- [ ] **Step 4: Apply text correction before reconciliation**

Extend `parse_semantic_document` and `DoclingParser` with optional correction planner and trusted fidelity inputs. For PDFs, call correction immediately after native extraction, then reconcile the corrected catalog against the same OCR skeleton. Skip structure-repair provider calls for OCR documents; unresolved OCR groups become ordinary `LosslessBlock` allocations with `structure_unresolved`, which the HTML-free renderer will emit as paragraphs. DOCX continues to use its current structure-repair path.

- [ ] **Step 5: Inject the same configured service for OCR correction**

In `_processing_parser`, construct both `OcrCorrectionPlanner(provider)` and `SemanticStructureRepairPlanner(provider)` when a provider exists. Pass trusted fidelity through `process_product` without changing the public parse return shape. Provider-free PDF processing must still publish unchanged OCR with the unavailable warning.

- [ ] **Step 6: Update only obsolete behavior assertions and run the authority test**

Replace parser expectations that specifically require `pdf_embedded`, `<pre>`, or `<br>`; retain all coverage, table-allocation, DOCX authority, and error assertions. Do not add parallel scenarios.

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q tests/integration/test_docling_pipeline.py -k 'always_uses_full_page_ocr or full_page_ocr_conversion_error or full_page_ocr_empty'`

Expected: the three focused OCR-path cases pass.

```bash
git add src/ard_ossie/semantic/sources.py src/ard_ossie/semantic/parser.py src/ard_ossie/docling_parser.py src/ard_ossie/pipeline.py tests/unit/semantic/test_pdf_source.py tests/integration/test_docling_pipeline.py
git commit -m "feat: make full-page OCR authoritative for PDFs"
```

---

### Task 4: HTML-free deterministic Markdown rendering

**Files:**
- Modify: `src/ard_ossie/semantic/render.py`
- Modify: `tests/unit/semantic/test_render.py`
- Modify: `tests/integration/test_docling_pipeline.py`

**Interfaces:**
- Consumes: existing `ReconciledDocument` blocks and corrected/native `SourceSpan` catalog.
- Produces: `render_semantic_markdown(...) -> str` containing only ATX headings, ordinary paragraphs, GFM lists/tables, and blank separators; raises `SemanticRawHtmlError("SEMANTIC_RAW_HTML_OUTPUT")` if a raw HTML element survives.

- [ ] **Step 1: Consolidate the renderer requirement into one regression**

Replace the old multiline-table `<br>` case with one test whose source contains `<pre>`, `<br>`, a multiline cell, a pipe, and a backslash. Assert angle brackets are backslash-escaped, the cell is `첫 줄 둘째 줄 셋째 줄`, pipes/backslashes are escaped, the result has no raw tag match, and no literal `<pre>`/`<br>` appears.

```python
rendered = render_semantic_markdown(semantic, native.span_catalog())
assert "<pre>" not in rendered and "<br>" not in rendered
assert "\\<pre\\>" in rendered
assert "첫 줄 둘째 줄 셋째 줄" in rendered
assert RAW_HTML_TAG.search(rendered) is None
```

- [ ] **Step 2: Run the renderer regression to verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q tests/unit/semantic/test_render.py -k html_free`

Expected: FAIL because `LosslessBlock` and line breaks still emit raw HTML and table cells use `<br>`.

- [ ] **Step 3: Remove every HTML-producing render path**

Render `LosslessBlock` through the ordinary paragraph escape path. Encode an internal non-table newline as a Markdown hard break (`\\\n`) rather than `<br>`. Backslash-escape angle brackets as source punctuation. For a table cell, normalize CRLF/CR to LF, trim whitespace adjacent to each internal LF, join lines and paragraph boundaries with one ordinary space, then escape backslash and pipe.

- [ ] **Step 4: Add a final raw-HTML guard**

After joining blocks, scan for raw HTML open/close tags, comments, declarations, and processing instructions. Raise the stable hard-failure code before returning Markdown. The guard examines rendered output only and never blocks backslash-escaped source angle brackets.

- [ ] **Step 5: Adjust existing expectations and run only renderer tests**

Update existing expected strings from `<pre>text</pre>` to escaped ordinary paragraphs and from `<br>` to Markdown hard breaks or table spaces. Keep the existing source coverage and GFM structural assertions.

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q tests/unit/semantic/test_render.py`

Expected: the renderer file passes with no new test functions beyond the consolidated regression.

```bash
git add src/ard_ossie/semantic/render.py tests/unit/semantic/test_render.py tests/integration/test_docling_pipeline.py
git commit -m "fix: render semantic Markdown without HTML"
```

---

### Task 5: Fidelity persistence, trusted-base loading, and schema synchronization

**Files:**
- Modify: `src/ard_ossie/semantic/parser.py`
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `src/ard_ossie/application/processing.py`
- Modify: `schemas/reports/semantic-fidelity.schema.json`
- Modify: existing `tests/unit/test_pipeline.py`, `tests/unit/test_processing_service.py`, and schema tests only where current fixtures require the new defaulted fields/argument

**Interfaces:**
- Consumes: page audit records and warning codes from Tasks 2-3; existing quality artifact hash map and raw-byte Git revision reader.
- Produces: `semantic-fidelity.json` containing validated `ocr_corrections`; `ProcessingService` supplies a hash-verified prior `SemanticFidelityReport` to the parser for exact binding reuse.

- [ ] **Step 1: Assemble correction status into fidelity**

Pass page audits and warning codes into `_build_fidelity`. Compute applied/reused and rejected/provider-failed counts from audits. `PASS` requires complete unique allocation, no degraded blocks, no correction warnings, and successful HTML-free rendering. `WARN` covers unavailable correction, provider failure, rejected page patches, or unresolved structure. Preserve current `warnings_as_errors` behavior by translating each fidelity warning code to one `QualityFinding` without duplicating codes.

- [ ] **Step 2: Load trusted fidelity bytes from the base revision**

Generalize the existing trusted semantic artifact helper so both `semantic-structure-repair.json` and `semantic-fidelity.json` are read as raw bytes from the exact base SHA and independently verified against `quality-report.json` `quality_artifact_hashes`. A missing old fidelity record yields `None`; present-but-unhashed, hash-mismatched, malformed, or non-object bytes raise `SEMANTIC_REPAIR_TRUST_MISMATCH` before provider use.

Add `trusted_semantic_fidelity: dict[str, object] | None = None` to `process_product`, validate it as `SemanticFidelityReport`, and pass it to `DoclingParser`. Preserve the old `trusted_semantic_repair` parameter for DOCX compatibility.

- [ ] **Step 3: Synchronize the checked-in fidelity schema**

Regenerate only `schemas/reports/semantic-fidelity.schema.json` from `SemanticFidelityReport.model_json_schema()` with `ensure_ascii=False`, two-space indentation, sorted keys, and one final LF. Do not hand-edit generated schema fragments.

- [ ] **Step 4: Run the existing schema gate and focused integration set once**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen python -m ard_ossie.application.model_schema_verification --repository .
UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q \
  tests/unit/semantic/test_correction.py \
  tests/unit/semantic/test_render.py \
  tests/integration/test_docling_pipeline.py \
  tests/unit/test_processing_service.py -k 'semantic or ocr or trusted'
```

Expected: model-schema verification exits `0`; all selected cases pass.

- [ ] **Step 5: Commit the audit/trust boundary**

```bash
git add src/ard_ossie/semantic/parser.py src/ard_ossie/pipeline.py src/ard_ossie/application/processing.py schemas/reports/semantic-fidelity.schema.json tests
git commit -m "feat: persist and reuse OCR correction audit"
```

---

### Task 6: Real Issue #3 acceptance, bounded final verification, and publication

**Files:**
- Create: `scripts/verify_issue_3_semantic.py`
- Modify: `docs/superpowers/specs/2026-08-13-ocr-first-semantic-markdown-design.md` only if implemented interface names differ while preserving approved decisions
- No manual changes: `products/500138301/generated/**`, `products/500138301/quality/**`, `registry/**`

**Interfaces:**
- Consumes: a trusted-workflow-generated PR #5 product tree containing the genuine five-page PDF, generated Markdown, quality report, and fidelity report.
- Produces: deterministic acceptance output and a reviewed feature PR; after merge, Issue #3 is replayed through its official label path and PR #5 receives regenerated artifacts.

- [ ] **Step 1: Write the single real acceptance script**

The script accepts `--product-root` and imports production parsing code. It verifies:

```python
assert fidelity.extraction_mode is ExtractionMode.OCR
assert fidelity.page_count == 5
assert fidelity.unmatched_span_count == 0
assert fidelity.duplicated_span_count == 0
assert fidelity.status != "FAIL"
assert RAW_HTML_TAG.search(markdown) is None
assert "<pre>" not in markdown and "<br>" not in markdown
assert "개인정보" in markdown and "유효성" in markdown
assert GFM_SEPARATOR_ROW.search(markdown)
assert all(
    patch.bbox is not None and patch.original_text_hash
    for page in fidelity.ocr_corrections
    for patch in page.patches
    if patch.outcome in {"applied", "reused"}
)
```

Then parse the same PDF once with the stored fidelity as trusted input and a provider stub that raises if called. Assert the reused Markdown bytes exactly equal `generated/data-semantic.md` and every formerly applied page audit is now `reused`. The script prints only counts/hashes and never source text, image bytes, prompts, or secrets.

- [ ] **Step 2: Run the bounded pre-publication verification**

Run the focused set once:

```bash
UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q \
  tests/unit/semantic \
  tests/unit/test_llm_service.py \
  tests/unit/test_openai_adapters.py \
  tests/unit/test_vertex_adapters.py \
  tests/unit/test_llm_profiles.py \
  tests/unit/test_llm_factory.py \
  tests/integration/test_docling_pipeline.py
UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen ruff check .
UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen python -m ard_ossie.application.model_schema_verification --repository .
```

Expected: focused tests, Ruff, and model-schema gate pass. Do not run the full suite at this step.

- [ ] **Step 3: Commit the acceptance script and review the exact diff**

```bash
git add scripts/verify_issue_3_semantic.py docs/superpowers/specs/2026-08-13-ocr-first-semantic-markdown-design.md
git commit -m "test: add Issue 3 OCR acceptance"
git diff --check origin/main...HEAD
git status --short
```

Expected: no conflict markers/whitespace errors and a clean worktree.

- [ ] **Step 4: Run the complete suite exactly once**

Run: `UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen pytest -q`

Expected: all tests pass. If it fails, fix only the observed regression and rerun only that target; run the complete suite a second time only after the targeted fix is green.

- [ ] **Step 5: Publish and merge the processor change through protected GitHub flow**

Push `agent/ocr-first-semantic-markdown`, open a focused PR to `main`, wait for exact-head repository gates, inspect the complete diff, and merge only when the exact head is green and review has no Critical or Important findings. Do not force-push or manually modify protected branches.

- [ ] **Step 6: Replay Issue #3 and run the real acceptance once**

After the processor PR merges, remove and re-add only the `ard:approved` label on Issue #3 to trigger the trusted workflow. Wait for the managed PR #5 head to advance and for `ard/quality-gate` and `ard/changeset` to refer to that exact head. Materialize its LFS source and run:

```bash
UV_CACHE_DIR=/tmp/ard-ocr-first-uv-cache uv run --frozen python scripts/verify_issue_3_semantic.py \
  --product-root products/500138301
```

Expected: five OCR pages, zero raw HTML/unmatched/duplicated spans, intact `개인정보`/`유효성`, at least one GFM table, valid correction evidence, and byte-identical trusted reuse.

- [ ] **Step 7: Finish PR #5 only after acceptance**

Review regenerated product/quality artifacts, require exact-head checks, mark PR #5 ready, and merge with expected-head protection only if the acceptance script and review are clean. Verify `main` contains the merged product, Issue #3 has no `ard:failed`, and no generated or Registry file was manually edited.
