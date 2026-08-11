# Issue Attachment and Data Dictionary Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept GitHub's canonical `/files/<id>/<filename>` Issue attachments, enforce create/update product-ID semantics, and parse the approved multi-sheet Korean Data Dictionary template into deterministic ARD table metadata.

**Architecture:** Keep attachment trust checks in `github_event.py` and add a second exact path validator without expanding the host boundary. Refactor `excel_adapter.py` into ordered sheet dialect parsers that normalize to the existing `ParsedDictionary` model, with optional table descriptions passed into pipeline drafts. Every behavior is implemented through a RED/GREEN test cycle before documentation and live GitHub verification.

**Tech Stack:** Python 3.12, Pydantic 2, openpyxl, pytest, Ruff, uv, GitHub Actions.

## Global Constraints

- Initial attachment host remains exactly `github.com`; storage hosts remain redirect-only.
- No query, fragment, credentials, non-443 port, decoded path separator, traversal, or extra path segment is accepted.
- Missing platform is exactly `unspecified`; vendor/platform is never inferred.
- A targetless `FK` marker never creates a relationship.
- Existing flat English workbooks retain their normalized output.
- Unsupported or malformed recognized input fails closed with a typed error.
- No force push or branch-protection bypass is permitted.

---

### Task 1: Canonical GitHub `/files` attachment URLs

**Files:**
- Modify: `src/ard_ossie/github_event.py`
- Modify: `tests/unit/test_github_event.py`

**Interfaces:**
- Consumes: `validate_attachment_url(url: str) -> str` and `_validate_attachment_redirect_url(url: str) -> str`.
- Produces: `_validate_user_attachment_path(path: str) -> None` accepting exactly the UUID and numeric-file path families.

- [x] **Step 1: Add failing behavior tests**

Add a success assertion for `https://github.com/user-attachments/files/30932953/Marketing.Insight.Data.Dictionary.xlsx`. Add parameterized rejections for `/files/030932953/name.xlsx`, missing filename, an extra segment, `%2F` in the decoded filename, `.`/`..`, malformed `%`, query, and fragment. Assert the same valid path works as a redirect target.

- [x] **Step 2: Run the focused URL tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv run --frozen pytest -q tests/unit/test_github_event.py
```

Expected: the canonical `/files` success cases fail with `UNTRUSTED_ATTACHMENT_PATH` while all existing `/assets` behavior remains green.

- [x] **Step 3: Implement the second exact path family**

Use separate regular expressions for canonical UUID assets and positive decimal file IDs. Decode the single filename segment only after validating percent escapes, then reject separators, control characters, dot paths, hidden leading-dot paths, and names over 255 characters. Reuse this validator for initial URLs and GitHub-host redirects.

- [x] **Step 4: Run the URL tests for GREEN and commit**

Run the command from Step 2, then commit `src/ard_ossie/github_event.py` and `tests/unit/test_github_event.py` with message `fix: accept canonical GitHub file attachments`.

### Task 2: Explicit create/update product-ID validation

**Files:**
- Modify: `src/ard_ossie/github_event.py`
- Modify: `tests/unit/test_github_event.py`
- Modify: `.github/ISSUE_TEMPLATE/ard-content.yml`

**Interfaces:**
- Consumes: raw `IssueIntake` input containing `operation` and optional `product_id`.
- Produces: `PRODUCT_ID_FORBIDDEN_FOR_CREATE` before ProductId pattern parsing, while retaining `PRODUCT_ID_REQUIRED_FOR_UPDATE`.

- [x] **Step 1: Add a failing invalid-value create test**

Construct the Issue #3 combination `operation=create`, `product_id=Marketing Insight` and assert `parse_issue_body` raises `PRODUCT_ID_FORBIDDEN_FOR_CREATE`. Add a valid-shaped existing ID create case with the same result.

- [x] **Step 2: Run the focused test and verify RED**

Run the specific new tests. Expected: the arbitrary text case reports generic ProductId validation and the valid-shaped ID case is currently accepted.

- [x] **Step 3: Add a before-model validator and clarify the template**

Inspect the raw mapping in `IssueIntake` before field validation. If normalized operation is `create` and `product_id` is non-empty, raise `PRODUCT_ID_FORBIDDEN_FOR_CREATE`. Change the Issue template description to `Must be empty for create; required for update.`

- [x] **Step 4: Run all GitHub event tests for GREEN and commit**

Run Task 1's focused command and commit the model, tests, and template with message `fix: enforce issue operation product identity`.

### Task 3: Multi-sheet Korean Data Dictionary dialect

**Files:**
- Modify: `src/ard_ossie/excel_adapter.py`
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `tests/unit/test_excel_adapter.py`
- Modify: `tests/unit/test_pipeline.py`

**Interfaces:**
- Consumes: an openpyxl worksheet and the existing source hash.
- Produces: normalized `DictionaryTable(locator, description, columns)` objects from either the flat dialect or the Korean template dialect.

- [x] **Step 1: Generate an exact-shape failing workbook test**

Build a workbook with two table sheets, metadata in rows 3/4/7, the Korean header in row 13, `PK`, `FK`, and `PK, FK` rows, then a blank row followed by a duplicate residue row. Assert deterministic locators beginning with `unspecified`, table descriptions, contiguous ordinals, nullability/PK mapping, no inferred foreign key, and exclusion of residue.

- [x] **Step 2: Verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv run --frozen pytest -q tests/unit/test_excel_adapter.py
```

Expected: the new workbook fails with `MISSING_DICTIONARY_HEADERS`.

- [x] **Step 3: Implement ordered dialect dispatch**

Extract the existing row-1 behavior into a flat-sheet parser. Add a Korean parser recognized by its metadata and four required column headers. Parse two-part storage locations as `unspecified|catalog|schema|table`, allow an explicit three-part platform form, end at the first fully blank data row, and preserve exact evidence ranges.

- [x] **Step 4: Add malformed and duplicate fail-closed tests**

Assert a recognized Korean sheet with an invalid one-part location raises `INVALID_KOREAN_DICTIONARY_LOCATION`, and duplicate live column names raise `DUPLICATE_DICTIONARY_COLUMN`. Keep the existing unsupported-header and unknown-boolean tests.

- [x] **Step 5: Pass dictionary table descriptions into the pipeline**

Add optional `description` to `DictionaryTable`, initialize `_TableDraft.description` from it in `_resolve_tables`, and assert the deterministic description exists before LLM suggestions are applied.

- [x] **Step 6: Run focused tests for GREEN and commit**

Run the Excel and pipeline unit files. Commit implementation and tests with message `feat: parse Korean multi-sheet data dictionaries`.

### Task 4: Documentation, full verification, and operational proof

**Files:**
- Modify: `docs/github-actions-setup.md`
- Modify: `docs/superpowers/specs/2026-08-10-trusted-processing-boundary-design.md`
- Create: `docs/superpowers/specs/2026-08-11-issue-attachment-dictionary-compatibility-design.md`
- Create: `docs/superpowers/plans/2026-08-11-issue-attachment-dictionary-compatibility.md`

**Interfaces:**
- Consumes: the exact Issue #3 workbook, local verification commands, PR checks, and Issue events.
- Produces: a protected merged compatibility change and a managed ARD Draft PR from Issue #3.

- [x] **Step 1: Document both canonical attachment forms and dialect behavior**

Replace UUID-only prose with the two exact GitHub URL forms. Document Korean sheet metadata, `unspecified` platform, targetless FK behavior, and operation-specific product-ID rules.

- [x] **Step 2: Parse the exact submitted workbook**

Run `parse_dictionary` against the downloaded Issue #3 file and assert four tables, 40 total columns, and locators for campaign, creative, delivery, and outcome.

- [ ] **Step 3: Run fresh complete local verification**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv run --frozen pytest -q
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv run --frozen ruff check src tests
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv build --sdist --wheel
git diff --check
```

Also run the repository static verifier using the feature head and confirm zero failures.

- [ ] **Step 4: Review, publish a PR, and require same-head checks**

Review the complete diff against this plan, push without force, create a Draft PR targeting `main`, and require the same head SHA to receive successful `ard/quality-gate` and `ard/changeset` statuses before merge.

- [ ] **Step 5: Merge and emit a corrected Issue event**

After protected merge, replace Issue #3's Existing product ID value with `_No response_`. Remove `ard:approved` and stale failure/processing labels, then add `ard:approved` again so GitHub emits a fresh `issues:labeled` event.

- [ ] **Step 6: Prove the live processing result**

Inspect the new workflow run and require intake to create the managed `ard/issue-3-500138301` Draft PR. Inspect validation and protected processing jobs, report any Environment approval requirement, and record the terminal check/result URLs without exposing the LLM secret.
