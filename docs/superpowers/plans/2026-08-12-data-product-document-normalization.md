# Data Product Document Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a concise, evidence-grounded `data-product.md` that omits portal boilerplate and empty sections while continuing to emit a schema-valid `generated/ossie-model.json` in the same processing run.

**Architecture:** Keep the existing Docling parse, single strict LLM request, canonical pipeline, and Ossie compiler. Add item-level HTML evidence, a closed `product_facts` response collection, deterministic fact validation and ordering, and a renderer context that maps only accepted facts into fixed Markdown sections. Explicit `product.yaml` descriptions seed the overview without requiring LLM evidence; every LLM-derived fact must cite the product HTML hash with a non-empty excerpt.

**Tech Stack:** Python 3.12, Pydantic 2, Docling, JSON Schema, Jinja2, pytest, Ruff, Hatchling

## Global Constraints

- Remove `## Parsed source`; never publish the raw parsed HTML Markdown as a fallback.
- Accept only the fact kinds and section order defined in `docs/superpowers/specs/2026-08-12-data-product-document-normalization-design.md`.
- Omit unavailable facts and whole empty sections; never render `미제공`, `N/A`, blank headings, or placeholder text.
- Always exclude fields explicitly labeled as AI-generated summaries.
- Require every LLM-derived product fact to cite `product_html` evidence with the exact source hash and a non-empty excerpt.
- Omit facts below confidence `0.7`, deduplicate identical facts, and fail closed on conflicting singleton facts.
- Preserve provider-free processing, current semantic suggestion behavior, current product/table version rules, Ossie 0.1.1 compilation, and `products/{product_key}/generated/ossie-model.json`.
- Do not change `data-semantic.md`, `data-dictionary.json`, or the Ossie schema mapping.

---

### Task 1: Capture item-level HTML evidence without page provenance

**Files:**
- Modify: `src/ard_ossie/docling_parser.py`
- Test: `tests/integration/test_docling_pipeline.py`

**Interfaces:**
- Consumes: `DoclingParser.parse(source: SourceFile) -> ParsedDocument`
- Produces: one `Evidence` item per text-bearing Docling item even when `item.prov` is empty, with `locator.document`, `locator.item_index`, `locator.level`, and a maximum 500-character `excerpt`

- [x] **Step 1: Write the failing no-page HTML evidence test**

Add a fake Docling document whose text item has `prov=[]`. Assert the parsed evidence is exactly:

```python
Evidence(
    source_hash="a" * 64,
    role=SourceRole.PRODUCT_HTML,
    locator={
        "document": "product-info/product.html",
        "item_index": 0,
        "level": 2,
    },
    excerpt="사용자가 입력한 제품 목적",
)
```

Also update the existing page-provenance assertion to include `document`.

- [x] **Step 2: Run the focused test and verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/integration/test_docling_pipeline.py -q`

Expected: FAIL because `_collect_evidence` skips items with no provenance and does not include the document path for page evidence.

- [x] **Step 3: Implement item-level evidence collection**

In `_collect_evidence`, create the base locator before iterating provenance:

```python
base_locator: dict[str, JsonValue] = {
    "document": source.relative_path,
    "item_index": item_index,
    "level": level,
}
```

For each provenance entry, copy the base locator and add page/bbox/charspan. When there is no provenance and `text` is non-empty, append one evidence item using the base locator and `str(text)[:500]`.

- [x] **Step 4: Run the focused test and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/integration/test_docling_pipeline.py -q`

Expected: all Docling integration tests pass.

### Task 2: Add the strict product-fact response and typed IR contracts

**Files:**
- Modify: `src/ard_ossie/llm.py`
- Modify: `src/ard_ossie/ir.py`
- Test: `tests/unit/test_llm.py`

**Interfaces:**
- Produces: `ProductFactKind`, `ProductFactSuggestion(kind, value, confidence, evidence)`, and `ProductFactIR(kind, value, evidence)`
- Produces: `semantic_extraction_schema()` with required top-level `product_facts`
- Produces: `ProductIR.product_facts: list[ProductFactIR]`
- Removes: `ProductIR.product_document_markdown`

- [x] **Step 1: Write failing schema and value-normalization tests**

Assert that `semantic_extraction_schema()`:

```python
assert schema["required"] == ["suggestions", "metrics", "product_facts"]
assert schema["properties"]["product_facts"]["items"]["properties"]["kind"]["enum"] == [
    "description", "purpose", "domain", "data_type", "storage_location",
    "source_system", "source_name", "tag", "access", "security_classification",
    "owner", "contact", "consumer", "refresh_schedule", "freshness", "sla",
    "ai_readiness", "quality", "constraint", "related_link",
]
```

Use `jsonschema.validate` to prove an unknown kind is rejected. Construct `ProductFactSuggestion` with surrounding/repeated whitespace and assert it normalizes to a single-spaced value; assert a whitespace-only value raises `ValidationError`.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/unit/test_llm.py -q`

Expected: FAIL because `product_facts` and the typed fact models do not exist.

- [x] **Step 3: Implement the closed schema and typed models**

Define a shared literal alias with the exact 20 allowed kinds. Add:

```python
class ProductFactSuggestion(StrictModel):
    kind: ProductFactKind
    value: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: object) -> str:
        normalized = " ".join(str(value).split())
        if not normalized:
            raise ValueError("product fact value must be non-empty")
        return normalized
```

Add a fully closed `product_fact` JSON Schema, require `product_facts` at the response root, add `ProductFactIR`, replace `product_document_markdown` with `product_facts`, and leave all existing semantic fields intact.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/unit/test_llm.py -q`

Expected: all LLM unit tests pass.

### Task 3: Validate, deduplicate, and order evidence-backed product facts

**Files:**
- Modify: `src/ard_ossie/pipeline.py`
- Test: `tests/unit/test_pipeline.py`

**Interfaces:**
- Extends: `SuggestionBatch.product_facts: list[ProductFactSuggestion]`
- Produces: `_validate_product_facts(facts, product_document, configured_description) -> list[ProductFactIR]`
- Consumes: confidence threshold `0.7`, canonical kind order, singleton/repeatable kind sets

- [x] **Step 1: Write failing fact-validation tests**

Create literal `ParsedDocument` and `ProductFactSuggestion` fixtures. Add separate tests proving:

1. confidence `0.69` is omitted;
2. duplicate repeated `tag` values differing only by case are deduplicated and repeatable values are case-insensitively sorted;
3. two different `purpose` values raise `LLM_PRODUCT_FACT_SINGLETON_CONFLICT`;
4. semantic-document role raises `LLM_PRODUCT_FACT_EVIDENCE_ROLE_INVALID`;
5. wrong hash raises `LLM_PRODUCT_FACT_EVIDENCE_SOURCE_UNKNOWN`;
6. blank excerpt raises `LLM_PRODUCT_FACT_EVIDENCE_EXCERPT_REQUIRED`;
7. a non-empty configured description becomes the authoritative first `description` fact with no LLM evidence and replaces an extracted description fact.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/unit/test_pipeline.py -q`

Expected: FAIL because `_validate_product_facts` and `SuggestionBatch.product_facts` do not exist.

- [x] **Step 3: Implement deterministic validation and wire extraction**

Add exact singleton and repeatable sets, validate every accepted LLM fact without including its value in error text, deduplicate by `(kind, value.casefold())`, reject distinct values for singleton kinds, and sort by canonical kind position then `value.casefold()`.

Extend the system instruction to require explicit product-HTML facts only and explicitly ignore navigation, menus, buttons, attachment UI, notices, authoring hints, empty fields, AI-generated summaries, footer, and chatbot content. Validate facts inside `_extract_suggestions`, preserve them in `llm-suggestions.json`, and return accepted `ProductFactIR` objects for product construction. Capture the original configured description before applying semantic suggestions so only the explicit configuration value seeds document facts.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/unit/test_pipeline.py tests/unit/test_llm.py -q`

Expected: all pipeline and LLM unit tests pass.

### Task 4: Render canonical non-empty sections and remove raw parsed Markdown

**Files:**
- Modify: `src/ard_ossie/renderers.py`
- Modify: `templates/data-product.md.j2`
- Modify: `tests/conftest.py`
- Modify: `tests/golden/sales-order/data-product.md`
- Modify: `tests/unit/test_renderers.py`

**Interfaces:**
- Produces: deterministic renderer `sections`, each item containing a heading and ordered labeled values
- Preserves: sorted mandatory dataset table
- Removes: `## Parsed source` and every use of raw HTML Markdown

- [x] **Step 1: Write failing complete and sparse renderer tests**

Extend the golden fixture with facts spanning all canonical sections and update the hand-authored golden Markdown. Add a sparse `ProductIR` test with no facts and assert:

```python
assert "## Parsed source" not in rendered
assert "미제공" not in rendered
assert "N/A" not in rendered
assert "## Overview" not in rendered
assert "## Datasets" in rendered
```

Reverse the fact list and assert rendered output is unchanged.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/unit/test_renderers.py -q`

Expected: FAIL because the current template emits `Parsed source` and has no canonical fact sections.

- [x] **Step 3: Build the deterministic section context and template**

In `renderers.py`, define the fixed section and fact-label mapping. Group and sort facts independent of input order, omit empty groups, and pass `sections` to Jinja. Render each accepted fact as `- **<Label>:** <value>`, followed by the mandatory datasets table. Remove the raw source block and unconditional description heading from the template.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/unit/test_renderers.py -q`

Expected: complete golden, sparse omission, and ordering tests all pass.

### Task 5: Prove noisy portal text is excluded and Ossie is emitted unchanged

**Files:**
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `tests/integration/test_cli_process.py`

**Interfaces:**
- Consumes: provider response with `suggestions`, `metrics`, and `product_facts`
- Produces: normalized `generated/data-product.md`, schema-valid `generated/ossie-model.json`, and audited `quality/llm-suggestions.json` in one atomic process

- [x] **Step 1: Write the failing noisy-portal integration test**

Replace the product fixture HTML with user-entered facts plus sentinels for menu text, authoring help, attachment controls, privacy notice, empty field, `(AI 자동생성)` summary, footer, and chatbot. Use a fake provider that returns only grounded product facts from the product document evidence. Assert the Markdown contains canonical user facts and excludes every sentinel and `## Parsed source`. Assert `ossie-model.json` exists and validates through `compile_ossie`/the shipped Ossie schema, and assert the quality audit contains `product_facts`.

- [x] **Step 2: Run the focused test and verify RED**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/integration/test_cli_process.py -q`

Expected: FAIL because the pipeline still copies raw parsed Markdown and does not attach accepted facts to `ProductIR`.

- [x] **Step 3: Complete pipeline construction without raw Markdown fallback**

Construct `ProductIR(product_facts=validated_facts)` and delete `product_document_markdown=...`. Keep `description`, `instructions`, tables, relationships, metrics, `compile_ossie(product_ir)`, and the five generated artifact names unchanged.

- [x] **Step 4: Run focused integration tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest tests/integration/test_cli_process.py tests/integration/test_wheel_assets.py -q`

Expected: normalized Markdown, same-run Ossie output, and packaged template tests pass.

### Task 6: Run repository gates and commit the implementation

**Files:**
- Modify: `docs/superpowers/plans/2026-08-12-data-product-document-normalization.md` (mark completed steps)

**Interfaces:**
- Produces: one clean implementation commit on `design/data-product-document-normalization`

- [x] **Step 1: Run the full test suite**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run pytest -q`

Expected: all tests pass with zero failures.

- [x] **Step 2: Run static and workflow checks**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv run ruff check .`

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run python - <<'PY'
from pathlib import Path
import yaml

paths = sorted(Path('.github/workflows').glob('*.yml'))
for path in paths:
    yaml.safe_load(path.read_text(encoding='utf-8'))
print(f'{len(paths)} workflow files parsed')
PY
```

Expected: Ruff exits 0 and all seven workflow files parse.

- [x] **Step 3: Build both distribution artifacts**

Run: `UV_CACHE_DIR=/tmp/ard-uv-cache uv build`

Expected: sdist and wheel build successfully.

- [x] **Step 4: Inspect the exact diff and commit**

Run: `git diff --check && git status --short && git diff --stat && git diff`

Stage only the planned files and commit:

```bash
git add docs/superpowers/plans/2026-08-12-data-product-document-normalization.md \
  src/ard_ossie/docling_parser.py src/ard_ossie/ir.py src/ard_ossie/llm.py \
  src/ard_ossie/pipeline.py src/ard_ossie/renderers.py templates/data-product.md.j2 \
  tests/conftest.py tests/golden/sales-order/data-product.md \
  tests/integration/test_cli_process.py tests/integration/test_docling_pipeline.py \
  tests/unit/test_llm.py tests/unit/test_pipeline.py tests/unit/test_renderers.py
git commit -m "feat: normalize data product documentation"
```

- [x] **Step 5: Re-run verification at the exact commit**

Repeat pytest, Ruff, workflow parsing, and `uv build`, then assert `git status --short` is empty.
