# Provenance-neutral Table Change Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task with review checkpoints.

**Goal:** Make table version decisions depend only on the published content of that table so one
workbook cell change cannot falsely version unrelated shared tables.

**Architecture:** Parse the prior published dictionary into a strict baseline model, bind that model
to the existing product's Registry mappings, and compare a versioned provenance-neutral projection
for each table. Protected processing injects baseline bytes read from the exact default-branch SHA;
local adapters explicitly snapshot their pre-run generated dictionary. Existing tables fail closed
without a valid baseline. Unchanged tables reuse their legacy Registry records, while changed and
new tables receive the new deterministic content hash.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, Typer, Git revision reads, Ruff, ARD repository
static verifier.

## Constraints

- Work only in `/home/moohyunkim/workspace/ard-ossie-provider/.worktrees/shared-table-e2e-refresh`
  on branch `fix/table-semantic-change-detection` based on `origin/main` SHA
  `367af2c78b6fabc169c85ef3c690940e2cc3c800`.
- Preserve the existing uncommitted `.gitignore` change and never stage it.
- Do not modify `products/**`, `registry/**`, workflows, production Issues #57/#58, or tracking PRs
  #55/#56 in this stage.
- Keep `Evidence` in current `TableIR` and audit artifacts. Exclude it only from table change/hash
  authority.
- Do not migrate unchanged legacy Registry hashes.
- Use stable redacted errors `TABLE_BASELINE_REQUIRED` and `TABLE_BASELINE_INVALID`; never include
  baseline JSON, IDs selected from malformed input, source excerpts, or bytes in workflow output.
- Do not retry the production Issues until this PR and the later product-fact singleton PR are both
  merged.

---

### Task 1: Define the strict published-dictionary baseline contract

**Files:**

- Create: `src/ard_ossie/table_baseline.py`
- Create: `tests/unit/test_table_baseline.py`

**Interfaces:**

- `parse_table_baseline(payload: bytes) -> PublishedDictionaryBaseline`
- `validate_table_baseline(baseline, *, product: ProductRecord, registry: Registry) -> dict[str, PublishedTableBaseline]`
- `read_local_table_baseline(product_path: Path) -> bytes | None`
- `table_content_hash(table: PublishedTableBaseline, *, locator: TableLocator) -> str`
- Error codes: `TABLE_BASELINE_REQUIRED`, `TABLE_BASELINE_INVALID`

- [ ] **Step 1: Write failing strict parsing tests**

Create baseline fixtures matching `render_dictionary_json`: product ID/version, table ID/version,
dataset name, source, description, and rendered column fields. Test valid parsing and parameterize
invalid UTF-8, unknown fields, duplicate table IDs, duplicate column IDs, duplicate ordinals, and
duplicate case-folded column names.

The public assertion must be code-only:

```python
with pytest.raises(TableBaselineError) as captured:
    parse_table_baseline(payload)
assert str(captured.value) == "TABLE_BASELINE_INVALID"
assert "secret marker" not in str(captured.value)
```

- [ ] **Step 2: Run the new tests and confirm RED**

```bash
uv run --frozen pytest tests/unit/test_table_baseline.py -q
```

Expected: collection or import fails because the contract does not exist.

- [ ] **Step 3: Implement the minimal strict models and parser**

Use `StrictModel`, existing typed IDs and `Version`. Model only fields emitted by
`render_dictionary_json`; optional `foreign_key`, `formula`, and `comment` default to `None`.
Add after-model validators for unique table and column identities. Decode bytes as strict UTF-8 and
translate `UnicodeDecodeError`, JSON/Pydantic validation errors, and duplicate violations into
`TableBaselineError("TABLE_BASELINE_INVALID") from None`.

The content projection must contain an explicit discriminator such as
`"schema_version": "table-content-v1"`, exclude `table_version`, and include the trusted four-part
`TableLocator` plus all published table/column content. Sort columns by `(ordinal, column_id)` before
calling `canonical_hash`.

- [ ] **Step 4: Add Registry binding tests**

Build a Registry product, exact mappings, and table records. Assert validation accepts the exact
baseline and rejects, with only `TABLE_BASELINE_INVALID`, each of:

- wrong product ID or product version;
- missing or extra mapped table;
- table version different from its mapping;
- dataset/source different from the Registry locator; and
- a baseline table ID not owned through the product mapping.

- [ ] **Step 5: Add local snapshot safety tests**

Assert `read_local_table_baseline` returns `None` when the generated dictionary is absent, returns
the original bytes without parsing when it is a regular file, and rejects a symlink/non-regular
path with the stable invalid code.

- [ ] **Step 6: Run focused tests and lint**

```bash
uv run --frozen pytest tests/unit/test_table_baseline.py -q
uv run --frozen ruff check src/ard_ossie/table_baseline.py tests/unit/test_table_baseline.py
```

Expected: all tests pass and Ruff exits `0`.

- [ ] **Step 7: Commit the baseline contract**

```bash
git add src/ard_ossie/table_baseline.py tests/unit/test_table_baseline.py
git commit -m "feat: define trusted table baseline contract"
```

---

### Task 2: Drive table version decisions from content projections

**Files:**

- Modify: `src/ard_ossie/pipeline.py`
- Modify: `tests/unit/test_pipeline.py`
- Create: `tests/integration/test_table_change_detection.py`

**Interfaces:**

- Extend `process_product(..., table_baseline: bytes | None = None)`.
- Extend `_build_table_records(drafts, registry, baseline_tables)`.
- Preserve the existing `ProcessResult`, quality report, version report, and changeset error
  contracts.

- [ ] **Step 1: Write a failing unit regression for workbook-wide provenance churn**

Construct two current drafts with source hash `b * 64` and a trusted baseline representing the same
published content from source hash `a * 64`. Change only the first table's description. Give the
existing Registry records distinct legacy hashes.

Assert:

```python
assert [decision.changed for decision in decisions] == [True, False]
assert records[0].version == 2
assert records[0].canonical_hash == expected_table_content_hash
assert records[1] is existing_second_table
assert records[1].canonical_hash == legacy_second_hash
assert irs[1].columns[0].evidence[0].source_hash == "b" * 64
```

The final assertion proves current provenance is still emitted even though it does not version the
table.

- [ ] **Step 2: Run the focused unit regression and confirm RED**

```bash
uv run --frozen pytest tests/unit/test_pipeline.py \
  -k 'table_records and (provenance or legacy)' -q
```

Expected: the unchanged second table is currently classified as changed.

- [ ] **Step 3: Validate the baseline before document/provider work**

In `process_product`, load the Registry snapshot and config, resolve the existing product, and then:

- require `table_baseline` when an existing product exists;
- parse and Registry-bind the baseline once;
- allow `None` only when the product does not yet exist; and
- perform this validation before parser/provider execution and before any candidate directory or
  Registry mutation.

Pass the validated table mapping into `_build_table_records`.

- [ ] **Step 4: Implement projection-based records**

For every draft, build the current published table projection and hash. For new tables, retain the
existing create/version logic and store the new hash. For existing tables, compare the trusted
baseline projection to the current projection:

- equal: `changed=False`, reuse the exact existing `TableRecord` model;
- different: `changed=True`, create the proposed record with the current content hash.

Continue building `TableIR` from the current draft and current evidence. Continue passing
`changed` and the configured base/proposed versions to `plan_version`; do not weaken numeric version
validation.

- [ ] **Step 5: Add the two-table full pipeline regression**

Create a reproducible two-sheet XLSX fixture. First process it as a new product and save the emitted
dictionary bytes. Add a second product mapping so both tables are shared, add a changeset containing
only the first table, update the first table description and workbook hash, and process product v2
with the saved baseline.

Assert:

- only the first `VersionDecision` is changed;
- first table moves `v1 -> v2`;
- second table stays v1 with its exact legacy hash;
- no `CHANGESET_TABLE_NOT_INCLUDED` appears;
- the new generated dictionary contains current source content; and
- Registry/generated/quality promotion remains atomic.

- [ ] **Step 6: Add required/invalid baseline failure tests**

For an existing product, pass `None`, malformed bytes, wrong product/version, incomplete table set,
and mismatched locator baselines. Assert `TABLE_BASELINE_REQUIRED` or `TABLE_BASELINE_INVALID`, no
provider call, no generated/Registry change, and no input text in the exception.

- [ ] **Step 7: Run pipeline and atomic-promotion tests**

```bash
uv run --frozen pytest \
  tests/unit/test_table_baseline.py \
  tests/unit/test_pipeline.py \
  tests/integration/test_table_change_detection.py \
  tests/integration/test_atomic_promotion.py \
  -q
uv run --frozen ruff check src/ard_ossie/pipeline.py tests/unit/test_pipeline.py \
  tests/integration/test_table_change_detection.py
```

Expected: all focused tests and lint pass.

- [ ] **Step 8: Commit pipeline behavior**

```bash
git add src/ard_ossie/pipeline.py tests/unit/test_pipeline.py \
  tests/integration/test_table_change_detection.py
git commit -m "fix: isolate table content change detection"
```

---

### Task 3: Wire trusted workflow and explicit local baselines

**Files:**

- Modify: `src/ard_ossie/application/processing.py`
- Modify: `src/ard_ossie/application/modeling.py`
- Modify: `src/ard_ossie/cli/process.py`
- Modify: `tests/unit/test_processing_service.py`
- Modify: `tests/integration/test_cli_process.py`
- Modify: `tests/integration/test_granular_cli.py`
- Modify: `tests/e2e/test_approved_issue_to_release.py`
- Modify direct reprocessing cases in: `tests/integration/test_atomic_promotion.py`

**Interfaces:**

- Add `_trusted_table_baseline(git, *, base_sha, product_key) -> bytes | None`.
- Protected service passes `table_baseline=<bytes from base SHA>`.
- CLI/modeling pass bytes returned by `read_local_table_baseline` before staging or processing.

- [ ] **Step 1: Write the protected revision-read test**

Add a valid `products/sales-order/generated/data-dictionary.json` to `FakeGit.revision_files`. Capture
processor kwargs and assert the exact bytes arrive as `table_baseline`. Assert the Git read is
`(base_sha, products/sales-order/generated/data-dictionary.json)` and never the PR head.

Also assert a missing file passes `None` for first-product creation and a non-`REVISION_FILE_NOT_FOUND`
Git error propagates without invoking the provider or processor.

- [ ] **Step 2: Run the protected service tests and confirm RED**

```bash
uv run --frozen pytest tests/unit/test_processing_service.py \
  -k 'table_baseline or matching_base_replay_catalog' -q
```

Expected: processor kwargs do not yet contain `table_baseline`.

- [ ] **Step 3: Implement the exact-base loader and injection**

Reuse `_read_revision_bytes_optional` so the production Git adapter retains its SHA validation,
raw-byte size bound, missing-file classification, and conflict behavior. Read the dictionary after
capturing `base_sha` and before `provider_factory()`. Pass it explicitly to the processor.

Do not parse the file in `ProcessingService`; pipeline validation needs the Registry snapshot and
must produce the stable table-baseline codes.

- [ ] **Step 4: Wire local CLI and modeling adapters**

Snapshot local baseline bytes before calling `process_product`. In `ModelingService`, read from the
validated original product tree before entering or mutating staged state, then pass the bytes into
the staged pipeline call. In the CLI, read before processing so output replacement cannot become its
own baseline.

Update direct integration/E2E reprocessing calls to explicitly save the prior generated dictionary
bytes and pass `table_baseline=...`. Do not add a hidden local fallback inside `process_product`.

- [ ] **Step 5: Add local repeatability and symlink tests**

Retain the CLI test that processes the same product twice and produces byte-identical artifacts.
Add assertions that the second call used the pre-run baseline. Add modeling coverage for an existing
product and verify a symlinked generated baseline is rejected before staging.

- [ ] **Step 6: Run all caller-boundary tests**

```bash
uv run --frozen pytest \
  tests/unit/test_processing_service.py \
  tests/integration/test_cli_process.py \
  tests/integration/test_granular_cli.py \
  tests/integration/test_atomic_promotion.py \
  tests/e2e/test_approved_issue_to_release.py \
  -q
uv run --frozen ruff check src tests
```

Expected: protected and local callers pass without changing public CLI arguments.

- [ ] **Step 7: Commit caller wiring**

```bash
git add src/ard_ossie/application/processing.py \
  src/ard_ossie/application/modeling.py \
  src/ard_ossie/cli/process.py \
  tests/unit/test_processing_service.py \
  tests/integration/test_cli_process.py \
  tests/integration/test_granular_cli.py \
  tests/integration/test_atomic_promotion.py \
  tests/e2e/test_approved_issue_to_release.py
git commit -m "fix: bind processing to trusted table baselines"
```

---

### Task 4: Verify the exact production invariant locally

**Files:**

- Read only: `.ard/run/shared-table-e2e-sources/dictionary-baseline.xlsx`
- Read only: `.ard/run/shared-table-e2e-sources/dictionary-shared-table-v2.xlsx`
- Read only: `products/500138302/generated/data-dictionary.json`
- Read only: `registry/**`
- Modify only if a durable assertion is missing: `tests/integration/test_table_change_detection.py`

- [ ] **Step 1: Verify source identities**

```bash
sha256sum \
  .ard/run/shared-table-e2e-sources/dictionary-baseline.xlsx \
  .ard/run/shared-table-e2e-sources/dictionary-shared-table-v2.xlsx
```

Require baseline SHA `10310e99c8a76b4b030935c432e6f879ac4c56361ee4a6d52d6a17b2726c306a`
and candidate SHA `eded683826ba55405c4963e5982f554bf86bbae34006c468f9a350c86f937375`.

- [ ] **Step 2: Run a read-only projection comparison**

Use `parse_dictionary`, the trusted published baseline parser, and the projection helper in a
temporary directory. Print only table ID, old/new content hash equality, and changed boolean. Do not
write product or Registry paths.

Expected sorted result:

```text
tbl_01a00585-94b8-7e49-ac43-97e00a165e26 changed=true
tbl_01a00585-94b9-70f1-b339-c7b2e9d77704 changed=false
tbl_01a00585-94b9-72c1-8f98-d818ed98b0a8 changed=false
tbl_01a00585-94b9-7cea-a110-ad22ea63a258 changed=false
```

If the helper cannot express this without private setup, add a public pure comparison helper and a
durable integration assertion rather than duplicating production logic in the diagnostic script.

- [ ] **Step 3: Re-run the exact changeset regression**

```bash
uv run --frozen pytest tests/integration/test_table_change_detection.py -q
```

Expected: coordinated first table passes and none of the other three tables is classified as
changed.

- [ ] **Step 4: Commit only if this checkpoint added durable test coverage**

```bash
git add tests/integration/test_table_change_detection.py
git commit -m "test: cover production shared-table provenance churn"
```

Skip the commit when no tracked file changed.

---

### Task 5: Verify and publish the stage-one fix PR

**Files:**

- Verify all files committed by Tasks 1–4.
- Verify design: `docs/superpowers/specs/2026-08-17-provenance-neutral-table-change-detection-design.md`
- Verify plan: `docs/superpowers/plans/2026-08-17-provenance-neutral-table-change-detection.md`

- [ ] **Step 1: Run focused checks**

```bash
uv run --frozen pytest \
  tests/unit/test_table_baseline.py \
  tests/unit/test_pipeline.py \
  tests/unit/test_processing_service.py \
  tests/integration/test_table_change_detection.py \
  tests/integration/test_cli_process.py \
  tests/integration/test_granular_cli.py \
  tests/integration/test_atomic_promotion.py \
  tests/e2e/test_approved_issue_to_release.py \
  -q
uv run --frozen ruff check src tests
git diff --check origin/main...HEAD
```

- [ ] **Step 2: Run the full suite**

```bash
uv run --frozen pytest -q
```

Expected: all tests pass; record the exact count and duration rather than assuming the baseline
count remains 1229.

- [ ] **Step 3: Run the integrated static verifier**

```bash
uv run --frozen ard workflow repository-check \
  --base-ref "$(git rev-parse origin/main)" \
  --head-ref "$(git rev-parse HEAD)" \
  --head-sha "$(git rev-parse HEAD)" \
  --repository . \
  --verification-group static
```

Expected: Ruff, pinned actionlint, workflow schema, model/schema, Ossie checksum, and secret scans
all succeed.

- [ ] **Step 4: Verify exact scope and user-change preservation**

```bash
git status --short --branch
git diff --name-only origin/main...HEAD
git diff --check origin/main...HEAD
git log --oneline origin/main..HEAD
```

Require no `products/**`, `registry/**`, workflow, secret, `.ard/**`, or `.gitignore` path in the PR.
The only uncommitted path may be the pre-existing `.gitignore` user change.

- [ ] **Step 5: Push and open a Draft PR**

```bash
git push -u origin fix/table-semantic-change-detection
gh pr create --draft \
  --repo kimohy/ard-ossie-provider \
  --base main \
  --head fix/table-semantic-change-detection \
  --title "fix: isolate shared-table content changes" \
  --body "Uses the exact default-branch published dictionary as table version authority, excludes workbook-wide provenance from content comparison, preserves unchanged legacy Registry records, and adds production-shape changeset regressions."
```

Capture PR number, URL, and exact `headRefOid`.

- [ ] **Step 6: Verify immutable CI evidence**

```bash
FIX_PR=$(gh pr view --repo kimohy/ard-ossie-provider --json number --jq .number)
FIX_HEAD=$(gh pr view "$FIX_PR" --repo kimohy/ard-ossie-provider --json headRefOid --jq .headRefOid)
gh pr diff "$FIX_PR" --repo kimohy/ard-ossie-provider --name-only
gh pr checks "$FIX_PR" --repo kimohy/ard-ossie-provider --watch --fail-fast
gh api "repos/kimohy/ard-ossie-provider/commits/$FIX_HEAD/status"
```

Require `ard/quality-gate` and `ard/changeset` success on the unchanged exact head, all repository
checks green, and no unresolved review conversation.

- [ ] **Step 7: Pause for explicit merge approval**

Present the PR URL, exact head, changed paths, local test count, static-verifier result, required
statuses, and the fact that Issues #57/#58 will not yet be retried. Merge only after the owner
explicitly approves that exact head.

After merge, verify the code/docs-only change did not trigger a numeric product release. Then begin
the separate product-fact singleton design and PR. Retry Issues #57/#58 only after that second PR is
also merged.
