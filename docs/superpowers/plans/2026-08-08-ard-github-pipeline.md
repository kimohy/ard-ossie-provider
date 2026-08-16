# ARD GitHub Pipeline Implementation Plan

> **Superseded policy (2026-08-16):** Public visibility requirements in this historical plan are
> replaced by the private repository and Issue intake contract in
> `docs/superpowers/specs/2026-08-16-private-repository-issue-intake-auth-design.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Implement a public-repository MVP that ingests one ARD product from an approved GitHub Issue or an internal source branch, parses the documents with Docling, generates canonical/Ossie artifacts through an OpenAI-compatible provider, blocks duplicates and invalid numeric versions, and publishes immutable Git tags and GitHub Releases after merge.

**Architecture:** A Python 3.12 `ard` CLI owns all deterministic behavior; GitHub Actions only supplies event data, credentials and orchestration. Registry entities are stored one file per immutable UUIDv7 ID, product and table versions increase independently from `v1` through `v999`, and a reusable processing workflow writes generated artifacts back to the trusted PR branch. Shared-table changes are grouped by `changeset_id` and hold release dispatch until every impacted product is current.

**Tech Stack:** Python 3.12, uv, Pydantic 2, Typer 0.26.8, Docling 2.114.0, openai 2.46.0, openpyxl, jsonschema, pytest, GitHub Actions

## Global Constraints

- Target repository is public `kimohy/ard-ossie-provider` with `main` as the protected default branch.
- Target output is Apache Ossie 0.1.1; do not compile against upstream development schemas.
- Store only the latest source and generated product state; recover historical state from Git tags, Git history and GitHub Releases.
- Assign immutable UUIDv7 IDs with `prd_`, `tbl_`, `col_`, `met_`, `rel_` and `lnk_` prefixes.
- Store product and table versions as integers from `1` through `999`; a changed entity must advance by exactly one.
- One Issue and one product PR represent exactly one product version.
- Use only the default `GITHUB_TOKEN` for repository mutations; do not introduce PAT or GitHub App credentials in this milestone.
- Read the OpenAI-compatible API key only from `ARD_LLM_API_KEY`; read base URL and model from `ARD_LLM_BASE_URL` and `ARD_LLM_MODEL`.
- Never expose LLM secrets to an unapproved Issue, an external fork PR or untrusted `pull_request_target` code.
- Require a write, maintain or admin actor to add `ard:approved` before Issue processing.
- Treat physical IDs, table locators, column names/types and keys as deterministic source facts; LLM output may only suggest semantic fields with evidence.
- Pin GitHub Actions to these verified commits: checkout `de0fac2e4500dabe0009e67214ff5f5447ce83dd`, setup-python `a309ff8b426b58ec0e2a45f0f869d46889d02405`, github-script `ed597411d8f924073f98dfc5c65a23a2325f34cd`, upload-artifact `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`.
- Follow red-green-refactor for Python behavior: every production behavior starts with a test that fails for the intended reason.

---

## File Map

```text
pyproject.toml                         Python, CLI and test dependencies
src/ard_ossie/models.py               Validated registry and report models
src/ard_ossie/ids.py                  Python 3.12 UUIDv7 allocation
src/ard_ossie/canonical.py            Stable normalization and hashes
src/ard_ossie/identity.py             UUIDv7 allocation and duplicate decisions
src/ard_ossie/versioning.py           v1-v999 transition checks
src/ard_ossie/registry.py             Per-entity registry persistence and indexes
src/ard_ossie/impact.py               Shared-table impact and changesets
src/ard_ossie/ingestion.py            Source role and file validation
src/ard_ossie/docling_parser.py       HTML/DOCX/PDF Docling conversion
src/ard_ossie/excel_adapter.py        XLSX physical dictionary extraction
src/ard_ossie/llm.py                  OpenAI-compatible structured extraction
src/ard_ossie/ir.py                   Canonical product/semantic/dictionary IR
src/ard_ossie/renderers.py            Deterministic MD and dictionary rendering
src/ard_ossie/ossie_compiler.py       Ossie 0.1.1 JSON compilation and validation
src/ard_ossie/pipeline.py             Atomic parse, compile and quality pipeline
src/ard_ossie/github_event.py         Issue Form parsing, authorization and URLs
src/ard_ossie/cli.py                  `ard` command surface
schemas/candidate-change.schema.json   PR candidate contract
schemas/reports/*.schema.json          Duplicate, version, impact and quality output
.github/ISSUE_TEMPLATE/ard-content.yml Structured public Issue submission
.github/workflows/ard-issue-intake.yml Approved Issue orchestrator
.github/workflows/ard-direct-change.yml Internal branch orchestrator
.github/workflows/ard-process.yml      Reusable build/write-back workflow
.github/workflows/ard-changeset.yml    Shared-table coordination workflow
.github/workflows/ard-release.yml      Tags, Release and protected dispatch
tests/unit/*                            Pure behavior tests
tests/integration/*                     CLI and repository fixture tests
tests/fixtures/*                        Hand-authored ARD and Registry fixtures
```

---

### Task 1: Python foundation and entity contracts

**Files:**
- Create: `pyproject.toml`
- Create: `src/ard_ossie/__init__.py`
- Create: `src/ard_ossie/ids.py`
- Create: `src/ard_ossie/models.py`
- Create: `schemas/candidate-change.schema.json`
- Create: `tests/unit/test_models.py`
- Create: `tests/unit/test_ids.py`

**Interfaces:**
- Produces: `ProductRecord`, `TableRecord`, `TableLocator`, `ProductTableRef`, `CandidateChange`, `EntityStatus`, `Operation`
- Consumes: no application code

- [x] **Step 1: Write failing validation tests**

```python
def test_versions_accept_1_and_999_but_reject_0_and_1000():
    assert ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=1).version == 1
    assert ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=999).version == 999
    with pytest.raises(ValidationError):
        ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=0)
    with pytest.raises(ValidationError):
        ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=1000)

def test_table_locator_normalizes_without_credentials():
    locator = TableLocator(
        source_system_id=" ERP-PROD ", catalog="Analytics", schema_name="Sales", table_name="Orders"
    )
    assert locator.key == "erp-prod|analytics|sales|orders"
    assert "password" not in locator.model_dump()
```

- [x] **Step 2: Run the tests and verify RED**

Run: `python -m pytest tests/unit/test_models.py -v`

Expected: collection fails because `ard_ossie.models` does not exist.

Also test that `new_id("prd")` returns a UUID version 7 payload, preserves chronological byte ordering for increasing millisecond timestamps, and rejects unknown prefixes. Implement UUIDv7 directly from RFC 9562 fields because Python 3.12 does not provide `uuid.uuid7()`.

- [x] **Step 3: Implement the validated models**

```python
class ProductRecord(BaseModel):
    product_id: Annotated[str, StringConstraints(pattern=r"^prd_[0-9a-f-]{36}$")]
    product_key: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
    version: Annotated[int, Field(ge=1, le=999)]
    status: EntityStatus = EntityStatus.ACTIVE
    canonical_hash: str | None = None

class TableRecord(BaseModel):
    table_id: Annotated[str, StringConstraints(pattern=r"^tbl_[0-9a-f-]{36}$")]
    locator: TableLocator
    version: Annotated[int, Field(ge=1, le=999)]
    status: EntityStatus = EntityStatus.ACTIVE
    schema_hash: str | None = None
    canonical_hash: str | None = None
```

`CandidateChange` must contain `operation`, one product, zero or more tables, mappings, base versions, proposed versions, source hashes and optional `changeset_id`. Generate `schemas/candidate-change.schema.json` from this model and commit the checked-in schema.

- [x] **Step 4: Run the focused and full tests**

Run: `python -m pytest tests/unit/test_models.py -v && python -m pytest -q`

Expected: all tests pass with no warnings.

- [x] **Step 5: Commit**

```bash
git add pyproject.toml src/ard_ossie schemas/candidate-change.schema.json tests/unit/test_models.py tests/unit/test_ids.py
git commit -m "feat: add ARD entity contracts"
```

### Task 2: Canonical hashes and duplicate classification

**Files:**
- Create: `src/ard_ossie/canonical.py`
- Create: `src/ard_ossie/identity.py`
- Create: `schemas/reports/duplicate-report.schema.json`
- Create: `tests/unit/test_canonical.py`
- Create: `tests/unit/test_identity.py`

**Interfaces:**
- Consumes: entity models from Task 1
- Produces: `canonical_hash(value) -> str`, `schema_hash(table) -> str`, `classify_product(...) -> DuplicateReport`, `classify_table(...) -> DuplicateReport`

- [x] **Step 1: Write failing canonicalization tests**

```python
def test_volatile_metadata_does_not_change_canonical_hash():
    left = {"name": "Orders", "generated_at": "2026-08-08T01:00:00Z", "fields": ["id"]}
    right = {"fields": ["id"], "name": "Orders", "generated_at": "2026-08-09T01:00:00Z"}
    assert canonical_hash(left) == canonical_hash(right)

def test_changed_semantic_description_changes_hash():
    assert canonical_hash({"description": "ordered"}) != canonical_hash({"description": "shipped"})
```

- [x] **Step 2: Verify RED, then implement deterministic JSON normalization**

Run: `python -m pytest tests/unit/test_canonical.py -v`

Implement sorted UTF-8 JSON with NFC strings and an explicit exclusion set containing `generated_at`, `commit_sha`, `actions_run_id`, `llm_response_id` and `provenance_collected_at`. Hash with SHA-256.

- [x] **Step 3: Write failing duplicate decision tests**

```python
def test_existing_product_key_blocks_create():
    report = classify_product(new_product(key="sales-order"), registry_with_product(key="sales-order"))
    assert report.decision == "BLOCK"
    assert report.code == "PRODUCT_KEY_CONFLICT"

def test_same_table_locator_reuses_existing_table_id():
    report = classify_table(candidate_table(locator="erp|db|sales|orders"), existing_table())
    assert report.decision == "REUSE"
    assert report.target_id == TABLE_ID

def test_same_schema_at_different_locator_is_only_a_clone_warning():
    report = classify_table(candidate_table(locator="dwh|db|sales|orders_copy"), existing_table())
    assert report.decision == "WARN"
    assert report.code == "POSSIBLE_CLONE"
```

- [x] **Step 4: Verify RED, implement ordered duplicate rules, and verify GREEN**

The order is explicit ID, immutable key/locator, alias, canonical hash, then semantic candidate. Semantic similarity never returns `REUSE`; it returns `WARN` or `BLOCK` with candidate IDs.

Run: `python -m pytest tests/unit/test_canonical.py tests/unit/test_identity.py -v`

Expected: all cases pass and the generated duplicate report validates against its JSON Schema.

- [x] **Step 5: Commit**

```bash
git add src/ard_ossie/canonical.py src/ard_ossie/identity.py schemas/reports/duplicate-report.schema.json tests/unit/test_canonical.py tests/unit/test_identity.py
git commit -m "feat: detect product and table duplicates"
```

### Task 3: Independent numeric version checker

**Files:**
- Create: `src/ard_ossie/versioning.py`
- Create: `schemas/reports/version-report.schema.json`
- Create: `tests/unit/test_versioning.py`

**Interfaces:**
- Consumes: current registry entity, candidate canonical hash, submitted base and proposed versions
- Produces: `plan_version(current, candidate_hash, base_version, proposed_version) -> VersionDecision`

- [x] **Step 1: Write failing transition tests**

```python
@pytest.mark.parametrize(
    ("current", "changed", "base", "proposed", "code"),
    [
        (None, True, None, 1, "NEW_V1"),
        (11, True, 11, 12, "ADVANCE"),
        (11, False, 11, 11, "NO_CHANGE"),
        (11, True, 10, 11, "VERSION_STALE"),
        (11, True, 11, 13, "VERSION_GAP"),
        (11, False, 11, 12, "VERSION_NO_CHANGE"),
        (999, True, 999, None, "VERSION_LIMIT_REACHED"),
    ],
)
def test_numeric_version_transition(current, changed, base, proposed, code):
    assert plan_version(current, changed, base, proposed).code == code
```

- [x] **Step 2: Run and verify RED**

Run: `python -m pytest tests/unit/test_versioning.py -v`

Expected: import fails because the planner is absent.

- [x] **Step 3: Implement one-step version decisions**

```python
def expected_version(current: int | None, changed: bool) -> int | None:
    if current is None:
        return 1
    if not changed:
        return current
    if current == 999:
        return None
    return current + 1
```

Return structured errors for stale base, gap, collision, no-change bump and exhausted range. Apply the function separately to the product and every changed table; do not derive table versions from product versions.

- [x] **Step 4: Verify focused and schema tests**

Run: `python -m pytest tests/unit/test_versioning.py -v`

Expected: all transition cases pass and report JSON validates.

- [x] **Step 5: Commit**

```bash
git add src/ard_ossie/versioning.py schemas/reports/version-report.schema.json tests/unit/test_versioning.py
git commit -m "feat: enforce independent numeric versions"
```

### Task 4: Per-entity Registry and shared-table changesets

**Files:**
- Create: `src/ard_ossie/registry.py`
- Create: `src/ard_ossie/impact.py`
- Create: `schemas/reports/impact-report.schema.json`
- Create: `tests/unit/test_registry.py`
- Create: `tests/unit/test_impact.py`
- Create: `tests/fixtures/registry/`

**Interfaces:**
- Consumes: product, table and mapping records
- Produces: `Registry.load(root)`, `Registry.write_candidate(change)`, `impacted_products(table_id)`, `build_changeset(table_ids, initiating_product_id)`

- [x] **Step 1: Write failing persistence and tombstone tests**

```python
def test_registry_writes_one_file_per_entity(tmp_path):
    registry = Registry(tmp_path)
    registry.write_product(product_record())
    assert (tmp_path / "products" / f"{PRODUCT_ID}.json").exists()

def test_retired_id_cannot_be_reused(tmp_path):
    registry = registry_with_retired_table(tmp_path)
    with pytest.raises(IdentityConflict, match="RETIRED_ID_REUSE"):
        registry.write_table(table_record(table_id=RETIRED_TABLE_ID, status="active"))
```

- [x] **Step 2: Verify RED, then implement atomic Registry writes and generated indexes**

Write to a sibling temporary file, `fsync`, then replace. Generate `indexes/product-keys.json` and `indexes/table-locators.json` from entity records; reject direct index values that disagree with canonical records.

- [x] **Step 3: Write failing shared-impact tests**

```python
def test_changed_shared_table_requires_every_linked_product():
    impact = analyze_table_change(TABLE_ID, mappings_for(PRODUCT_A, PRODUCT_B))
    assert impact.required_product_ids == [PRODUCT_A, PRODUCT_B]

def test_changeset_is_incomplete_until_every_product_version_is_ready():
    change = build_changeset(TABLE_ID, [PRODUCT_A, PRODUCT_B])
    change.mark_ready(PRODUCT_A, version=4)
    assert change.status == "blocked"
    change.mark_ready(PRODUCT_B, version=9)
    assert change.status == "ready"
```

- [x] **Step 4: Implement changeset state and verify GREEN**

Persist `changeset_id`, changed table base/proposed versions, required product IDs, PR numbers and status under `registry/changesets/<id>.json`. Only `ready` permits release dispatch.

Run: `python -m pytest tests/unit/test_registry.py tests/unit/test_impact.py -v`

- [x] **Step 5: Commit**

```bash
git add src/ard_ossie/registry.py src/ard_ossie/impact.py schemas/reports/impact-report.schema.json tests/unit/test_registry.py tests/unit/test_impact.py tests/fixtures/registry
git commit -m "feat: coordinate shared table changes"
```

### Task 5: Docling ingestion and OpenAI-compatible semantic extraction

**Files:**
- Create: `src/ard_ossie/ingestion.py`
- Create: `src/ard_ossie/docling_parser.py`
- Create: `src/ard_ossie/excel_adapter.py`
- Create: `src/ard_ossie/llm.py`
- Create: `tests/unit/test_ingestion.py`
- Create: `tests/unit/test_llm.py`
- Create: `tests/integration/test_docling_pipeline.py`
- Create: `tests/fixtures/product/`

**Interfaces:**
- Consumes: exactly one HTML product document, one DOCX or PDF semantic document and one XLSX dictionary
- Produces: `ParsedSources`, `Evidence`, `AISuggestion`; never allocates identity IDs

- [x] **Step 1: Write failing source-role tests**

```python
def test_source_set_requires_each_ard_role(tmp_path):
    with pytest.raises(SourceValidationError, match="MISSING_DICTIONARY"):
        scan_sources(tmp_path / "sources-without-xlsx")

def test_source_set_rejects_two_semantic_documents(tmp_path):
    with pytest.raises(SourceValidationError, match="MULTIPLE_SEMANTIC_DOCUMENTS"):
        scan_sources(tmp_path / "sources-with-docx-and-pdf")
```

- [x] **Step 2: Verify RED, implement source scanning, then verify GREEN**

Accept `.html`, exactly one of `.docx`/`.pdf`, and `.xlsx`; reject symlinks, path traversal, macro-enabled workbooks and files over the configured limits. Record SHA-256 and role in the source manifest.

- [x] **Step 3: Write Docling and Excel fixture tests before adapters**

The integration test must assert that fixture headings, PDF page evidence, Excel sheet/cell coordinates, column types and PK/FK values survive parsing. Run it once and confirm failure because adapters are missing.

- [x] **Step 4: Implement adapters and the provider boundary**

```python
class LLMProvider(Protocol):
    def generate_structured(self, *, schema: dict[str, object], messages: list[dict[str, str]]) -> dict[str, object]: ...

class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: SecretStr, model: str, timeout_seconds: int = 120): ...
```

Use Docling for HTML/DOCX/PDF structure and openpyxl for cell coordinates, formulas, merged cells and comments. Validate LLM output locally; reject any suggestion that writes physical table/column identity. Attach evidence locator and `ai_suggested` to accepted semantic candidates.

- [x] **Step 5: Run focused integration tests and commit**

Run: `python -m pytest tests/unit/test_ingestion.py tests/unit/test_llm.py tests/integration/test_docling_pipeline.py -v`

```bash
git add src/ard_ossie/ingestion.py src/ard_ossie/docling_parser.py src/ard_ossie/excel_adapter.py src/ard_ossie/llm.py tests/unit/test_ingestion.py tests/unit/test_llm.py tests/integration/test_docling_pipeline.py tests/fixtures/product
git commit -m "feat: parse ARD source documents"
```

### Task 6: Canonical IR renderers and Ossie 0.1.1 compiler

**Files:**
- Create: `src/ard_ossie/ir.py`
- Create: `src/ard_ossie/renderers.py`
- Create: `src/ard_ossie/ossie_compiler.py`
- Create: `schemas/ossie/0.1.1/osi-schema.json`
- Create: `templates/data-product.md.j2`
- Create: `templates/data-semantic.md.j2`
- Create: `tests/unit/test_renderers.py`
- Create: `tests/unit/test_ossie_compiler.py`
- Create: `tests/golden/sales-order/`

**Interfaces:**
- Consumes: `ParsedSources`, accepted semantic suggestions and resolved Registry IDs
- Produces: canonical IR, byte-stable Markdown/dictionary JSON and schema-valid Ossie 0.1.1 JSON

- [x] **Step 1: Vendor and checksum the approved Ossie schema**

Copy the exact `osi-0.1.1-rc1` schema into `schemas/ossie/0.1.1/osi-schema.json`, record its SHA-256 in a sibling `SHA256SUMS`, and add a test that fails if either file changes independently.

- [x] **Step 2: Write failing deterministic renderer tests**

```python
def test_renderers_match_golden_files(resolved_sales_order_ir):
    assert render_product_markdown(resolved_sales_order_ir) == golden("data-product.md")
    assert render_semantic_markdown(resolved_sales_order_ir) == golden("data-semantic.md")
    assert render_dictionary_json(resolved_sales_order_ir) == golden("data-dictionary.json")
```

Run: `python -m pytest tests/unit/test_renderers.py -v`

Expected: import fails because IR and renderers do not exist.

- [x] **Step 3: Implement canonical IR and renderers**

Sort tables by immutable `table_id`, columns by ordinal then `column_id`, metrics by `metric_id`, and mappings by `link_id`. Emit UTF-8, LF and one final newline. Preserve every generated semantic value's evidence and `ai_suggested` status in IR while keeping the public Markdown concise.

- [x] **Step 4: Write the failing Ossie compiler contract and implement it**

```python
def test_compiler_emits_ossie_011_with_internal_ids(resolved_sales_order_ir):
    model = compile_ossie(resolved_sales_order_ir)
    validate(instance=model, schema=load_ossie_011_schema())
    assert model["version"] == "0.1.1"
    assert "ai_ready_data" in json.dumps(model["custom_extensions"])
```

Map only resolved datasets, fields, relationships, metrics and AI context. Reject unresolved references and SQL dialect loss before writing output.

- [x] **Step 5: Run golden/schema verification and commit**

Run: `python -m pytest tests/unit/test_renderers.py tests/unit/test_ossie_compiler.py -v`

```bash
git add src/ard_ossie/ir.py src/ard_ossie/renderers.py src/ard_ossie/ossie_compiler.py schemas/ossie templates tests/unit/test_renderers.py tests/unit/test_ossie_compiler.py tests/golden/sales-order
git commit -m "feat: compile deterministic Ossie artifacts"
```

### Task 7: Atomic product processor and CLI quality gate

**Files:**
- Create: `src/ard_ossie/pipeline.py`
- Create: `src/ard_ossie/cli.py`
- Create: `schemas/reports/quality-report.schema.json`
- Create: `tests/integration/test_cli_process.py`
- Create: `tests/integration/test_atomic_promotion.py`

**Interfaces:**
- Consumes: parsed sources, Registry, provider config and product path
- Produces: generated MD/JSON/Ossie, manifest, duplicate/version/impact/quality reports and process exit code

- [x] **Step 1: Write failing CLI acceptance tests**

```python
def test_process_emits_all_required_artifacts(runner, product_fixture):
    result = runner.invoke(app, ["process", str(product_fixture), "--registry", "registry"])
    assert result.exit_code == 0
    assert required_outputs(product_fixture) == {
        "data-product.md", "data-semantic.md", "data-dictionary.json", "ossie-model.json"
    }

def test_hard_quality_error_keeps_previous_generated_directory(runner, invalid_fixture):
    before = tree_hash(invalid_fixture / "generated")
    result = runner.invoke(app, ["process", str(invalid_fixture), "--registry", "registry"])
    assert result.exit_code == 2
    assert tree_hash(invalid_fixture / "generated") == before
```

- [x] **Step 2: Verify RED and implement candidate build directory**

Build into `.build/<content-hash>/candidate`, run source, IR, duplicate, version, reference, Ossie and completeness checks, then replace `generated/` only when hard errors are zero.

- [x] **Step 3: Implement the command surface**

```text
ard process <product-path> --registry registry --report quality/report.json
ard registry check <candidate.json> --registry registry
ard impact table <table-id> --registry registry
ard release plan <product-id> --registry registry
ard history <product-key>
ard show <product-key>@<number>
ard diff <product-key>@<number>..<number>
```

Exit `0` for success, `1` for warning-only when `--warnings-as-errors` is set, `2` for validation failure and `3` for provider/infrastructure failure.

- [x] **Step 4: Run all tests and determinism check**

Run: `python -m pytest -q && uv run ard process tests/fixtures/product/sales-order --registry tests/fixtures/registry && uv run ard process tests/fixtures/product/sales-order --registry tests/fixtures/registry`

Expected: tests pass and the second generated tree hash equals the first.

- [x] **Step 5: Commit**

```bash
git add src/ard_ossie/pipeline.py src/ard_ossie/cli.py schemas/reports/quality-report.schema.json tests/integration/test_cli_process.py tests/integration/test_atomic_promotion.py
git commit -m "feat: add ARD processing quality gate"
```

### Task 8: Approved Issue intake contract

**Files:**
- Create: `.github/ISSUE_TEMPLATE/ard-content.yml`
- Create: `src/ard_ossie/github_event.py`
- Create: `tests/unit/test_github_event.py`
- Create: `.github/workflows/ard-issue-intake.yml`

**Interfaces:**
- Consumes: `issues:labeled` event JSON and GitHub collaborator permission response
- Produces: sanitized intake manifest, `ard/issue-<number>-<product-key>` branch, source commit and Draft PR

- [x] **Step 1: Write failing Issue body and authorization tests**

```python
def test_approved_label_requires_write_permission():
    assert authorize_label("write", "ard:approved").allowed
    assert not authorize_label("read", "ard:approved").allowed

def test_attachment_rejects_non_github_host():
    with pytest.raises(AttachmentSecurityError, match="UNTRUSTED_ATTACHMENT_HOST"):
        validate_attachment_url("https://example.org/dictionary.xlsx")

def test_issue_form_requires_one_product_and_three_source_roles():
    intake = parse_issue_body(load_fixture("valid-issue.md"))
    assert intake.product_key == "sales-order"
    assert set(intake.attachments) == {"product_html", "semantic_document", "dictionary_excel"}
```

- [x] **Step 2: Verify RED and implement strict Issue parsing**

Accept only GitHub-owned attachment hosts, validate every redirect, cap configured byte size before storing, verify magic/MIME/extension agreement and write the content SHA-256 into the intake manifest.

- [x] **Step 3: Create the Issue Form**

Require operation, product key, existing product ID for update/retire, requested numeric version, one attachment field per source role and change reason. Explain that every submission and attachment becomes public.

- [x] **Step 4: Create and statically validate the workflow**

The workflow must use `issues: [labeled]`, test for `ard:approved`, set explicit `contents: write`, `issues: write`, `pull-requests: write`, `statuses: write`, use `concurrency: ard-intake-${{ github.event.issue.number }}`, and keep `ARD_LLM_API_KEY` out of the authorization job. After the source commit and Draft PR are created, call `ard-process.yml` directly rather than waiting for a token-generated event.

Run: `python -m pytest tests/unit/test_github_event.py -v && actionlint .github/workflows/ard-issue-intake.yml`

- [x] **Step 5: Commit**

```bash
git add .github/ISSUE_TEMPLATE/ard-content.yml .github/workflows/ard-issue-intake.yml src/ard_ossie/github_event.py tests/unit/test_github_event.py
git commit -m "feat: ingest approved ARD issues"
```

### Task 9: Reusable processing and direct-branch workflows

**Files:**
- Create: `.github/workflows/ard-process.yml`
- Create: `.github/workflows/ard-direct-change.yml`
- Create: `tests/integration/test_workflow_contracts.py`

**Interfaces:**
- Consumes: trusted branch, product key, PR number and source event
- Produces: generated commit, `ard/quality-gate` commit status, quality artifact and PR summary

- [x] **Step 1: Write failing workflow contract tests**

Load YAML as data and assert externally observable security contracts: explicit permissions, no `pull_request_target`, fork guard before the secret-bearing job, one-product path guard, source-only trigger paths, product-key concurrency and pinned Action SHAs.

- [x] **Step 2: Verify RED and implement `ard-process.yml`**

Use `workflow_call` inputs `branch`, `product_key`, `pr_number`, `allow_writeback`. Checkout the exact branch, install the locked project, call `ard process`, commit only known generated/registry/report paths, push with `GITHUB_TOKEN`, then post `ard/quality-gate` status on the final SHA. Upload the quality reports with 30-day retention.

- [x] **Step 3: Implement `ard-direct-change.yml`**

Trigger trusted non-main pushes under `products/*/sources/**`. Reject changes spanning more than one product. Find or create the product PR, then call the reusable processor. A PR from a fork runs source/schema checks only and never receives LLM secrets or writeback permissions.

- [x] **Step 4: Validate workflow behavior**

Run: `python -m pytest tests/integration/test_workflow_contracts.py -v && actionlint .github/workflows/ard-process.yml .github/workflows/ard-direct-change.yml`

Expected: all contracts pass; actionlint reports no findings.

- [x] **Step 5: Commit**

```bash
git add .github/workflows/ard-process.yml .github/workflows/ard-direct-change.yml tests/integration/test_workflow_contracts.py
git commit -m "feat: process trusted ARD pull requests"
```

### Task 10: Changeset coordination and numeric releases

**Files:**
- Create: `.github/workflows/ard-changeset.yml`
- Create: `.github/workflows/ard-release.yml`
- Create: `src/ard_ossie/release.py`
- Create: `tests/unit/test_release.py`
- Modify: `tests/integration/test_workflow_contracts.py`

**Interfaces:**
- Consumes: merged product PR, ready changeset, manifest and validation report
- Produces: product/table tags, product GitHub Release bundle and approved `ard_product_released` repository dispatch

- [x] **Step 1: Write failing tag and release-plan tests**

```python
def test_release_tags_use_immutable_ids_and_numeric_versions():
    plan = build_release_plan(product(version=12), [table(version=7)])
    assert plan.product_tag == f"product/{PRODUCT_ID}/v12"
    assert plan.table_tags == [f"table/{TABLE_ID}/v7"]

def test_incomplete_changeset_blocks_release_dispatch():
    with pytest.raises(ReleaseBlocked, match="CHANGESET_INCOMPLETE"):
        build_release_plan(product(), tables(), changeset=blocked_changeset())
```

- [x] **Step 2: Verify RED, implement release bundle and verify GREEN**

Bundle the four public artifacts, manifest and validation reports from the merged commit. Reject an existing tag pointing at another commit, stale base versions, nonzero hard errors or incomplete shared-table changesets.

- [x] **Step 3: Implement changeset workflow**

Create/update `registry/changesets/<id>.json`, open linked product Draft PRs, publish the impact table in the initiating PR and register `ard/changeset` status. Serialize Registry mutations with `concurrency: ard-registry-write`.

- [x] **Step 4: Implement release workflow and verify**

Trigger on `push` to `main` under product/registry paths. Create product and changed-table tags, create the product GitHub Release, then use the protected `production-linkage` environment before emitting `repository_dispatch` event `ard_product_released` with product ID, numeric version, tag, commit and artifact hashes.

Run: `python -m pytest tests/unit/test_release.py tests/integration/test_workflow_contracts.py -v && actionlint .github/workflows/ard-changeset.yml .github/workflows/ard-release.yml`

- [x] **Step 5: Commit**

```bash
git add .github/workflows/ard-changeset.yml .github/workflows/ard-release.yml src/ard_ossie/release.py tests/unit/test_release.py tests/integration/test_workflow_contracts.py
git commit -m "feat: release numeric ARD versions"
```

### Task 11: Documentation, end-to-end fixture and repository verification

**Files:**
- Modify: `README.md`
- Create: `docs/github-actions-setup.md`
- Create: `tests/e2e/test_approved_issue_to_release.py`
- Create: `tests/fixtures/github/`

**Interfaces:**
- Consumes: all prior CLI and workflow contracts
- Produces: operator setup guide and executable acceptance evidence

- [x] **Step 1: Write the failing end-to-end fixture test**

Use local event fixtures and a temporary Git repository to simulate approved Issue intake, source commit, processing, duplicate/version reports, generated commit and release plan. Assert product `v1`, shared table `v1`, stable IDs, complete provenance and no secret values in tracked files.

- [x] **Step 2: Run and verify RED, then add the minimum orchestration fixture**

Run: `python -m pytest tests/e2e/test_approved_issue_to_release.py -v`

Expected initial failure: the fixture orchestration entrypoint is absent. Add only the local event runner needed to exercise existing production commands; do not duplicate production decisions inside the test.

- [x] **Step 3: Document repository configuration**

Document these exact settings:

- Secrets: `ARD_LLM_API_KEY`; optional secret `ARD_LLM_BASE_URL`
- Variables: `ARD_LLM_MODEL`, `ARD_LLM_API_STYLE`, file-size limits
- Labels: `ard:submission`, `ard:approved`, `ard:processing`, `ard:failed`, `ard:pr-created`
- Required status: `ard/quality-gate`, `ard/changeset`
- Branch protection: no direct `main` push, required review and current branch
- Environment: `production-linkage` with required reviewers
- Actions: permit repository `GITHUB_TOKEN` to create PRs and set explicit workflow permissions

- [x] **Step 4: Run final verification**

Run: `python -m pytest -q && actionlint .github/workflows/*.yml && git diff --check && rg -n "(sk-[A-Za-z0-9]|ARD_LLM_API_KEY=|Authorization: Bearer)" . --glob '!docs/**'`

Expected: all tests pass, actionlint and diff checks are clean, and the secret scan finds no credential values.

- [x] **Step 5: Commit**

```bash
git add README.md docs/github-actions-setup.md tests/e2e tests/fixtures/github
git commit -m "docs: add ARD pipeline operations guide"
```

---

## Self-Review Result

- Spec coverage: Issue approval, direct commits, same-PR writeback, OpenAI-compatible secrets, Docling parsing, UUIDv7 identities, deterministic IR/Ossie 0.1.1 compilation, duplicate checks, independent numeric versions, shared changesets, current-only storage, tags, Releases and protected linkage each map to a task.
- Placeholder scan: the plan contains no deferred implementation markers; concrete values, file paths, signatures, commands and error codes are specified.
- Type consistency: Tasks 2-4 consume Task 1 models; Tasks 5-6 produce parsed and compiled artifacts; Task 7 composes Tasks 2-6 behind the CLI; Tasks 8-10 call the Task 7 CLI; Task 11 validates the public contracts end to end.
