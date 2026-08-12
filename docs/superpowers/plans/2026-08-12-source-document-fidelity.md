# Source Document Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `data-semantic.md` a faithful projection of the semantic source and `data-dictionary.json` a faithful projection of the Excel dictionary, while keeping generated metrics and relationships in Ossie and audit outputs.

**Architecture:** Add an embedded-text-first path for semantic PDFs, falling back as a whole to the existing Docling converter whenever any page lacks usable text. Stop the semantic renderer from appending generated content, and stop LLM table/column descriptions from mutating canonical dictionary IR; retain existing product-level suggestion behavior and all audit records.

**Tech Stack:** Python 3.12, Docling 2.114.0, pypdfium2 5.12.1, Jinja2, Pydantic, pytest, Ruff, uv

## Global Constraints

- Work from `fix/source-document-fidelity`, whose first parent is the current `main` processor code.
- Follow RED-GREEN-REFACTOR for every behavior change; observe each focused test fail for the intended reason before implementation.
- Do not add Korean word-repair heuristics or rewrite text inside a page.
- Use embedded PDF text only when every page is non-empty after page-edge trimming; otherwise use one whole-document Docling fallback.
- Preserve existing system-owned product/table/column IDs, numeric versions, source paths, hashes, atomic promotion, and validation behavior.
- Keep LLM suggestions and generated metrics auditable, but never publish them into `data-semantic.md` or `data-dictionary.json` unless they were already present in the corresponding source.
- Do not edit generated PR #5 artifacts manually. Merge this processor fix first, then regenerate PR #5 through the trusted Issue #3 workflow.
- Use `UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache` for every `uv` command.

---

### Task 1: Parse semantic PDFs from their embedded text layer

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/ard_ossie/docling_parser.py`
- Test: `tests/integration/test_docling_pipeline.py`

**Interfaces:**
- Add: `_parse_embedded_pdf(source: SourceFile, *, pdfium: Any | None = None) -> ParsedDocument | None`
- Extend: `DoclingParser.__init__` with an optional embedded-PDF parser seam
- Preserve: `DoclingParser.parse(source) -> ParsedDocument`

- [ ] **Step 1: Add RED tests for exact embedded text and evidence**

Add small fake PDFium document, page, and text-page objects to `tests/integration/test_docling_pipeline.py`. They must expose `PdfDocument`, `__len__`, `__getitem__`, `get_textpage`, `get_text_range`, and `close`, so production extraction logic is tested without a model download or OCR.

Add a test with two pages whose raw text includes mixed line endings and Korean words:

```python
def test_embedded_pdf_preserves_internal_text_and_records_page_evidence(
    tmp_path: Path,
) -> None:
    source = semantic_pdf_source(tmp_path)
    parsed = _parse_embedded_pdf(
        source,
        pdfium=FakePdfium(["  개인정보\r\n유효성  ", "둘째 페이지\r끝  "]),
    )

    assert parsed is not None
    assert parsed.markdown == "개인정보\n유효성\n\n둘째 페이지\n끝"
    assert [item.locator for item in parsed.evidence] == [
        {"document": "semantic/semantic.pdf", "page": 1},
        {"document": "semantic/semantic.pdf", "page": 2},
    ]
    assert [item.excerpt for item in parsed.evidence] == [
        "개인정보\n유효성",
        "둘째 페이지\n끝",
    ]
```

Also assert `개 인정보` and `유 효 성` are absent. Run it before adding the helper:

```bash
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen pytest -q \
  tests/integration/test_docling_pipeline.py::test_embedded_pdf_preserves_internal_text_and_records_page_evidence
```

Expected: FAIL because `_parse_embedded_pdf` does not exist.

- [ ] **Step 2: Add RED tests for selection and whole-document fallback**

Add one helper test that passes `FakePdfium(["page one", "   "])` and expects `None`. Add one adapter test that injects an embedded parser returning a `ParsedDocument` and an `ExplodingConverter`, then proves the converter is not called. Retain the existing malformed fake-PDF test; its embedded parser must return `None`, allowing `FakeConverter` to prove the Docling fallback remains intact.

```python
def test_embedded_pdf_rejects_document_when_any_page_has_no_text(tmp_path: Path) -> None:
    assert _parse_embedded_pdf(
        semantic_pdf_source(tmp_path),
        pdfium=FakePdfium(["page one", "   "]),
    ) is None


def test_docling_adapter_prefers_complete_embedded_pdf_text(tmp_path: Path) -> None:
    expected = ParsedDocument(
        role=SourceRole.SEMANTIC_DOCUMENT,
        source_hash="a" * 64,
        markdown="개인정보와 유효성",
        evidence=[],
    )
    parsed = DoclingParser(
        converter=ExplodingConverter(),
        embedded_pdf_parser=lambda _source: expected,
    ).parse(semantic_pdf_source(tmp_path))

    assert parsed == expected
```

Run the three new tests. Expected: FAIL on the missing constructor seam and helper behavior.

- [ ] **Step 3: Make pypdfium2 a direct locked dependency**

Add the exact already-resolved runtime version beside Docling:

```toml
dependencies = [
  "docling==2.114.0",
  "pypdfium2==5.12.1",
  # existing dependencies remain unchanged
]
```

Regenerate and inspect the lock:

```bash
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv lock
git diff -- pyproject.toml uv.lock
```

Expected: the project package gains a direct `pypdfium2==5.12.1` requirement; no unrelated package version changes.

- [ ] **Step 4: Implement the minimum embedded-text-first parser**

In `src/ard_ossie/docling_parser.py`, import `Callable` from `collections.abc`, add a callable type
alias, and inject the default helper:

```python
EmbeddedPdfParser = Callable[[SourceFile], ParsedDocument | None]


class DoclingParser:
    def __init__(
        self,
        *,
        converter: Any | None = None,
        embedded_pdf_parser: EmbeddedPdfParser | None = None,
    ) -> None:
        self._converter = converter
        self._embedded_pdf_parser = embedded_pdf_parser or _parse_embedded_pdf

    def parse(self, source: SourceFile) -> ParsedDocument:
        if source.role is SourceRole.DICTIONARY_EXCEL:
            raise ValueError("dictionary Excel is handled by the cell-preserving adapter")
        if source.role is SourceRole.SEMANTIC_DOCUMENT and source.path.suffix.lower() == ".pdf":
            embedded = self._embedded_pdf_parser(source)
            if embedded is not None:
                return embedded
        # existing Docling conversion path remains unchanged
```

Implement `_parse_embedded_pdf` with a lazy `pypdfium2` import. Open pages in index order; normalize `\r\n` and bare `\r` to `\n`; call `.strip()` only once around each page; reject a zero-page document or any empty normalized page; join accepted pages with `"\n\n"`; create one `Evidence` item per page using the existing source hash, role, relative path, one-based page number, and a 500-character excerpt; close text pages, pages, and the PDF document in `finally` blocks. Catch `pypdfium2.PdfiumError` and return `None` so malformed/unreadable PDFs follow the current Docling failure path.

```python
def _parse_embedded_pdf(
    source: SourceFile,
    *,
    pdfium: Any | None = None,
) -> ParsedDocument | None:
    if pdfium is None:
        import pypdfium2

        pdfium = pypdfium2

    try:
        document = pdfium.PdfDocument(source.path)
    except pdfium.PdfiumError:
        return None

    pages: list[str] = []
    evidence: list[Evidence] = []
    try:
        if len(document) == 0:
            return None
        for page_index in range(len(document)):
            page = document[page_index]
            try:
                text_page = page.get_textpage()
                try:
                    text = text_page.get_text_range()
                finally:
                    text_page.close()
            finally:
                page.close()
            normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not normalized:
                return None
            pages.append(normalized)
            evidence.append(
                Evidence(
                    source_hash=source.sha256,
                    role=source.role,
                    locator={
                        "document": source.relative_path,
                        "page": page_index + 1,
                    },
                    excerpt=normalized[:500],
                )
            )
    except pdfium.PdfiumError:
        return None
    finally:
        document.close()

    return ParsedDocument(
        role=source.role,
        source_hash=source.sha256,
        markdown="\n\n".join(pages),
        evidence=evidence,
    )
```

Do not add a final newline in `ParsedDocument.markdown`; the artifact renderer owns the single trailing newline.

- [ ] **Step 5: Run parser tests and the complete parser integration file**

```bash
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen pytest -q \
  tests/integration/test_docling_pipeline.py
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen ruff check \
  src/ard_ossie/docling_parser.py tests/integration/test_docling_pipeline.py
```

Expected: PASS, including the existing HTML, DOCX, provenance, malformed-PDF fallback, and new embedded-text tests.

- [ ] **Step 6: Commit the parser change**

```bash
git add pyproject.toml uv.lock src/ard_ossie/docling_parser.py \
  tests/integration/test_docling_pipeline.py
git commit -m "fix: preserve embedded PDF text"
```

---

### Task 2: Render only semantic-source content

**Files:**
- Modify: `src/ard_ossie/renderers.py`
- Modify: `templates/data-semantic.md.j2`
- Modify: `tests/conftest.py`
- Modify: `tests/golden/sales-order/data-semantic.md`
- Modify: `tests/unit/test_renderers.py`
- Modify: `tests/integration/test_cli_process.py`

**Interfaces:**
- Preserve: `render_semantic_markdown(product: ProductIR) -> str`
- Change authority: only `ProductIR.instructions` may contribute content to `data-semantic.md`

- [ ] **Step 1: Add the source semantic text to the renderer fixture and golden**

Set the fixture's `ProductIR.instructions` to the complete source text:

```python
instructions="# Sales Order semantics\n\nNet revenue excludes tax.",
```

Replace `tests/golden/sales-order/data-semantic.md` with exactly:

```markdown
# Sales Order semantics

Net revenue excludes tax.
```

The file must end with one newline.

- [ ] **Step 2: Add a RED source-only renderer test**

```python
def test_semantic_renderer_does_not_append_generated_semantics(
    resolved_sales_order_ir: ProductIR,
) -> None:
    rendered = render_semantic_markdown(resolved_sales_order_ir)

    assert rendered == "# Sales Order semantics\n\nNet revenue excludes tax.\n"
    assert "net_revenue" not in rendered
    assert "orders_customer" not in rendered
    assert "## Metrics" not in rendered
    assert "## Relationships" not in rendered
```

Update `test_pipeline_qualifies_single_dataset_metric_and_excludes_cross_dataset_metric` so it proves accepted and rejected metrics are both absent from the semantic artifact while the original DOCX text remains:

```python
semantic_markdown = (product / "generated" / "data-semantic.md").read_text()
assert "confirmed customer purchase" in semantic_markdown
assert "Campaign Count" not in semantic_markdown
assert "Modeled Efficiency" not in semantic_markdown
```

Run the focused tests:

```bash
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen pytest -q \
  tests/unit/test_renderers.py::test_renderers_match_hand_authored_golden_files \
  tests/unit/test_renderers.py::test_semantic_renderer_does_not_append_generated_semantics \
  tests/integration/test_cli_process.py::test_pipeline_qualifies_single_dataset_metric_and_excludes_cross_dataset_metric
```

Expected: FAIL because the current template adds a generated title, Metrics, and Relationships.

- [ ] **Step 3: Reduce the renderer and template to the source text**

Keep the template as a wheel asset, but replace its contents with:

```jinja2
{{ product.instructions or "" }}
```

Simplify the renderer without metrics, relationships, or table-name inputs:

```python
def render_semantic_markdown(product: ProductIR) -> str:
    template = _ENVIRONMENT.get_template("data-semantic.md.j2")
    return template.render(product=product).strip() + "\n"
```

Do not change `ProductIR`, the Ossie compiler, Registry metric/relationship records, or the suggestion audit.

- [ ] **Step 4: Run renderer, pipeline, and package-asset tests**

```bash
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen pytest -q \
  tests/unit/test_renderers.py \
  tests/integration/test_cli_process.py::test_process_emits_required_artifacts_and_reuses_column_ids \
  tests/integration/test_cli_process.py::test_pipeline_qualifies_single_dataset_metric_and_excludes_cross_dataset_metric \
  tests/integration/test_installed_package_assets.py
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen ruff check \
  src/ard_ossie/renderers.py tests/conftest.py tests/unit/test_renderers.py \
  tests/integration/test_cli_process.py
```

Expected: PASS. The metric safety test must still prove `Campaign Count` is present in Ossie and the Registry, both metrics remain in the raw audit, and only the cross-dataset metric is excluded from publication.

- [ ] **Step 5: Commit the semantic renderer change**

```bash
git add src/ard_ossie/renderers.py templates/data-semantic.md.j2 tests/conftest.py \
  tests/golden/sales-order/data-semantic.md tests/unit/test_renderers.py \
  tests/integration/test_cli_process.py
git commit -m "fix: keep semantic artifact source-only"
```

---

### Task 3: Keep the dictionary authoritative to Excel

**Files:**
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `tests/integration/test_cli_process.py`

**Interfaces:**
- Narrow: `_apply_suggestions(...)` may apply product description/synonyms only
- Preserve: all validated `AISuggestion` objects in `quality/llm-suggestions.json`

- [ ] **Step 1: Extend the fake provider with both table and column descriptions**

Read the allowed paths from the system prompt so the test uses the runtime-generated stable column
ID rather than the physical column name, then add a second evidence-backed suggestion to
`FakeSemanticProvider`:

```python
allowed_paths = json.loads(
    messages[0]["content"].split("Allowed field_path values: ", maxsplit=1)[1]
)
column_description_path = next(
    path
    for path in allowed_paths
    if path.startswith(f"tables.{TABLE_ID}.columns.")
    and path.endswith(".description")
)

{
    "field_path": column_description_path,
    "value": "LLM-only order identifier",
    "confidence": 0.9,
    "evidence": evidence,
    "status": "ai_suggested",
},
```

Rename the existing integration test to communicate the publication boundary, for example `test_pipeline_audits_table_and_column_suggestions_without_publishing_them`.

- [ ] **Step 2: Make the integration test RED on canonical and rendered outputs**

After processing, retain the product-synonym assertion, then assert the source workbook's blank table and column descriptions stay blank in both Ossie and the dictionary while both provider values remain in the raw audit:

```python
model = ossie["semantic_model"][0]
assert model["ai_context"]["synonyms"] == ["purchase orders"]
assert "description" not in model["datasets"][0]

dictionary = json.loads(
    (product / "generated" / "data-dictionary.json").read_text(encoding="utf-8")
)
assert dictionary["tables"][0]["description"] is None
assert dictionary["tables"][0]["columns"][0]["description"] is None

audit = json.loads(
    (product / "quality" / "llm-suggestions.json").read_text(encoding="utf-8")
)
assert {item["value"] for item in audit["suggestions"]} >= {
    "Confirmed orders",
    "LLM-only order identifier",
}
```

Run the renamed test:

```bash
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen pytest -q \
  tests/integration/test_cli_process.py::test_pipeline_audits_table_and_column_suggestions_without_publishing_them
```

Expected: FAIL because `_apply_suggestions` currently mutates the table and column descriptions.

- [ ] **Step 3: Remove only table/column mutation from `_apply_suggestions`**

Delete the `by_table` map and all branches that parse `tables.<id>.description` or `tables.<id>.columns.<id>.description`. Keep deep copies and the existing product behavior:

```python
def _apply_suggestions(
    config: ProductConfig,
    drafts: list[_TableDraft],
    suggestions: list[AISuggestion],
) -> tuple[ProductConfig, list[_TableDraft]]:
    updated_config = config.model_copy(deep=True)
    updated_drafts = [draft.model_copy(deep=True) for draft in drafts]
    for suggestion in suggestions:
        if suggestion.confidence < 0.7:
            continue
        if suggestion.field_path == "product.description" and not updated_config.description:
            updated_config.description = str(suggestion.value)
        elif suggestion.field_path == "product.synonyms" and isinstance(suggestion.value, list):
            updated_config.synonyms = [str(item) for item in suggestion.value]
    return updated_config, updated_drafts
```

Do not remove table/column paths from the provider schema or validation allowlist: those responses remain valid audit information.

- [ ] **Step 4: Run suggestion, dictionary, Registry, and pipeline regressions**

```bash
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen pytest -q \
  tests/integration/test_cli_process.py::test_pipeline_audits_table_and_column_suggestions_without_publishing_them \
  tests/unit/test_renderers.py \
  tests/unit/test_pipeline.py \
  tests/integration/test_cli_process.py
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen ruff check \
  src/ard_ossie/pipeline.py tests/integration/test_cli_process.py
```

Expected: PASS. The dictionary still includes stable IDs, numeric versions, source paths, and source-derived values; product synonyms still reach Ossie; table/column suggestions remain in the audit only.

- [ ] **Step 5: Commit the dictionary authority change**

```bash
git add src/ard_ossie/pipeline.py tests/integration/test_cli_process.py
git commit -m "fix: keep dictionary source-authoritative"
```

---

### Task 4: Verify the actual PR #5 PDF and the complete processor

**Files:**
- Verify: all changed source, tests, templates, lockfile, schemas, workflows, and wheel assets
- Verify input: PR #5 `products/500138301/sources/semantic/semantic.pdf`

**Interfaces:**
- Consumes: exact feature HEAD plus the Git LFS materialized PR #5 PDF
- Produces: acceptance evidence and a clean exact tree ready for review

- [ ] **Step 1: Materialize and parse the exact PR #5 PDF**

Use a temporary clone so the data PR stays untouched:

```bash
acceptance_dir="$(mktemp -d)"
git clone https://github.com/kimohy/ard-ossie-provider.git "$acceptance_dir/repo"
git -C "$acceptance_dir/repo" fetch origin pull/5/head:pr-5
git -C "$acceptance_dir/repo" switch pr-5
git -C "$acceptance_dir/repo" lfs pull
```

Parse the exact file with the feature implementation and assert the reported words and five-page evidence:

```bash
PR5_REPOSITORY="$acceptance_dir/repo" \
PYTHONPATH=src \
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache \
uv run --frozen python - <<'PY'
import hashlib
import os
from pathlib import Path

from ard_ossie.docling_parser import DoclingParser
from ard_ossie.ingestion import SourceFile, SourceRole

path = Path(os.environ["PR5_REPOSITORY"]) / "products/500138301/sources/semantic/semantic.pdf"
payload = path.read_bytes()
parsed = DoclingParser().parse(
    SourceFile(
        role=SourceRole.SEMANTIC_DOCUMENT,
        path=path,
        relative_path="semantic/semantic.pdf",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
)
assert len(parsed.evidence) == 5
assert parsed.markdown.count("개인정보") == 2
assert parsed.markdown.count("유효성") == 1
assert "개 인정보" not in parsed.markdown
assert "유 효 성" not in parsed.markdown
print("PR #5 PDF acceptance: 5 pages, Korean spacing preserved")
PY
```

Expected: the printed acceptance message and no assertion failure.

- [ ] **Step 2: Run the complete local gate**

```bash
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv sync --locked --all-groups
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen pytest -q
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen ruff check .
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv build
git diff --check origin/main...HEAD
git status --short
```

Expected: all tests pass, Ruff is clean, sdist/wheel build succeeds, diff check is empty, and the working tree is clean apart from expected build artifacts ignored by Git.

- [ ] **Step 3: Run the exact isolated repository verifier groups**

Create an isolated candidate checkout for the exact feature HEAD, then invoke the trusted code from the current worktree. The repository checker requires 40-character base/head SHAs and refuses an aggregate same-checkout run:

```bash
candidate_dir="$(mktemp -d)/candidate"
git clone --no-local . "$candidate_dir"
git -C "$candidate_dir" switch --detach HEAD
base_sha="$(git rev-parse origin/main)"
head_sha="$(git rev-parse HEAD)"
for group in static model-schemas pytest wheel; do
  UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache uv run --frozen ard workflow repository-check \
    --base-ref "$base_sha" \
    --head-ref "$head_sha" \
    --head-sha "$head_sha" \
    --repository "$candidate_dir" \
    --verification-group "$group"
done
```

Expected: all four groups publish successful local result JSON. Inspect `.ard/run/workflow.repository-check-result.json` after each invocation if a group fails; do not bypass a verifier.

- [ ] **Step 4: Review the exact diff**

Use the `superpowers:requesting-code-review` skill. Review `origin/main...HEAD` for Critical, Important, and Minor findings, with special attention to resource closing, malformed-PDF fallback, source-authority boundaries, audit retention, wheel dependencies, and output determinism. Apply every valid finding through a fresh RED-GREEN cycle and rerun Steps 1-3.

- [ ] **Step 5: Record final verification evidence**

```bash
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git status --short --branch
```

Expected: only the approved design/plan and three focused implementation commits, with a clean feature branch.

---

### Task 5: Publish the processor fix and regenerate PR #5

**Files:**
- Publish: the verified processor branch as a separate Draft PR
- Regenerate through: `.github/workflows/ard-issue-intake.yml`
- Verify outputs in: PR #5 product, Registry, and quality artifacts

**Interfaces:**
- Consumes: clean exact feature HEAD and Issue #3's `ard:approved` event route
- Produces: merged processor fix and a regenerated PR #5 head satisfying the user's source-fidelity criteria

- [ ] **Step 1: Rebase safely if `main` moved**

Fetch `origin/main`. If it differs from the plan base, rebase the local feature branch without force-pushing any published branch, then rerun all of Task 4. Do not merge stale verification evidence.

- [ ] **Step 2: Open a separate Draft processor PR**

Push `fix/source-document-fidelity` without rewriting remote history and open a Draft PR against `main`. The PR body must state:

- embedded semantic PDF text is preferred only when every page has text;
- scanned PDFs retain the Docling fallback;
- semantic and dictionary artifacts are source-authoritative;
- metrics/relationships and LLM suggestions remain in Ossie/audit outputs;
- the actual PR #5 Korean-spacing acceptance result;
- the exact local gate results.

Do not add processor code or manual output edits to PR #5.

- [ ] **Step 3: Require exact-head CI and fresh review, then merge the processor PR**

Wait for every required check on the unchanged PR head, resolve all actionable review threads through tested commits, request a fresh exact-head review, mark ready, and squash-merge with expected-head protection. Verify the merged PR state and resulting `main` SHA.

- [ ] **Step 4: Replay Issue #3 through the official trusted label path**

Remove stale `ard:failed` and toggle only `ard:approved` to emit a new `issues:labeled` event. Monitor authorize, intake, credential-free validation, protected processor execution, writeback, base synchronization, and finalize. If `ard-llm` requests protected-environment approval, stop and give the user the exact run link; do not bypass the environment.

- [ ] **Step 5: Verify the regenerated PR #5 exact head**

On the new PR #5 head, assert:

```text
data-semantic.md contains 개인정보 exactly twice
data-semantic.md contains 유효성 exactly once
data-semantic.md contains neither 개 인정보 nor 유 효 성
data-semantic.md contains no generated Metrics or Relationships appendix
data-dictionary.json keeps system IDs/versions/source paths
every non-system dictionary table/column value matches the Excel adapter output
ossie-model.json still contains accepted metrics and deterministic relationships
quality/llm-suggestions.json still contains raw provider suggestions
quality report hard errors = 0
ard/changeset = success
ard/quality-gate = success
unresolved actionable review threads = 0
```

Fetch the new PR head into the acceptance clone, materialize LFS, and run the source-value and
schema comparison rather than relying on visual sampling:

```bash
git -C "$acceptance_dir/repo" fetch origin pull/5/head
git -C "$acceptance_dir/repo" switch --detach FETCH_HEAD
git -C "$acceptance_dir/repo" lfs pull
PR5_REPOSITORY="$acceptance_dir/repo" \
PYTHONPATH=src \
UV_CACHE_DIR=/tmp/ard-source-fidelity-uv-cache \
uv run --frozen python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

from jsonschema import validate

from ard_ossie.excel_adapter import parse_dictionary
from ard_ossie.ossie_compiler import load_ossie_011_schema

root = Path(os.environ["PR5_REPOSITORY"]) / "products/500138301"
semantic = (root / "generated/data-semantic.md").read_text(encoding="utf-8")
assert semantic.count("개인정보") == 2
assert semantic.count("유효성") == 1
assert "개 인정보" not in semantic
assert "유 효 성" not in semantic
assert "## Metrics" not in semantic
assert "## Relationships" not in semantic

workbook = root / "sources/dictionary/dictionary.xlsx"
source_bytes = workbook.read_bytes()
parsed = parse_dictionary(
    workbook,
    source_hash=hashlib.sha256(source_bytes).hexdigest(),
)
source_tables = {table.locator.rsplit("|", maxsplit=1)[1]: table for table in parsed.tables}
rendered = json.loads(
    (root / "generated/data-dictionary.json").read_text(encoding="utf-8")
)
assert rendered["product_id"] == "prd_019ff10c-8be8-79d0-af07-21450abedf9e"
assert rendered["product_version"] == 1
assert set(source_tables) == {table["dataset_name"] for table in rendered["tables"]}

column_fields = (
    "ordinal",
    "name",
    "logical_name",
    "data_type",
    "nullable",
    "primary_key",
    "foreign_key",
    "description",
    "formula",
    "comment",
)
for table in rendered["tables"]:
    source_table = source_tables[table["dataset_name"]]
    assert table["table_id"].startswith("tbl_")
    assert isinstance(table["table_version"], int)
    assert table["description"] == source_table.description
    assert len(table["columns"]) == len(source_table.columns)
    for output_column, source_column in zip(
        table["columns"],
        source_table.columns,
        strict=True,
    ):
        assert output_column["column_id"].startswith("col_")
        for field in column_fields:
            assert output_column.get(field) == getattr(source_column, field)

ossie = json.loads((root / "generated/ossie-model.json").read_text(encoding="utf-8"))
validate(instance=ossie, schema=load_ossie_011_schema())
model = ossie["semantic_model"][0]
metric_names = {metric["name"] for metric in model["metrics"]}
assert metric_names == {
    "Campaign Count",
    "Active Campaign Count",
    "Creative Count",
    "Impressions",
    "Engagements",
    "Engagement Rate",
    "Spend Units",
    "Cost per Engagement",
    "Interest Signals",
    "Action Signals",
    "Modeled Value Units",
    "Modeled Efficiency",
}
assert isinstance(model.get("relationships", []), list)
assert all(name not in semantic for name in metric_names)

audit = json.loads(
    (root / "quality/llm-suggestions.json").read_text(encoding="utf-8")
)
assert {metric["name"] for metric in audit["metrics"]} == metric_names
quality = json.loads(
    (root / "quality/quality-report.json").read_text(encoding="utf-8")
)
assert quality["hard_errors"] == 0
print("PR #5 regenerated artifacts satisfy source-fidelity acceptance")
PY
```

Confirm PR #5's base includes the merged processor commit and its head SHA did not change during
this verification. Check `ard/changeset`, `ard/quality-gate`, and review threads against that exact
head.

- [ ] **Step 6: Resolve superseded review feedback and merge PR #5**

Reply to the user's fidelity feedback with the exact regenerated evidence. Resolve the outdated integer-division thread only after confirming the previously merged fix remains in the regenerated Ossie expression. Request a fresh exact-head review, mark PR #5 ready, and squash-merge with expected-head protection. Finally verify PR #5 is merged, `main` contains its merge result, Issue #3 has no `ard:failed`, and both required statuses refer to the merged PR head.
